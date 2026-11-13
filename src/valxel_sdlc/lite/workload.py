"""An OpenChoreo workload.yaml as a Docker Compose service (lite mode).

What OpenChoreo does from the descriptor, Compose does here:

- Endpoints: the component listens on its endpoint ports inside the environment. Nothing is
  published on the VM; a component with an `external` endpoint joins the environment's edge
  network, where the VM's proxy routes to it. Each environment has its own edge network, so
  nothing in staging can reach production through the proxy's network.
- Who may call it (VX-17): each component that has endpoints owns a network, `to-<component>`, and
  is reachable there by its name. A component joins the networks of the components it declares
  under `dependencies.endpoints`, and no others, so an undeclared call has no route, as with
  OpenChoreo's network policies.
- Dependency addresses: each `envBindings.address` becomes `http://<component>:<port><basePath>`.
- Settings: `configurations.env` values as given; `secretKeyRef` values from the environment's
  secrets; `dependencies.resources` outputs from the environment's resource bindings.
- Capacity: the binding's replicas, CPU request and memory limit (capacity.binding_configs).

Images are pinned by digest, so what was scanned and signed is what runs.
"""

from __future__ import annotations

import re
from collections.abc import Callable

EDGE_NETWORK = "edge"


class WorkloadError(ValueError):
    """The workload can't run as given: an unpinned image, an unknown dependency, a missing secret."""


def short(component: str) -> str:
    return component.rsplit("/", 1)[-1]


def network_of(component: str) -> str:
    return f"to-{short(component)}"


def _cpus(value) -> float | None:
    s = str(value or "").strip()
    if not s:
        return None
    return round(float(s[:-1]) / 1000, 3) if s.endswith("m") else float(s)


def _memory(value) -> str | None:
    s = str(value or "").strip()
    m = re.fullmatch(r"(\d+(?:\.\d+)?)(Mi|Gi|M|G)?", s)
    if not m:
        return None
    unit = {"Mi": "m", "M": "m", "Gi": "g", "G": "g", None: "b"}[m.group(2)]
    return f"{m.group(1)}{unit}"


def endpoints(workload: dict) -> list[dict]:
    return [e for e in (workload.get("endpoints") or []) if isinstance(e, dict) and e.get("port")]


def address(workload: dict, component: str, endpoint: str) -> str:
    """How another component reaches `endpoint` of this one, inside the environment."""
    for e in endpoints(workload):
        if e.get("name") == endpoint:
            base = str(e.get("basePath") or "").rstrip("/")
            return f"http://{short(component)}:{int(e['port'])}{base}"
    raise WorkloadError(f"{short(component)} has no endpoint {endpoint!r}")


def to_service(workload: dict, *, component: str, image: str, env: str,
               peers: dict[str, dict], secrets: Callable[[str, str], str | None],
               resource_outputs: dict[str, dict] | None = None,
               capacity: dict | None = None, release: str = "") -> dict:
    """The Compose service for one component in one environment.

    `peers`: component short name -> its workload, for every component bound in the environment.
    `secrets(name, key)`: the environment's secret value, or None. `resource_outputs`: resource
    name -> {output: value}, for the resources bound in the environment."""
    name = short(component)
    if "@sha256:" not in image:
        raise WorkloadError(f"{name}: the image must be pinned by digest (repo@sha256:...), not {image!r}")
    environment: dict[str, str] = {}
    missing: list[str] = []
    networks: dict[str, dict] = {network_of(name): {"aliases": [name]}}

    for e in endpoints(workload):
        if "external" in (e.get("visibility") or []):
            networks[EDGE_NETWORK] = {"aliases": [name]}

    deps = workload.get("dependencies") or {}
    for d in deps.get("endpoints") or []:
        target = short(str(d.get("component") or ""))
        bindings = d.get("envBindings") or {}
        if target not in peers:
            # Optional in the templates (the BFF lists an agent the product may not have): no route, no address.
            continue
        networks[network_of(target)] = {}
        if bindings.get("address"):
            environment[str(bindings["address"])] = address(peers[target], target, str(d.get("name") or ""))

    outputs = resource_outputs or {}
    for r in deps.get("resources") or []:
        ref = str(r.get("ref") or "")
        for output, var in (r.get("envBindings") or {}).items():
            value = (outputs.get(ref) or {}).get(output)
            if value is None:
                missing.append(f"resource {ref}.{output}")
            else:
                environment[str(var)] = str(value)

    for item in (workload.get("configurations") or {}).get("env") or []:
        var = str(item.get("name") or "")
        if not var:
            continue
        ref = ((item.get("valueFrom") or {}).get("secretKeyRef") or {})
        if ref:
            value = secrets(str(ref.get("name") or ""), str(ref.get("key") or ""))
            if value is None:
                missing.append(f"secret {ref.get('name')}/{ref.get('key')}")
            else:
                environment[var] = value
        else:
            environment[var] = str(item.get("value", ""))

    if missing:
        raise WorkloadError(f"{name} in {env}: no value for " + ", ".join(missing))

    service: dict = {
        "image": image,
        "restart": "unless-stopped",
        "environment": dict(sorted(environment.items())),
        "networks": networks,
        "security_opt": ["no-new-privileges:true"],
        "cap_drop": ["ALL"],
        "labels": {"dev.valxel.component": component, "dev.valxel.env": env, "dev.valxel.release": release,
                   **{f"dev.valxel.port.{e['name']}": str(int(e["port"])) for e in endpoints(workload)
                      if "external" in (e.get("visibility") or [])}},
        "logging": {"driver": "json-file", "options": {"max-size": "10m", "max-file": "5"}},
    }
    cap = capacity or {}
    replicas = int(cap.get("replicas") or 1)
    if replicas > 1:
        service["deploy"] = {"replicas": replicas}
    resources = cap.get("resources") or {}
    cpus = _cpus((resources.get("limits") or {}).get("cpu") or (resources.get("requests") or {}).get("cpu"))
    mem = _memory((resources.get("limits") or {}).get("memory"))
    if cpus:
        service["cpus"] = cpus
    if mem:
        service["mem_limit"] = mem
    return service


def edge_network(project: str) -> str:
    """The Docker network the VM's proxy shares with one environment's external components."""
    return f"edge-{project}"


def compose_file(services: dict[str, dict], *, project: str) -> dict:
    """The environment's whole Compose file (Compose project `project`): its services and the
    networks they use. The edge network is created outside the project, by the proxy's setup."""
    nets: dict[str, dict] = {}
    for svc in services.values():
        for n in svc["networks"]:
            nets[n] = {"external": True, "name": edge_network(project)} if n == EDGE_NETWORK else {}
    return {"services": dict(sorted(services.items())), "networks": dict(sorted(nets.items()))}

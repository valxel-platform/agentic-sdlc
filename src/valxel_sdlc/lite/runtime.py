"""ComposeRuntime: the OpenChoreo interface on Docker Compose (lite mode).

The OpenChoreo model maps onto one or two VMs:

    OpenChoreo                      lite mode
    data plane (cluster)            a VM, reached through the Docker CLI (`hosts`, by data plane name)
    environment                     a Compose project, `<project>-<env>`, on its data plane's VM
    WorkflowRun -> ComponentRelease an OpenChoreo build -> a release: image digest + workload.yaml (`Builds`)
    ReleaseBinding                  the release a component runs in an environment, with its history
    change-set environment          another Compose project on the VM of the environment it copies

Every change renders the environment's whole Compose file from its bindings and applies it, so
what runs is always what the bindings say. A change is saved only once Docker has accepted it.

The rules stay the same as with OpenChoreo: agents never deploy to production or roll it back
(VX-3, `check_promotion`); a component reaches only the components it declares (VX-17, see
lite.workload); images run pinned by digest.

Not yet in lite mode, each refused with a clear message: managed resources (Postgres and Kafka as
containers come with the resources step) and autoscaling (a VM scales by being made bigger).
"""

from __future__ import annotations

import re
import threading
from datetime import UTC, datetime
from typing import Protocol

from ..integrations.openchoreo import PIPELINE, ROLLBACK_ACTORS, check_promotion
from .builds import Builds
from .docker import Docker
from .state import MemoryState, State
from .workload import EDGE_NETWORK, compose_file, edge_network, short, to_service


class LiteUnsupported(RuntimeError):
    """Something OpenChoreo does that lite mode doesn't (yet)."""


class Secrets(Protocol):
    def get(self, *, namespace: str, env: str, name: str, key: str) -> str | None: ...


class DictSecrets:
    """Secrets held in memory: {(env, name, key): value}, or {(name, key): value} for every environment."""

    def __init__(self, values: dict | None = None) -> None:
        self.values = dict(values or {})

    def get(self, *, namespace, env, name, key) -> str | None:
        return self.values.get((env, name, key), self.values.get((name, key)))


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9-]+", "-", text.lower()).strip("-") or "default"


def _now() -> str:
    return datetime.now(UTC).isoformat()


class ComposeRuntime:
    """`hosts`: data plane name -> Docker on that VM. One VM: give only `default`. Production on its
    own VM: also `hosts={"<pid>-prod": DockerCli("ssh://valxel@prod-vm")}`."""

    def __init__(self, *, default: Docker, builds: Builds, hosts: dict[str, Docker] | None = None,
                 secrets: Secrets | None = None, state: State | None = None) -> None:
        self.default = default
        self.hosts = dict(hosts or {})
        self.builds = builds
        self.secrets = secrets or DictSecrets()
        self.state = state or MemoryState()
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ helpers

    def _scope(self, data: dict, component: str, project: str | None, namespace: str | None) -> tuple[str, str]:
        c = data["components"].get(component) or {}
        return (project or c.get("project") or "default", namespace or c.get("namespace") or "default")

    @staticmethod
    def _key(namespace: str, env: str) -> str:
        return f"{namespace}/{env}"

    def _host(self, data: dict, namespace: str, env: str, project: str) -> Docker:
        e = data["environments"].get(self._key(namespace, env)) or {}
        if e.get("like"):
            e = data["environments"].get(self._key(namespace, e["like"])) or {}
        dp = e.get("data_plane") or (f"{project}-prod" if env == PIPELINE[-1] else f"{project}-nonprod")
        return self.hosts.get(dp, self.default)

    def _project_of(self, data: dict, namespace: str, env: str, fallback: str) -> str:
        e = data["environments"].get(self._key(namespace, env)) or {}
        return e.get("project") or fallback

    @staticmethod
    def _compose_project(project: str, env: str) -> str:
        return _slug(f"{project}-{env}")

    @staticmethod
    def _release(data: dict, component: str, name: str) -> dict:
        for r in data["releases"].get(component) or []:
            if r["name"] == name:
                return r
        raise ValueError(f"{component}: no release {name!r}")

    def _apply(self, data: dict, namespace: str, env: str, project: str) -> None:
        """Render the environment's Compose file from its bindings and apply it."""
        bound = data["bindings"].get(self._key(namespace, env)) or {}
        host = self._host(data, namespace, env, project)
        cp = self._compose_project(project, env)
        if not bound:
            host.down(cp)
            return
        releases = {comp: self._release(data, comp, b["release"]) for comp, b in bound.items()}
        peers = {short(comp): r.get("workload") or {} for comp, r in releases.items()}
        services = {}
        for comp, b in sorted(bound.items()):
            r = releases[comp]
            services[short(comp)] = to_service(
                r.get("workload") or {}, component=comp, image=r["image"], env=env, peers=peers,
                secrets=lambda name, key: self.secrets.get(namespace=namespace, env=env, name=name, key=key),
                capacity=b.get("capacity"), release=r["name"])
        spec = compose_file(services, project=cp)
        edge = (edge_network(cp),) if EDGE_NETWORK in spec["networks"] else ()
        host.up(cp, spec, networks=edge)

    def _bind(self, data: dict, namespace: str, env: str, component: str, release: str, actor: str,
              project: str) -> str:
        """Point the component's binding at `release`; returns the release it ran before ("" if none)."""
        envb = data["bindings"].setdefault(self._key(namespace, env), {})
        b = envb.get(component)
        before = (b or {}).get("release", "")
        if b is None:
            envb[component] = {"release": release, "history": [], "actor": actor, "project": project,
                               "bound": _now()}
        elif before != release:
            b["history"] = (b.get("history") or [])[-9:] + [before]
            b.update({"release": release, "actor": actor, "bound": _now()})
        return before

    def _change(self, fn):
        """Load, change, apply, then save: a change Docker refuses is never remembered."""
        with self._lock:
            data = self.state.load()
            out = fn(data)
            self.state.save(data)
            return out

    def _read(self):
        with self._lock:
            return self.state.load()

    # ------------------------------------------------------------------ components and builds

    def register_component(self, *, component, project, namespace, component_type, workflow, repo_url,
                           branch, parameters=None) -> str:
        def fn(data):
            if component in data["components"]:
                return "exists"
            data["components"][component] = {"project": project, "namespace": namespace,
                                             "component_type": component_type, "workflow": workflow,
                                             "repo_url": repo_url, "branch": branch,
                                             "parameters": dict(parameters or {}), "traits": []}
            return "created"
        return self._change(fn)

    def ensure_traits(self, *, component, traits, project=None, namespace=None) -> list[str]:
        """Kept with the component for the VM's Prometheus (alert rules arrive with the monitoring step)."""
        from ..ops.alert_rules import missing

        def fn(data):
            c = data["components"].setdefault(component, {"traits": []})
            have = c.setdefault("traits", [])
            add = missing(have, traits)
            have.extend(add)
            return [t["instanceName"] for t in add]
        return self._change(fn)

    def run_workflow(self, *, component, ref, kind, project=None, namespace=None) -> dict:
        def fn(data):
            c = data["components"].get(component) or {}
            run = self.builds.start(component=component, ref=ref, kind=kind, repo_url=c.get("repo_url", ""),
                                    project=project or c.get("project"), namespace=namespace or c.get("namespace"))
            data["runs"][run["run_id"]] = {"component": component, "ref": ref, "kind": kind, "release": ""}
            return {"run_id": run["run_id"], "status": "running", "url": run.get("url", "")}
        return self._change(fn)

    def run_status(self, *, run_id, component, project=None, namespace=None) -> dict:
        known = self._read()
        c = known["components"].get(component) or {}
        s = self.builds.status(run_id=run_id, component=component,
                               ref=(known["runs"].get(run_id) or {}).get("ref", ""),
                               project=project or c.get("project"), namespace=namespace or c.get("namespace"))

        def fn(data):
            run = data["runs"].setdefault(run_id, {"component": component, "ref": "", "kind": "", "release": ""})
            if s.get("status") == "succeeded" and not run["release"]:
                rel = data["releases"].setdefault(component, [])
                if not s.get("image"):
                    raise ValueError(f"{component}: build {run_id} succeeded without an image digest")
                name = f"{short(component)}-r{len(rel) + 1}"
                rel.append({"name": name, "image": s["image"], "workload": s.get("workload") or {},
                            "ref": run["ref"], "run_id": run_id, "created": _now(),
                            **({"openchoreo_release": s["openchoreo_release"]} if s.get("openchoreo_release") else {})})
                run["release"] = name
            return run["release"]
        release = self._change(fn)
        out = {k: v for k, v in s.items() if k not in ("workload", "image", "openchoreo_release")}
        return {**out, **({"release": release} if release else {})}

    # ------------------------------------------------------------------ the deployment pipeline

    def environment(self, component: str) -> str | None:
        data = self._read()
        reached = [env.split("/", 1)[1] for env, b in data["bindings"].items() if component in b]
        on = [e for e in PIPELINE if e in reached]
        return on[-1] if on else None

    def promote(self, *, component, env, actor, actor_type, project=None, namespace=None,
                pipeline=PIPELINE) -> dict:
        check_promotion(env, actor_type, pipeline)

        def fn(data):
            proj, ns = self._scope(data, component, project, namespace)
            i = pipeline.index(env)
            if i == 0:
                rel = data["releases"].get(component) or []
                if not rel:
                    raise ValueError(f"{component}: nothing built yet to deploy to {env}")
                release = rel[-1]["name"]
            else:
                prev = (data["bindings"].get(self._key(ns, pipeline[i - 1])) or {}).get(component)
                if not prev:
                    raise ValueError(f"{component}: cannot promote to {env!r}; it doesn't run in {pipeline[i - 1]!r}")
                release = prev["release"]
            proj = self._project_of(data, ns, env, proj)
            self._bind(data, ns, env, component, release, actor, proj)
            self._apply(data, ns, env, proj)
            return {"component": component, "env": env, "actor": actor, "actor_type": actor_type,
                    "release": release}
        return self._change(fn)

    def _state(self, data: dict, component: str, ns: str, env: str, project: str) -> str:
        cp = self._compose_project(self._project_of(data, ns, env, project), env)
        rows = [r for r in self._host(data, ns, env, project).ps(cp) if r["service"] == short(component)]
        if not rows:
            return "absent"
        states = {r["state"] for r in rows}
        health = {r["health"] for r in rows}
        if states & {"exited", "dead", "restarting"} or "unhealthy" in health:
            return "failed"
        if states == {"running"} and not (health & {"starting"}):
            return "ready"
        return "progressing"

    def deployment(self, *, component, env, project=None, namespace=None) -> dict:
        data = self._read()
        proj, ns = self._scope(data, component, project, namespace)
        b = (data["bindings"].get(self._key(ns, env)) or {}).get(component)
        if not b:
            return {"state": "absent"}
        return {"state": self._state(data, component, ns, env, proj), "release": b["release"], "url": ""}

    def configure_binding(self, *, component, env, configs, traits, project=None, namespace=None) -> dict:
        """Replicas, CPU and memory from the capacity plan. An autoscaler range is kept but not applied:
        a lite VM doesn't autoscale."""
        def fn(data):
            proj, ns = self._scope(data, component, project, namespace)
            b = (data["bindings"].get(self._key(ns, env)) or {}).get(component)
            if not b:
                raise ValueError(f"{component} doesn't run in {env}")
            b["capacity"], b["traits"] = dict(configs or {}), dict(traits or {})
            self._apply(data, ns, env, self._project_of(data, ns, env, proj))
            return {"component": component, "env": env}
        return self._change(fn)

    def rollback(self, *, component, env, actor, actor_type, project=None, namespace=None) -> dict:
        if actor_type not in ROLLBACK_ACTORS:
            raise PermissionError("Only a person, or the tool under the remediation policy, can roll back production (VX-3)")

        def fn(data):
            proj, ns = self._scope(data, component, project, namespace)
            b = (data["bindings"].get(self._key(ns, env)) or {}).get(component)
            if not b or not b.get("history"):
                raise ValueError(f"{component}: no earlier release to roll back to in {env}")
            current, previous = b["release"], b["history"].pop()
            b.update({"release": previous, "actor": actor, "bound": _now()})
            self._apply(data, ns, env, self._project_of(data, ns, env, proj))
            data["rollbacks"].append({"component": component, "env": env, "from": current, "to": previous,
                                      "actor": actor, "at": _now()})
            return {"component": component, "env": env, "from": current, "to": previous, "actor": actor}
        return self._change(fn)

    def observe(self, *, component, env, project=None, namespace=None, minutes=60) -> dict:
        """What the component is doing: its containers' state and recent logs. Metrics come from the
        VM's Prometheus with the monitoring step."""
        data = self._read()
        proj, ns = self._scope(data, component, project, namespace)
        b = (data["bindings"].get(self._key(ns, env)) or {}).get(component)
        if not b:
            return {"available": False, "logs": [], "metrics": {}, "state": "absent"}
        state = self._state(data, component, ns, env, proj)
        cp = self._compose_project(self._project_of(data, ns, env, proj), env)
        logs = self._host(data, ns, env, proj).logs(cp, short(component), minutes=minutes)
        return {"available": state == "ready", "logs": logs, "metrics": {}, "state": state,
                "release": b["release"]}

    def component_bindings(self, *, component, project=None, namespace=None) -> list[dict]:
        data = self._read()
        proj, ns = self._scope(data, component, project, namespace)
        out = []
        for key, envb in sorted(data["bindings"].items()):
            env_ns, env = key.split("/", 1)
            if env_ns == ns and component in envb:
                out.append({"env": env, "release": envb[component]["release"],
                            "state": self._state(data, component, ns, env, proj),
                            "binding": f"{self._compose_project(self._project_of(data, ns, env, proj), env)}/{short(component)}"})
        return out

    # ------------------------------------------------------------------ the product's environments

    def data_plane(self, *, name, namespace=None) -> dict:
        """A data plane is a VM: ready if it is configured by name, or a default VM takes it."""
        return {"state": "ready" if (name in self.hosts or self.default is not None) else "absent", "name": name,
                "host": "own" if name in self.hosts else "default"}

    def ensure_project(self, *, project, pipeline, namespace=None) -> str:
        def fn(data):
            if project in data["projects"]:
                return "exists"
            data["projects"][project] = {"pipeline": pipeline, "namespace": namespace}
            return "created"
        return self._change(fn)

    def ensure_pipeline(self, *, name, stages, namespace=None) -> str:
        def fn(data):
            if name in data["pipelines"]:
                return "exists"
            data["pipelines"][name] = list(stages)
            return "created"
        return self._change(fn)

    def ensure_environment(self, *, name, data_plane, production, actor, namespace=None, labels=None) -> str:
        if production and not str(actor).startswith("human:"):
            raise PermissionError("Agents cannot create production environments (VX-3)")
        if self.data_plane(name=data_plane)["state"] != "ready":
            raise ValueError(f"{data_plane}: no such data plane")

        def fn(data):
            key = self._key(namespace or "default", name)
            if key in data["environments"]:
                return "exists"
            project = (labels or {}).get("valxel.io/product") or data_plane.rsplit("-", 1)[0]
            data["environments"][key] = {"name": name, "data_plane": data_plane, "production": production,
                                         "actor": actor, "labels": dict(labels or {}), "project": project,
                                         "created": _now()}
            return "created"
        return self._change(fn)

    # Change-set environments: one per open change set, on the VM of the environment it copies.
    def create_environment(self, *, name, like, project=None, namespace=None, labels=None) -> dict:
        def fn(data):
            ns = namespace or "default"
            key = self._key(ns, name)
            if key not in data["environments"]:
                base = data["environments"].get(self._key(ns, like)) or {}
                data["environments"][key] = {"name": name, "like": like, "changeset": True,
                                             "project": project or base.get("project") or "default",
                                             "namespace": namespace, "labels": dict(labels or {}),
                                             "created": _now()}
            e = data["environments"][key]
            return {k: e.get(k) for k in ("name", "like", "project", "namespace", "labels", "created")}
        return self._change(fn)

    def delete_environment(self, *, name, project=None, namespace=None) -> None:
        def fn(data):
            ns = namespace or "default"
            key = self._key(ns, name)
            if key in data["environments"] or key in data["bindings"]:
                proj = self._project_of(data, ns, name, project or "default")
                data["bindings"].pop(key, None)
                self._apply(data, ns, name, proj)  # nothing bound: the Compose project goes
                data["environments"].pop(key, None)
        self._change(fn)

    def list_environments(self, *, namespace=None) -> list[dict]:
        data = self._read()
        return [{k: e.get(k) for k in ("name", "like", "project", "namespace", "labels", "created")}
                for key, e in sorted(data["environments"].items())
                if e.get("changeset") and (namespace is None or key.split("/", 1)[0] == namespace)]

    def bind_latest(self, *, component, env, actor, project=None, namespace=None) -> dict:
        def fn(data):
            proj, ns = self._scope(data, component, project, namespace)
            if self._key(ns, env) not in data["environments"]:
                raise ValueError(f"{env}: no such environment")
            rel = data["releases"].get(component) or []
            if not rel:
                raise ValueError(f"{component}: nothing built yet to deploy to {env}")
            proj = self._project_of(data, ns, env, proj)
            self._bind(data, ns, env, component, rel[-1]["name"], actor, proj)
            self._apply(data, ns, env, proj)
            return {"component": component, "env": env, "release": rel[-1]["name"], "actor": actor,
                    "actor_type": "agent"}
        return self._change(fn)

    def unbind(self, *, component, env, project=None, namespace=None) -> None:
        def fn(data):
            proj, ns = self._scope(data, component, project, namespace)
            envb = data["bindings"].get(self._key(ns, env)) or {}
            if envb.pop(component, None) is not None:
                self._apply(data, ns, env, self._project_of(data, ns, env, proj))
        self._change(fn)

    # ------------------------------------------------------------------ managed resources (VX-19)

    def list_resources(self, *, project, namespace=None) -> list[dict]:
        data = self._read()
        return [{"name": n, "type": r["type"], "parameters": dict(r["parameters"])}
                for n, r in sorted(data["resources"].items()) if r["project"] == project]

    def apply_resource(self, *, name, type, parameters, project, namespace=None) -> dict:
        def fn(data):
            prior = data["resources"].get(name)
            if prior and prior["type"] != type:
                raise ValueError(f"{name} is a {prior['type']}; the pipeline never replaces a resource")
            if prior and prior["parameters"] == parameters:
                return {"name": name, "release": f"{name}-r{prior['releases']}", "change": "unchanged"}
            n = (prior or {}).get("releases", 0) + 1
            data["resources"][name] = {"type": type, "parameters": dict(parameters), "project": project,
                                       "namespace": namespace, "releases": n}
            return {"name": name, "release": f"{name}-r{n}", "change": "updated" if prior else "created"}
        return self._change(fn)

    def bind_resource(self, *, name, env, size, actor, actor_type, project, namespace=None, pipeline=PIPELINE,
                      retain=False) -> dict:
        check_promotion(env, actor_type, pipeline)
        raise LiteUnsupported(f"{name}: lite mode can't run managed services yet; Postgres and Kafka as "
                              "containers come with the resources step. Until then, use full mode for a "
                              "product whose design needs one.")

    def resource_status(self, *, name, env, project, namespace=None) -> dict:
        return {"state": "absent", "message": "lite mode: no managed services yet"}

    def resource_bindings_of(self, *, name, project, namespace=None) -> list[dict]:
        return []

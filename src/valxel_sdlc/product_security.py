"""A product's mesh rules: declared in its own security repo, applied to its own components.

Each product org has a security repo beside its governance repo (`<org>/<prefix>security`, created
at the product gate from the platform's template-security). Its `security.yaml` is the product's
security spec in platform-security's format: its components, its clients and scopes, and `calls`,
who may call whom and on which port. The product's security owners review it there (CODEOWNERS),
and its CI runs platform-security's checker, so a product can't declare what the platform forbids.

At G4 the approved design's `## Endpoints` names what each component calls; the tool opens a pull
request adding the calls `security.yaml` lacks (on the services' port, 8080), never removing one,
so the security owners approve each new path before the code that uses it ships.

The platform defines the rules (agentic-sdlc platform/openchoreo/: the `valxel` project type makes
each environment's namespace mTLS STRICT and deny-by-default; every component type allows only the
callers in its `allowedCallers` parameter, from the same namespace). This module joins the two: when
the security repo's main branch changes, it sets each component's `allowedCallers` and `fromIngress`
from `calls` and the BFF. The change reaches an environment with the component's next release, so
rules travel through dev, staging and production with the code that needs them.

Only the product's own components are touched, and a call naming a component the product doesn't
have is reported, never applied.
"""

from __future__ import annotations

import logging
from typing import Any

import yaml

log = logging.getLogger(__name__)

SECURITY_FILE = "security.yaml"


def template() -> str:
    """The template each product's security repo starts from (read when used, so a deployment can
    point at its own copy)."""
    import os

    return os.environ.get("VALXEL_SECURITY_TEMPLATE", "valxel-platform/template-security")


def security_repo(governance_repo: str) -> str:
    """The product's security repo, beside its governance repo: acme-pay/pay-governance ->
    acme-pay/pay-security."""
    org, name = governance_repo.split("/", 1)
    prefix = name[: -len("governance")] if name.endswith("governance") else f"{name}-"
    return f"{org}/{prefix}security"


def mesh_parameters(spec: dict[str, Any], components: list[str]) -> tuple[dict[str, dict], list[str]]:
    """Each component's `allowedCallers` and `fromIngress`, from the spec's `calls` and its BFF.
    Every one of `components` gets a value (an empty list resets one whose calls were removed).
    Returns the parameters and the problems: calls naming a component the product doesn't have."""
    known = set(components)
    callers: dict[str, dict[str, set[int]]] = {c: {} for c in components}
    problems: list[str] = []
    for call in spec.get("calls") or []:
        src, dst, port = call.get("from"), call.get("to"), call.get("port")
        missing = [n for n in (src, dst) if n not in known]
        if missing or not isinstance(port, int):
            problems.append(f"call {src} -> {dst}:{port}: "
                            + (f"{', '.join(map(str, missing))} isn't one of the product's components"
                               if missing else "port must be a number"))
            continue
        callers[dst].setdefault(src, set()).add(port)
    bff = ((spec.get("bff") or {}).get("client_id")) or ""
    if bff and bff not in known:
        problems.append(f"bff {bff} isn't one of the product's components")
    params = {c: {"allowedCallers": [{"component": src, "ports": sorted(ports)}
                                     for src, ports in sorted(callers[c].items())],
                  "fromIngress": c == bff}
              for c in components}
    return params, problems


SERVICE_PORT = 8080  # every template serves its API here (8081 is the management port, the collector's)


def missing_calls(spec: dict[str, Any], wanted: list[tuple[str, str]]) -> list[dict]:
    """The design's (from, to) calls the spec doesn't allow yet, on the service port."""
    have = {(c.get("from"), c.get("to")) for c in spec.get("calls") or []}
    return [{"from": a, "to": b, "port": SERVICE_PORT} for a, b in dict.fromkeys(wanted) if (a, b) not in have]


def with_calls(text: str, calls: list[dict]) -> str:
    """security.yaml with `calls` appended, its other contents and comments kept as they are."""
    lines = [f"  - {{from: {c['from']}, to: {c['to']}, port: {c['port']}}}" for c in calls]
    body = text.rstrip("\n").split("\n")
    at = next((i for i, line in enumerate(body) if line.startswith("calls:")), None)
    if at is None:
        return "\n".join([*body, "calls:", *lines]) + "\n"
    if body[at].strip() == "calls: []":
        body[at] = "calls:"
    end = at + 1
    while end < len(body) and (body[end].startswith((" ", "-")) or not body[end].strip()):
        end += 1
    while end > at + 1 and not body[end - 1].strip():
        end -= 1
    return "\n".join([*body[:end], *lines, *body[end:]]) + "\n"


def propose(services, product_id: str, feature_id: str, hld: str) -> dict:
    """At G4: a pull request adding the design's new calls to the product's security repo."""
    from .agents.parse import parse_endpoints
    from .repo_policy import component_name

    product = services.snapshot(product_id).product
    by_short = {r.name.split("/", 1)[-1]: component_name(product, r.name) for r in product.repos or []}
    by_short.update({r.name: component_name(product, r.name) for r in product.repos or []})
    wanted = [(by_short[e["repo"]], by_short[c]) for e in parse_endpoints(hld) if e["repo"] in by_short
              for c in e["calls"] if c in by_short]
    repo = security_repo(services.settings.entry(product_id).governance.full_name)
    text = services.github.get_file(repo=repo, path=SECURITY_FILE, ref="")
    if text is None:
        return {"repo": repo, "skipped": True, "why": f"{repo} has no {SECURITY_FILE}"}
    new = missing_calls(yaml.safe_load(text) or {}, wanted)
    if not new:
        return {"repo": repo, "calls": []}
    branch = f"valxel/{feature_id.lower()}-calls"
    services.github.put_file(repo=repo, branch=branch, path=SECURITY_FILE, content=with_calls(text, new),
                             message=f"{feature_id}: the calls its approved design makes")
    listed = "\n".join(f"- {c['from']} -> {c['to']}:{c['port']}" for c in new)
    pr = services.github.open_pr(
        repo=repo, branch=branch, title=f"{feature_id}: allow the calls its design makes",
        files=[SECURITY_FILE], labels=["valxel", "security"],
        body=(f"{feature_id}'s design was approved at G4. It has these components call each other, which "
              f"the mesh denies until they're listed here:\n\n{listed}\n\nCheck each one, and the port. "
              "Merging sets the components' allowed callers in OpenChoreo; each environment gets them with the "
              "component's next release."))
    return {"repo": repo, "calls": new, "url": pr["url"]}


def apply(services, product_id: str) -> dict:
    """Reads the product's security spec and sets every component's mesh parameters."""
    from .repo_policy import component_name

    entry = services.settings.entry(product_id)
    repo = security_repo(entry.governance.full_name)
    text = services.github.get_file(repo=repo, path=SECURITY_FILE, ref="") or ""
    if not text:
        return {"product": product_id, "repo": repo, "applied": {}, "problems": [f"{repo} has no {SECURITY_FILE}"]}
    spec = yaml.safe_load(text) or {}
    product = services.snapshot(product_id).product
    rt = product.runtime or {}
    components = sorted({component_name(product, r.name) for r in product.repos or []})
    params, problems = mesh_parameters(spec, components)
    applied: dict[str, str] = {}
    for component, values in params.items():
        try:
            applied[component] = services.openchoreo.set_parameters(
                component=component, parameters=values, project=rt.get("openchoreo_project"),
                namespace=rt.get("namespace"))
        except Exception as exc:  # noqa: BLE001 - not registered yet: it gets its rules when it is
            applied[component] = f"skipped: {type(exc).__name__}: {exc}"
            log.info("mesh rules: %s/%s not applied: %s", product_id, component, exc)
    return {"product": product_id, "repo": repo, "applied": applied, "problems": problems}


def on_push(services, kind: str, event: dict) -> dict | None:
    """A GitHub webhook event: a push to main of a product's security repo applies its rules."""
    if kind != "push" or event.get("ref") != "refs/heads/main":
        return None
    pid = product_for_repo(services, (event.get("repository") or {}).get("full_name", ""))
    return apply(services, pid) if pid else None


def product_for_repo(services, repo: str) -> str:
    """The product whose security repo `repo` is, or ""."""
    for pid, entry in services.settings.registry.items():
        if entry.kind == "product" and security_repo(entry.governance.full_name) == repo:
            return pid
    return ""

"""Validate a product's product.yaml against the registry and platform limits.

Products own their lane (repos, approvers, budget, thresholds), but may only tighten what the
platform sets: a product cannot lower the critic threshold, raise the loop cap or budget, drop a
gate's approvers, change its data class, or list repos outside its own organization.
"""

from __future__ import annotations

import re

import yaml

from ..config import Limits, ProductConfig, RegistryEntry, RepoSpec
from .errors import GovernanceError

_ROLES = {"provider", "consumer", "library"}
_REPO = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9._-]+$")


def parse_manifest(text: str, entry: RegistryEntry, limits: Limits,
                   registry: dict[str, RegistryEntry] | None = None) -> tuple[ProductConfig, dict]:
    try:
        raw = yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        raise GovernanceError(f"{entry.governance.full_name}: product.yaml is not valid YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise GovernanceError(f"{entry.governance.full_name}: product.yaml must be a mapping")

    org = entry.governance.org
    problems: list[str] = []

    if raw.get("product_id") != entry.product_id:
        problems.append(f"product_id is {raw.get('product_id')!r}; the registry expects {entry.product_id!r}")
    if "data_class" in raw and raw["data_class"] != entry.data_class:
        problems.append(f"data_class {raw['data_class']!r} differs from the registry's {entry.data_class!r}; "
                        "data classes are set by the platform")

    def num(key, cast, default):
        try:
            return cast(raw.get(key, default))
        except (TypeError, ValueError):
            problems.append(f"{key} must be a number")
            return cast(default)

    threshold = num("critic_threshold", float, limits.min_critic_threshold)
    max_loops = num("max_loops", int, 3)
    budget = num("budget_usd", float, 150)
    if threshold < limits.min_critic_threshold or threshold > 1:
        problems.append(f"critic_threshold {threshold} must be between {limits.min_critic_threshold} and 1")
    if not 1 <= max_loops <= limits.max_loops:
        problems.append(f"max_loops {max_loops} must be between 1 and {limits.max_loops}")
    if not 0 < budget <= limits.max_budget_usd:
        problems.append(f"budget_usd {budget} must be above 0 and at most {limits.max_budget_usd}")

    repos = []
    for r in raw.get("repos") or []:
        name, role = (r or {}).get("name", ""), (r or {}).get("role", "")
        if not _REPO.match(str(name)):
            problems.append(f"repo {name!r} must be written as <org>/<repo>")
        elif name.split("/", 1)[0] != org:
            problems.append(f"repo {name} is outside the product's organization {org}")
        if role not in _ROLES:
            problems.append(f"repo {name} has role {role!r}; expected one of {sorted(_ROLES)}")
        repos.append(RepoSpec(name=str(name), role=str(role)))
    if not repos:
        problems.append("no repos listed")

    approvers = {k: [str(a) for a in (v or [])] for k, v in (raw.get("approvers") or {}).items()}
    for gate in limits.required_gates:
        if not approvers.get(gate):
            problems.append(f"gate {gate} has no approvers")

    # Runtime infrastructure stays with products; shared lanes ship packages that products run.
    runtime = raw.get("runtime")
    if entry.kind == "shared":
        if runtime:
            problems.append("a shared lane owns no runtime; consuming products run shared services in their own runtime")
        runtime = None
    elif not isinstance(runtime, dict) or not runtime.get("openchoreo_project"):
        problems.append("runtime.openchoreo_project is required: each product owns its runtime")
    else:
        runtime = {"openchoreo_project": str(runtime["openchoreo_project"]),
                   "pipeline": [str(e) for e in runtime.get("pipeline") or ["dev", "staging", "production"]]}

    registry = registry or {}
    depends_on = [str(d) for d in raw.get("depends_on") or []]
    for dep in depends_on:
        if dep == entry.product_id:
            problems.append("a lane cannot depend on itself")
        elif dep not in registry:
            problems.append(f"depends_on {dep!r} is not a registered lane")
        elif entry.kind == "shared" and registry[dep].kind != "shared":
            problems.append(f"shared lane may not depend on product {dep}")

    if problems:
        raise GovernanceError(f"{entry.governance.full_name}/product.yaml is invalid: " + "; ".join(problems))

    product = ProductConfig(
        product_id=entry.product_id,
        name=str(raw.get("name") or entry.product_id),
        org=org,
        data_class=entry.data_class,
        workspace=entry.workspace,
        budget_usd=budget,
        critic_threshold=threshold,
        max_loops=max_loops,
        repos=tuple(repos),
        approvers=approvers,
        high_risk_paths=tuple(str(p) for p in raw.get("high_risk_paths") or []),
        kind=entry.kind,
        runtime=runtime,
        depends_on=tuple(dict.fromkeys(depends_on)),
    )
    files = {"constitution": str(raw.get("constitution") or "constitution.md"),
             "context": str(raw.get("context") or "context.md")}
    return product, files

"""The platform's repo standards as rules: naming, the proposal check, and the plan of what a new
repo gets (config/repos.yaml, a product's repo_standards), and every managed repo with its plan
(`targets`, which reads the lanes' governance through the services it's handed). Nothing here acts
on GitHub.

Governance, the evaluators and delivery all read these, so they live in the core (ADR 0005); the
provisioner that applies a plan through GitHub and OpenChoreo stays with delivery (provisioning.py).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .repo_roles import ROLES

PERMISSIONS = ("pull", "triage", "push", "maintain", "admin")


@dataclass(frozen=True)
class RepoStandards:
    naming: dict = field(default_factory=dict)
    defaults: dict = field(default_factory=dict)
    roles: dict = field(default_factory=dict)
    floors: dict = field(default_factory=dict)
    platform_repos: dict = field(default_factory=dict)  # repo -> {kind, role?, product?, required_checks?}


def load_standards(path: Path) -> RepoStandards:
    if not path.exists():
        return RepoStandards()
    raw = yaml.safe_load(path.read_text()) or {}
    return RepoStandards(naming=dict(raw.get("naming") or {}), defaults=dict(raw.get("defaults") or {}),
                         roles=dict(raw.get("roles") or {}), floors=dict(raw.get("floors") or {}),
                         platform_repos=dict(raw.get("platform_repos") or {}))


def effective_standards(std: RepoStandards, data_class: str, overrides: dict | None,
                        problems: list[str] | None = None) -> dict:
    """Platform defaults, the data-class floor, then the product's tightening. Products may set
    their own templates and add required checks; they may not lower required approvals or turn
    off a security feature."""
    problems = problems if problems is not None else []
    out = {"defaults": yaml.safe_load(yaml.safe_dump(std.defaults)) or {},
           "roles": yaml.safe_load(yaml.safe_dump(std.roles)) or {}}
    rs = out["defaults"].setdefault("ruleset", {})
    floor = std.floors.get(data_class, {})
    if floor.get("required_approvals"):
        rs["required_approvals"] = max(int(rs.get("required_approvals", 0)), int(floor["required_approvals"]))
    o = overrides or {}
    unknown = set(o) - {"templates", "required_approvals", "extra_required_checks", "team_access", "security"}
    if unknown:
        problems.append(f"repo_standards has unknown keys {sorted(unknown)}")
    if "required_approvals" in o:
        if int(o["required_approvals"]) < int(rs.get("required_approvals", 0)):
            problems.append(f"repo_standards.required_approvals {o['required_approvals']} is below the platform's "
                            f"{rs.get('required_approvals')}")
        else:
            rs["required_approvals"] = int(o["required_approvals"])
    for role, template in (o.get("templates") or {}).items():
        if role not in ROLES:
            problems.append(f"repo_standards.templates.{role} is not a repo role")
        else:
            out["roles"].setdefault(role, {})["template"] = str(template)
    for check in o.get("extra_required_checks") or []:
        for role in out["roles"].values():
            role["required_checks"] = list(dict.fromkeys([*role.get("required_checks", []), str(check)]))
    for key, value in (o.get("security") or {}).items():
        if out["defaults"].get("security", {}).get(key) and not value:
            problems.append(f"repo_standards.security.{key} cannot be turned off")
        else:
            out["defaults"].setdefault("security", {})[key] = bool(value)
    for team_gate, perm in (o.get("team_access") or {}).items():
        if perm not in PERMISSIONS:
            problems.append(f"repo_standards.team_access.{team_gate} must be one of {list(PERMISSIONS)}")
        else:
            out["defaults"].setdefault("team_access", {})[team_gate] = perm
    return out


def prefix_of(product) -> str:
    if getattr(product, "repo_prefix", ""):
        return product.repo_prefix
    names = [r.name.split("/", 1)[1] for r in product.repos]
    return names[0].split("-", 1)[0] + "-" if names else ""


def naming_policy(std: RepoStandards, product) -> dict:
    """What the evaluator needs to check proposals, as plain data (it travels in phase state)."""
    pattern = std.naming.get("pattern") or r"^[a-z][a-z0-9-]+$"
    return {"org": product.org, "prefix": prefix_of(product), "pattern": pattern,
            "max_length": int(std.naming.get("max_length") or 100), "roles": sorted(std.roles)}


def check_proposal(policy: dict, proposals: list[dict], existing: set[str]) -> list[str]:
    """Evaluator findings for the HLD's new repos."""
    out = []
    pattern = re.compile(policy["pattern"])
    max_len = policy["max_length"]
    prefix = policy["prefix"]
    seen = set()
    for p in proposals:
        org, _, name = p["name"].partition("/")
        if org != policy["org"]:
            out.append(f"New repo {p['name']} must be created in {policy['org']}, the product's own organization")
        if p["name"] in existing:
            out.append(f"New repo {p['name']} already exists; list it under Affected repos instead")
        if p["name"] in seen:
            out.append(f"New repo {p['name']} is listed twice")
        seen.add(p["name"])
        if not pattern.match(name) or len(name) > max_len:
            out.append(f"New repo name {name!r} must match {pattern.pattern} and be at most {max_len} characters")
        if prefix and not name.startswith(prefix):
            out.append(f"New repo name {name!r} must start with the product prefix {prefix!r}")
        if p["role"] not in policy["roles"]:
            out.append(f"New repo {p['name']} has role {p['role']!r}; the platform has templates for {policy['roles']}")
        if not p.get("adr"):
            out.append(f"New repo {p['name']} needs an ADR that says why an existing repo will not do")
        if not p.get("purpose"):
            out.append(f"New repo {p['name']} needs a one-line purpose")
    return out


def _fmt(value, ctx: dict):
    if isinstance(value, str):
        return value.format(**ctx)
    if isinstance(value, list):
        return [_fmt(v, ctx) for v in value]
    if isinstance(value, dict):
        return {k: _fmt(v, ctx) for k, v in value.items()}
    return value


def plan(product, proposal: dict, standards: dict) -> dict:
    """Everything the provisioner will apply to one repo, resolved to concrete values."""
    d, role = standards["defaults"], standards["roles"].get(proposal["role"], {})
    ctx = {"product": product.product_id.lower(), "role": proposal["role"], "data_class": product.data_class}
    teams: dict[str, str] = {}
    for gate, perm in (d.get("team_access") or {}).items():
        for approver in product.approvers.get(gate, []):
            if "/" in approver:  # teams only; individual logins get access through their teams
                slug = approver.split("/", 1)[1]
                if PERMISSIONS.index(perm) > PERMISSIONS.index(teams.get(slug, "pull")) or slug not in teams:
                    teams[slug] = perm
    codeowners = ["# Generated by the Valxel SDLC from the approvers in the product's governance repo."]
    leads = [a for a in product.approvers.get("g5", []) if "/" in a]
    sec = [a for a in product.approvers.get("security", []) if "/" in a]
    if leads:
        codeowners.append("* " + " ".join(f"@{a}" for a in leads))
    for path in product.high_risk_paths:
        owners = sec or leads
        if owners:
            codeowners.append(f"/{path.strip('/')}/ " + " ".join(f"@{a}" for a in owners))
    return {
        "repo": proposal["name"], "role": proposal["role"], "purpose": proposal.get("purpose", ""),
        "template": role.get("template"), "visibility": d.get("visibility", "public"),
        "default_branch": d.get("default_branch", "main"), "merge": dict(d.get("merge") or {}),
        "features": dict(d.get("features") or {}), "is_template": None,
        "tag_ruleset": dict(d.get("tag_ruleset") or {}), "files": list(d.get("files") or []),
        "managed_files": dict(d.get("managed_files") or {}),
        "security": dict(d.get("security") or {}),
        "ruleset": {**(d.get("ruleset") or {}), "required_checks": list(role.get("required_checks") or [])},
        "teams": teams, "codeowners": "\n".join(codeowners) + "\n",
        "topics": [t.lower() for t in _fmt(list(d.get("topics") or []), ctx)],
        "properties": _fmt(dict(d.get("properties") or {}), ctx),
        "openchoreo": role.get("openchoreo"),
    }


def component_name(product, repo: str) -> str:
    """The repo's OpenChoreo component name: runtime.components, else the repo's short name."""
    named = ((product.runtime or {}).get("components") or {}).get(repo)
    return str(named or repo.split("/", 1)[-1])


# --------------------------------------------------------------------------- the managed repos
# Every repo the standards cover and what each should look like: read by delivery (drift) and by
# ops (upkeep finds a dependency's repos with it).


@dataclass
class Target:
    repo: str
    kind: str
    product: str  # lane id, or "platform"
    plan: dict  # what the standards ask for (valxel_sdlc.provisioning.plan, plus kind-specific settings)
    error: str = ""  # why the lane's own repos couldn't be listed (its governance couldn't be read)


class _Lane:
    """Just enough of a ProductConfig for plan() on repos outside a lane."""

    def __init__(self, product_id: str, data_class: str, approvers: dict | None = None, high_risk_paths=()) -> None:
        self.product_id, self.data_class = product_id, data_class
        self.approvers, self.high_risk_paths = approvers or {}, tuple(high_risk_paths)


def targets(services, only: list[str] | None = None) -> list[Target]:
    """Every managed repo with its plan. Lanes whose governance can't be read are skipped (the
    governance loader's error is the report for them)."""
    from .repo_files import contracts_for

    std = services.settings.repos
    out: dict[str, Target] = {}
    for pid, entry in services.settings.registry.items():
        gov = entry.governance.full_name
        lane, error = None, ""
        try:
            lane = services.snapshot(pid).product
        except Exception as exc:  # an unreadable lane still has a governance repo to keep on standards
            error = f"couldn't read {pid}'s governance, so its repos weren't checked: {exc}"
        out[gov] = Target(gov, "governance", pid, _governance_plan(std, gov, pid, entry.data_class,
                                                                  lane.approvers if lane else {}), error)
        if lane is None:
            continue
        standards = lane.repo_standards or effective_standards(std, lane.data_class, {})
        contracts = contracts_for(std, gov if getattr(entry, "kind", "product") == "product" else None)
        for r in lane.repos:
            p = plan(lane, {"name": r.name, "role": r.role, "purpose": ""}, standards)
            p["managed_files"] = {**p.get("managed_files", {}), **contracts}
            out[r.name] = Target(r.name, "lane", pid, p)
    for repo, spec in (std.platform_repos or {}).items():
        p = _platform_plan(std, repo, spec)
        if spec.get("kind", "tool") != "governance":
            p["managed_files"] = {**p.get("managed_files", {}), **contracts_for(std, None)}
        out[repo] = Target(repo, spec.get("kind", "tool"), spec.get("product", "platform"), p)
    # The platform's GitHub App merges for the merge train once the pipeline's gates pass, so it
    # may bypass the review rules (pull requests only) wherever the pipeline merges: lane repos.
    import os

    app_id = os.environ.get("VALXEL_GITHUB_APP_ID", "")
    for t in out.values():
        if app_id and t.kind == "lane":
            t.plan["ruleset"]["bypass_app_id"] = app_id
    wanted = set(only or [])
    return [t for name, t in sorted(out.items()) if not wanted or name in wanted]


def _base(std, repo: str, product: str, data_class: str, approvers: dict, role: str) -> dict:
    lane = _Lane(product, data_class, approvers)
    return plan(lane, {"name": repo, "role": role, "purpose": ""}, effective_standards(std, data_class, {}))


def _governance_plan(std, repo: str, pid: str, data_class: str, approvers: dict) -> dict:
    p = _base(std, repo, pid, data_class, approvers, "governance")
    p["ruleset"]["required_checks"] = []
    p["ruleset"]["gates_check"] = None  # the pipeline doesn't deliver here
    p["ruleset"]["code_scanning_merge_protection"] = None
    p["security"]["code_scanning_default_setup"] = False  # no code to scan
    p["tag_ruleset"] = {}
    p["files"] = [f for f in p["files"] if f != ".github/dependabot.yml"]
    p["codeowners"] = None  # owned by the lane's people, not generated
    # No code: the secrets scan, and the check of the product's data contracts (datasets/).
    p["managed_files"] = {".github/workflows/secrets.yml": "governance-secrets.yml",
                          ".github/workflows/datasets.yml": "governance-datasets.yml",
                          ".github/scripts/datasets.py": "datasets.py",
                          ".github/scripts/odcs-json-schema-v3.2.0.json": "odcs-json-schema-v3.2.0.json"}
    return p


def _platform_plan(std, repo: str, spec: dict) -> dict:
    kind = spec.get("kind", "tool")
    if kind == "governance":
        return _governance_plan(std, repo, spec.get("product", "platform"), "general", {})
    role = spec.get("role") or "tool"
    p = _base(std, repo, spec.get("product", "platform"), spec.get("data_class", "general"), {}, role)
    checks = spec.get("required_checks") or (std.roles.get(role) or {}).get("required_checks") or []
    p["ruleset"]["required_checks"] = list(checks)
    p["ruleset"]["gates_check"] = None  # the pipeline doesn't deliver here
    p["is_template"] = kind == "template"
    p["codeowners"] = None
    if spec.get("visibility"):
        p["visibility"] = spec["visibility"]
    return p


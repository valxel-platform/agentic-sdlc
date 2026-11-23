"""The platform's repo standards as rules: naming, the proposal check, and the plan of what a new
repo gets (config/repos.yaml, a product's repo_standards). Plain data in, plain data out; nothing
here acts on GitHub.

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

"""New repositories on the golden path.

The architect proposes a repo in the HLD's `## New repos` section, with an ADR saying why an
existing repo will not do. The evaluator checks the proposal against config/repos.yaml; G4
approves it with the rest of the architecture. After G3 and G4, the provision step creates each
repo in the product's org and applies the standards, so the rest of the pipeline works on it:

  repo from the role's template -> merge settings -> security features (dependency graph,
  Dependabot, secret scanning with push protection, CodeQL default setup) -> CODEOWNERS ->
  team access from the gate approvers -> topics and custom properties -> branch ruleset with
  required reviews and checks -> OpenChoreo component -> a governance PR registering the repo.

Every step is safe to repeat. Until the governance PR merges, the feature carries the new repos
itself (Services.feature_snapshot), so its change set, risk tier and delivery include them.
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
    return {"org": product.org, "prefix": prefix_of(product), "pattern": std.naming.get("pattern") or r"^[a-z][a-z0-9-]+$",
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
    teams = {}
    for gate, perm in (d.get("team_access") or {}).items():
        for approver in product.approvers.get(gate, []):
            if "/" in approver:  # teams only; individual logins get access through their teams
                slug = approver.split("/", 1)[1]
                if PERMISSIONS.index(perm) > PERMISSIONS.index(teams.get(slug, "pull")) or slug not in teams:
                    teams[slug] = perm
    codeowners = ["# Generated by the Vrixel agentic SDLC from the approvers in the product's governance repo."]
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
        "security": dict(d.get("security") or {}), "ruleset": {**(d.get("ruleset") or {}),
                                                              "required_checks": list(role.get("required_checks") or [])},
        "teams": teams, "codeowners": "\n".join(codeowners) + "\n",
        "topics": [t.lower() for t in _fmt(list(d.get("topics") or []), ctx)],
        "properties": _fmt(dict(d.get("properties") or {}), ctx),
        "openchoreo": role.get("openchoreo"),
    }


def add_repo_to_manifest(text: str, repo: str, role: str) -> str:
    """Append a repo to product.yaml's `repos:` list, keeping the rest of the file as written."""
    if f"name: {repo}" in text:
        return text
    lines = text.splitlines()
    try:
        start = next(i for i, ln in enumerate(lines) if re.match(r"^repos:\s*$", ln))
    except StopIteration:
        return text.rstrip() + f"\nrepos:\n- name: {repo}\n  role: {role}\n"
    end = start + 1
    while end < len(lines) and (lines[end].startswith(("-", " ")) or not lines[end].strip()):
        end += 1
    while end > start + 1 and not lines[end - 1].strip():
        end -= 1
    indent = re.match(r"^(\s*)-", lines[start + 1]).group(1) if start + 1 < len(lines) and \
        re.match(r"^(\s*)-", lines[start + 1]) else ""
    lines[end:end] = [f"{indent}- name: {repo}", f"{indent}  role: {role}"]
    return "\n".join(lines) + "\n"


class RepoProvisioner:
    """Applies a plan through the GitHub and OpenChoreo adapters. Returns per-step results; a
    failed step stops that repo, and a rerun picks up where it stopped."""

    def __init__(self, github, openchoreo) -> None:
        self.github = github
        self.openchoreo = openchoreo

    def provision(self, product, p: dict, *, feature_id: str, governance_repo: str, manifest_text: str) -> dict:
        steps: list[dict] = []
        repo, org = p["repo"], p["repo"].split("/", 1)[0]

        def step(name: str, fn):
            try:
                res = fn()
                steps.append({"step": name, "ok": True, **({"detail": res} if isinstance(res, str) else {})})
                return True
            except Exception as exc:
                steps.append({"step": name, "ok": False, "error": f"{type(exc).__name__}: {exc}"})
                return False

        gh = self.github
        ok = step("create", lambda: gh.create_repo(repo=repo, template=p["template"], private=p["visibility"] != "public",
                                                   description=p["purpose"]))
        ok = ok and step("settings", lambda: gh.update_repo(repo=repo, merge=p["merge"], default_branch=p["default_branch"],
                                                            description=p["purpose"], features=p.get("features")))
        ok = ok and step("security", lambda: gh.enable_security(repo=repo, **p["security"]))
        ok = ok and step("codeowners", lambda: gh.put_file(repo=repo, branch=p["default_branch"], path=".github/CODEOWNERS",
                                                           content=p["codeowners"],
                                                           message=f"{feature_id}: CODEOWNERS from governance"))
        ok = ok and step("teams", lambda: [gh.grant_team(org=org, team=t, repo=repo, permission=perm)
                                           for t, perm in sorted(p["teams"].items())])
        ok = ok and step("labels", lambda: (gh.set_topics(repo=repo, topics=p["topics"]),
                                            gh.ensure_org_properties(org=org, names=sorted(p["properties"])),
                                            gh.set_properties(repo=repo, properties=p["properties"])))
        ok = ok and step("ruleset", lambda: gh.upsert_ruleset(repo=repo, branch=p["default_branch"], spec=p["ruleset"]))
        if ok and p.get("tag_ruleset"):
            ok = step("tags", lambda: gh.upsert_ruleset(repo=repo, branch=p["default_branch"], spec=p["tag_ruleset"],
                                                        target="tag"))
        oc = p.get("openchoreo")
        if ok and oc and product.runtime:
            rt = product.runtime
            ok = step("openchoreo", lambda: self.openchoreo.register_component(
                component=(rt.get("components") or {}).get(repo, repo), project=rt.get("openchoreo_project"),
                namespace=rt.get("namespace"), component_type=oc["component_type"], workflow=oc.get("workflow"),
                repo_url=f"https://github.com/{repo}", branch=p["default_branch"]))
        pr = None
        if ok:
            def governance_pr():
                nonlocal pr
                path = "product.yaml"
                branch = f"vrixel/{feature_id}-repos"
                staged = gh.get_file(repo=governance_repo, path=path, ref=branch) or manifest_text
                updated = add_repo_to_manifest(staged, repo, p["role"])
                if updated != staged:
                    gh.put_file(repo=governance_repo, branch=branch, path=path, content=updated,
                                message=f"{feature_id}: register {repo}")
                pr = gh.open_pr(repo=governance_repo, branch=branch, title=f"{feature_id}: register new repositories",
                                body=f"Created by the Vrixel agentic SDLC after G4 approved {feature_id}.\n\n"
                                     f"- {repo} ({p['role']}): {p['purpose']}",
                                files=[path], labels=["vrixel", "governance"])
                return pr["url"]
            ok = step("governance", governance_pr)
        return {"repo": repo, "role": p["role"], "purpose": p["purpose"], "ok": ok, "steps": steps,
                "url": f"https://github.com/{repo}", "governance_pr": pr["url"] if pr else None,
                "required_checks": p["ruleset"]["required_checks"],
                "required_approvals": p["ruleset"].get("required_approvals"),
                "status": "provisioned" if ok else "failed"}

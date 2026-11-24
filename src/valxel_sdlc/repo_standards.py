"""Keep every repo on the standards (config/repos.yaml), not only the ones the pipeline creates.

`valxel repos check` compares each managed repo with what the standards ask for and reports the
drift; `valxel repos apply` changes what differs. Managed repos are:

- lane repos, from each lane's governance manifest (product and shared-services repos)
- each lane's governance repo, from registry.yaml
- the platform's own repos (`platform_repos` in repos.yaml): the tool, its governance, templates

Per repo the standards cover merge settings (squash only, branches deleted on merge, auto-merge),
features (no wiki or projects), security features, the default-branch ruleset (reviews, code
owners, required checks, linear history, no force pushes or deletion), a release-tag ruleset,
team access, topics, custom properties and the standard files.

Some things are reported but never changed automatically: a repo that is public when it should be
private, missing files (they arrive with a pull request), and security features that need GitHub
Advanced Security on a private repo. Without Code Security, CodeQL isn't required as a check and
there's no code-scanning merge rule, since a required check that never runs blocks every merge.

Every apply is safe to repeat: it changes only what differs, and rulesets are updated in place.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

from .integrations.github import GitHubError, repo_settings_body, ruleset_body
from .repo_files import (  # noqa: F401  (re-exported for delivery's readers)
    REMOTE,
    contracts_for,
    managed_content,
    remote_source,
    standard_file,
    standard_file_path,
)
from .repo_policy import effective_standards, plan

KINDS = ("lane", "governance", "template", "tool")


@dataclass
class Target:
    repo: str
    kind: str
    product: str  # lane id, or "platform"
    plan: dict  # what the standards ask for (valxel_sdlc.provisioning.plan, plus kind-specific settings)
    error: str = ""  # why the lane's own repos couldn't be listed (its governance couldn't be read)


@dataclass
class Drift:
    area: str  # settings | security | ruleset | tags | teams | topics | properties | files | workflows | visibility | repo | copies
    item: str
    want: object
    have: object
    fix: str = "apply"  # apply | manual | unavailable | pending (a pull request will fix it)
    needs: str = ""  # for unavailable: the GitHub plan or product it needs

    def line(self) -> str:
        how = {"manual": " (fix by hand)", "unavailable": f" (needs {self.needs or 'a paid GitHub feature'})",
               "pending": " (pull request open)"}.get(self.fix, "")
        return f"{self.area}.{self.item}: want {self.want!r}, have {self.have!r}{how}"


@dataclass
class Report:
    repo: str
    kind: str
    exists: bool = True
    drift: list[Drift] = field(default_factory=list)
    applied: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        # A template missing a capability needs code, not an apply, but still fails the check.
        return (self.exists and not [d for d in self.drift if d.fix == "apply" or d.area in ("capabilities", "copies")]
                and not self.errors)

    def as_dict(self) -> dict:
        return {"repo": self.repo, "kind": self.kind, "exists": self.exists, "ok": self.ok,
                "drift": [d.__dict__ for d in self.drift], "applied": self.applied, "errors": self.errors}


class _Lane:
    """Just enough of a ProductConfig for plan() on repos outside a lane."""

    def __init__(self, product_id: str, data_class: str, approvers: dict | None = None, high_risk_paths=()) -> None:
        self.product_id, self.data_class = product_id, data_class
        self.approvers, self.high_risk_paths = approvers or {}, tuple(high_risk_paths)


def targets(services, only: list[str] | None = None) -> list[Target]:
    """Every managed repo with its plan. Lanes whose governance can't be read are skipped (the
    governance loader's error is the report for them)."""
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


# -- comparing ---------------------------------------------------------------------------


def desired_ruleset(p: dict, state: dict) -> dict:
    """The branch ruleset for this repo, without what its plan can't have (CodeQL without Code Security)."""
    spec = dict(p["ruleset"])
    if not state["available"].get("code_scanning"):
        spec["required_checks"] = [c for c in spec.get("required_checks") or [] if c != "CodeQL"]
        spec["code_scanning_merge_protection"] = None
    return spec


def _rules_differ(want: dict, have: dict | None) -> list[str]:
    """Names of what differs between a desired ruleset body and GitHub's view of the ruleset."""
    if not have:
        return ["missing"]
    out = []
    if have.get("enforcement") != "active":
        out.append("enforcement")
    if sorted((have.get("conditions") or {}).get("ref_name", {}).get("include") or []) != \
            sorted(want["conditions"]["ref_name"]["include"]):
        out.append("branches")
    have_rules = {r["type"]: r.get("parameters") or {} for r in have.get("rules") or []}
    want_rules = {r["type"]: r.get("parameters") or {} for r in want["rules"]}
    for t in sorted(set(want_rules) ^ set(have_rules)):
        out.append(f"rule {t}")
    for t, params in want_rules.items():
        if t not in have_rules:
            continue
        hp = have_rules[t]
        for k, v in params.items():
            if k == "required_status_checks":
                pair = lambda c: (c.get("context"), int(c.get("integration_id") or 0))  # noqa: E731
                if sorted(map(pair, v)) != sorted(map(pair, hp.get(k) or [])):
                    out.append(f"{t}.{k}")
            elif k == "code_scanning_tools":
                if [(x["tool"], x["security_alerts_threshold"]) for x in v] != \
                        [(x.get("tool"), x.get("security_alerts_threshold")) for x in hp.get(k) or []]:
                    out.append(f"{t}.{k}")
            elif hp.get(k) != v:
                out.append(f"{t}.{k}")
    key = lambda a: (a.get("actor_type"), int(a.get("actor_id") or 0), a.get("bypass_mode"))  # noqa: E731
    if sorted(map(key, want["bypass_actors"])) != sorted(map(key, have.get("bypass_actors") or [])):
        out.append("bypass")
    return out


def compare(t: Target, state: dict | None) -> list[Drift]:
    p = t.plan
    if state is None:
        return [Drift("repo", "exists", True, False, "manual")]
    if state.get("archived"):
        return []
    out: list[Drift] = []
    if state["visibility"] != p["visibility"]:
        out.append(Drift("visibility", "visibility", p["visibility"], state["visibility"], "manual"))
    want = repo_settings_body(p["merge"], p["default_branch"], None, p.get("features"), p.get("is_template"))
    free_private = state["available"].get("rulesets") is False  # GitHub Free, private repo
    for k, v in want.items():
        if state["settings"].get(k) != v:
            if k == "allow_auto_merge" and free_private:
                out.append(Drift("settings", k, v, state["settings"].get(k), "unavailable",
                                 "GitHub Team, or a public repo: GitHub Free has no auto-merge on private repos"))
            else:
                out.append(Drift("settings", k, v, state["settings"].get(k)))
    avail = state["available"]
    for k, v in p["security"].items():
        if not v or state["security"].get(k):
            continue
        needs = {"code_scanning_default_setup": "code_scanning", "secret_scanning": "secret_scanning",
                 "push_protection": "secret_scanning"}.get(k)
        if k == "dependency_graph":
            # No API turns it on: dependency review and submission fail until someone does.
            out.append(Drift("security", k, True, False, "manual",
                             "Settings > Advanced Security > Dependency graph: Enable (or enable it for all "
                             "repositories in the organization's settings)"))
        elif k == "private_vulnerability_reporting" and state["visibility"] != "public":
            out.append(Drift("security", k, True, False, "unavailable", "a public repo"))
        elif needs and not avail.get(needs):
            out.append(Drift("security", k, True, False, "unavailable",
                             "GitHub Advanced Security (Code Security)" if needs == "code_scanning" else
                             "GitHub Advanced Security (Secret Protection)"))
        else:
            out.append(Drift("security", k, True, False))
    rulesets = [("ruleset", ruleset_body(desired_ruleset(p, state)))]
    if p.get("tag_ruleset"):
        rulesets.append(("tags", ruleset_body(p["tag_ruleset"], target="tag")))
    for area, body in rulesets:
        if avail.get("rulesets") is False:
            out.append(Drift(area, body["name"], "present", "not available", "unavailable",
                             "GitHub Team, or a public repo: GitHub Free has no rulesets on private repos"))
            continue
        for what in _rules_differ(body, state["rulesets"].get(body["name"])):
            out.append(Drift(area, body["name"], what, "differs" if what != "missing" else None))
    for team, perm in sorted(p["teams"].items()):
        if state["teams"].get(team) != perm:
            out.append(Drift("teams", team, perm, state["teams"].get(team)))
    missing_topics = [x for x in p["topics"] if x not in state["topics"]]
    if missing_topics:
        out.append(Drift("topics", "topics", sorted(set(state["topics"]) | set(p["topics"])), state["topics"]))
    for k, v in sorted(p["properties"].items()):
        if state["properties"].get(k) != v:
            out.append(Drift("properties", k, v, state["properties"].get(k)))
    for f in p.get("files") or []:
        if state["files"].get(f) is None:
            out.append(Drift("files", f, "present", "missing", "manual"))
    return out


def managed_drift(gh, t: Target) -> list[Drift]:
    """Managed workflow files whose copy differs from the platform's, or that are missing. A
    template is checked too: new repos start from it."""
    out = []
    for path, name in sorted((t.plan.get("managed_files") or {}).items()):
        src = remote_source(name)
        if src is not None and src[0] == t.repo:  # the owner keeps its own file
            continue
        want = managed_content(gh, name)
        if want is None:
            continue
        have = gh.get_file(repo=t.repo, path=path, ref="")
        if have != want:
            out.append(Drift("workflows", path, "the platform's copy", "missing" if have is None else "differs"))
    return out


def capability_drift(gh, t: Target) -> list[Drift]:
    """A template's gaps against the template contract (config/template-contract.yaml): security,
    observability, the audit trail and business metrics every repo of its role starts with."""
    from .template_contract import audit_event_check, check

    gaps = check(gh, t.repo, t.plan["role"]) if t.kind == "template" and t.plan.get("role") else []
    gaps += audit_event_check(gh, t.repo)
    return [Drift("capabilities", g["capability"], g["says"], g["problem"], fix="manual") for g in gaps]


# -- applying ----------------------------------------------------------------------------


def reconcile(gh, t: Target, *, apply: bool) -> Report:
    rep = Report(t.repo, t.kind)
    if t.error:
        rep.errors.append(t.error)
    try:
        state = gh.repo_state(repo=t.repo)
    except Exception as exc:  # one repo's problem (e.g. missing App permissions) doesn't stop the run
        rep.errors.append(f"{type(exc).__name__}: {exc}" if not isinstance(exc, GitHubError) else str(exc))
        return rep
    if state is None:
        rep.exists = False
        rep.drift = compare(t, None)
        return rep
    rep.drift = compare(t, state)
    if not state.get("archived"):
        try:
            rep.drift += managed_drift(gh, t)
        except GitHubError as exc:
            rep.errors.append(f"managed files: {exc}")
        if t.kind != "governance":
            try:
                rep.drift += capability_drift(gh, t)
            except GitHubError as exc:
                rep.errors.append(f"capabilities: {exc}")
    if not apply:
        return rep
    p, areas = t.plan, {d.area for d in rep.drift if d.fix == "apply"}

    def do(what: str, fn) -> None:
        try:
            fn()
            rep.applied.append(what)
        except GitHubError as exc:
            rep.errors.append(f"{what}: {exc}")

    if "settings" in areas:
        do("settings", lambda: gh.update_repo(repo=t.repo, merge=p["merge"], default_branch=p["default_branch"],
                                              features=p.get("features"), is_template=p.get("is_template")))
    if "security" in areas:
        wanted = {d.item: True for d in rep.drift if d.area == "security" and d.fix == "apply"}
        flags = {k: bool(wanted.get(k)) for k in p["security"]}
        do("security", lambda: gh.enable_security(repo=t.repo, **flags))
    if "ruleset" in areas:
        do("ruleset", lambda: gh.upsert_ruleset(repo=t.repo, branch=p["default_branch"],
                                                spec=desired_ruleset(p, state)))
    if "tags" in areas:
        do("tags", lambda: gh.upsert_ruleset(repo=t.repo, branch=p["default_branch"], spec=p["tag_ruleset"],
                                             target="tag"))
    if "teams" in areas:
        org = t.repo.split("/", 1)[0]
        for d in [d for d in rep.drift if d.area == "teams"]:
            do(f"team {d.item}", lambda d=d: gh.grant_team(org=org, team=d.item, repo=t.repo, permission=d.want))
    if "topics" in areas:
        want = next(d.want for d in rep.drift if d.area == "topics")
        do("topics", lambda: gh.set_topics(repo=t.repo, topics=list(want)))
    if "properties" in areas:
        org = t.repo.split("/", 1)[0]
        do("properties", lambda: (gh.ensure_org_properties(org=org, names=sorted(p["properties"])),
                                  gh.set_properties(repo=t.repo, properties=p["properties"])))
    workflows = [d for d in rep.drift if d.area == "workflows"]
    pr_url = ""
    if workflows:
        def open_pr():
            nonlocal pr_url
            names = t.plan.get("managed_files") or {}
            contents = {d.item: managed_content(gh, names[d.item]) for d in workflows}
            # A branch per content: once a pull request for one version merges, the next version of
            # the platform's files gets a new branch and pull request (a fixed branch name would keep
            # finding the merged one and never bring the files up to date again).
            digest = hashlib.sha256("".join(f"{k}\0{v}\0" for k, v in sorted(contents.items())).encode())
            branch = f"valxel/managed-files-{digest.hexdigest()[:10]}"
            for d in workflows:
                gh.put_file(repo=t.repo, branch=branch, path=d.item, content=contents[d.item],
                            message=f"Bring {d.item} back to the platform's copy")
            pr = gh.open_pr(repo=t.repo, branch=branch, title="Update the platform's managed workflows",
                            body="The Valxel SDLC manages these files (config/repos.yaml `managed_files`); this "
                                 "brings them back to the platform's copy:\n\n"
                                 + "\n".join(f"- `{d.item}` ({d.have})" for d in workflows)
                                 + "\n\nChange them where they're owned (agentic-sdlc, or the repo a contract names), "
                                   "not here: the next check would undo it.",
                            files=[d.item for d in workflows], labels=["valxel"])
            pr_url = pr.get("url", "")

        do("managed files", open_pr)
    if rep.applied:  # what's left after the changes
        try:
            after = gh.repo_state(repo=t.repo)
            rep.drift = compare(t, after) if after is not None else rep.drift
            # The workflow files change when the pull request merges; until then they still differ.
            rep.drift += [Drift(d.area, d.item, d.want, f"{d.have}; pull request {pr_url}".rstrip("; "), "pending")
                          for d in workflows] if pr_url else workflows
        except GitHubError as exc:
            rep.errors.append(str(exc))
    return rep


def run(services, gh, *, apply: bool, only: list[str] | None = None) -> list[Report]:
    reports = [reconcile(gh, t, apply=apply) for t in targets(services, only)]
    return reports + copy_reports(gh, only)


def copy_reports(gh, only: list[str] | None = None) -> list[Report]:
    """Copies other repos keep of the platform's files (template-contract.yaml shared_copies), one
    report per repo with a copy that drifted. Fixed by hand: the copy's repo regenerates from it."""
    from .template_contract import shared_copy_check

    by: dict[str, Report] = {}
    for f in shared_copy_check(gh):
        if only and f["repo"] not in only:
            continue
        rep = by.setdefault(f["repo"], Report(f["repo"], "copy"))
        rep.drift.append(Drift("copies", f["capability"], f["says"], f["problem"], fix="manual"))
    return list(by.values())


def summary(reports: list[Report]) -> str:
    lines = []
    for r in reports:
        if not r.exists:
            lines.append(f"✗ {r.repo} ({r.kind}): doesn't exist")
            continue
        mark = "✓" if r.ok and not r.drift else "!" if r.ok else "✗"
        lines.append(f"{mark} {r.repo} ({r.kind})" + (f": applied {', '.join(r.applied)}" if r.applied else ""))
        lines += [f"    {d.line()}" for d in r.drift]
        lines += [f"    error: {e}" for e in r.errors]
    bad = sum(1 for r in reports if not r.ok)
    lines.append(f"\n{len(reports)} repos, {bad} with drift to fix" if bad else f"\n{len(reports)} repos on standard")
    return "\n".join(lines)

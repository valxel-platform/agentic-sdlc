"""Keep every repo on the standards (config/repos.yaml), not only the ones the pipeline creates.

`vrixel repos check` compares each managed repo with what the standards ask for and reports the
drift; `vrixel repos apply` changes what differs. Managed repos are:

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

from dataclasses import dataclass, field

from .integrations.github import GitHubError, repo_settings_body, ruleset_body
from .provisioning import effective_standards, plan

KINDS = ("lane", "governance", "template", "tool")


@dataclass
class Target:
    repo: str
    kind: str
    product: str  # lane id, or "platform"
    plan: dict  # what the standards ask for (vrixel_sdlc.provisioning.plan, plus kind-specific settings)


@dataclass
class Drift:
    area: str  # settings | security | ruleset | tags | teams | topics | properties | files | visibility | repo
    item: str
    want: object
    have: object
    fix: str = "apply"  # apply | manual | unavailable

    def line(self) -> str:
        how = {"manual": " (fix by hand)", "unavailable": " (needs GitHub Advanced Security)"}.get(self.fix, "")
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
        return self.exists and not [d for d in self.drift if d.fix == "apply"] and not self.errors

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
        lane = None
        try:
            lane = services.snapshot(pid).product
        except Exception:  # an unreadable lane still has a governance repo to keep on standards
            pass
        out[gov] = Target(gov, "governance", pid, _governance_plan(std, gov, pid, entry.data_class,
                                                                  lane.approvers if lane else {}))
        if lane is None:
            continue
        standards = lane.repo_standards or effective_standards(std, lane.data_class, {})
        for r in lane.repos:
            p = plan(lane, {"name": r.name, "role": r.role, "purpose": ""}, standards)
            out[r.name] = Target(r.name, "lane", pid, p)
    for repo, spec in (std.platform_repos or {}).items():
        out[repo] = Target(repo, spec.get("kind", "tool"), spec.get("product", "platform"),
                           _platform_plan(std, repo, spec))
    # The platform's GitHub App merges for the merge train once the pipeline's gates pass, so it
    # may bypass the review rules (pull requests only) wherever the pipeline merges: lane repos.
    import os

    app_id = os.environ.get("VRIXEL_GITHUB_APP_ID", "")
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
    p["ruleset"]["code_scanning_merge_protection"] = None
    p["security"]["code_scanning_default_setup"] = False  # no code to scan
    p["tag_ruleset"] = {}
    p["files"] = [f for f in p["files"] if f != ".github/dependabot.yml"]
    p["codeowners"] = None  # owned by the lane's people, not generated
    return p


def _platform_plan(std, repo: str, spec: dict) -> dict:
    kind = spec.get("kind", "tool")
    if kind == "governance":
        return _governance_plan(std, repo, spec.get("product", "platform"), "general", {})
    role = spec.get("role") or "tool"
    p = _base(std, repo, spec.get("product", "platform"), spec.get("data_class", "general"), {}, role)
    checks = spec.get("required_checks") or (std.roles.get(role) or {}).get("required_checks") or []
    p["ruleset"]["required_checks"] = list(checks)
    p["is_template"] = kind == "template"
    p["codeowners"] = None
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
                if sorted(c["context"] for c in v) != sorted(c.get("context") for c in hp.get(k) or []):
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
    for k, v in want.items():
        if state["settings"].get(k) != v:
            out.append(Drift("settings", k, v, state["settings"].get(k)))
    avail = state["available"]
    for k, v in p["security"].items():
        if not v or state["security"].get(k):
            continue
        needs = {"code_scanning_default_setup": "code_scanning", "secret_scanning": "secret_scanning",
                 "push_protection": "secret_scanning"}.get(k)
        out.append(Drift("security", k, True, False, "unavailable" if needs and not avail.get(needs) else "apply"))
    body = ruleset_body(desired_ruleset(p, state))
    for what in _rules_differ(body, state["rulesets"].get(body["name"])):
        out.append(Drift("ruleset", body["name"], what, "differs" if what != "missing" else None))
    if p.get("tag_ruleset"):
        tags = ruleset_body(p["tag_ruleset"], target="tag")
        for what in _rules_differ(tags, state["rulesets"].get(tags["name"])):
            out.append(Drift("tags", tags["name"], what, "differs" if what != "missing" else None))
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


# -- applying ----------------------------------------------------------------------------


def reconcile(gh, t: Target, *, apply: bool) -> Report:
    rep = Report(t.repo, t.kind)
    try:
        state = gh.repo_state(repo=t.repo)
    except GitHubError as exc:
        rep.errors.append(str(exc))
        return rep
    if state is None:
        rep.exists = False
        rep.drift = compare(t, None)
        return rep
    rep.drift = compare(t, state)
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
    if rep.applied:  # what's left after the changes
        try:
            after = gh.repo_state(repo=t.repo)
            rep.drift = compare(t, after) if after is not None else rep.drift
        except GitHubError as exc:
            rep.errors.append(str(exc))
    return rep


def run(services, gh, *, apply: bool, only: list[str] | None = None) -> list[Report]:
    return [reconcile(gh, t, apply=apply) for t in targets(services, only)]


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

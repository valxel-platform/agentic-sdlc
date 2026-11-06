"""Deterministic checks per phase. They run before any critic, so a broken artifact never
costs a critic call's worth of judgment, and they are the evaluator layer a human can trust."""

from __future__ import annotations

from ..agents.parse import (
    bullets,
    is_testable,
    parse_acs,
    parse_affected_repos,
    parse_tasks,
    parse_trace,
    section,
)


def _discovery(text: str, ctx: dict) -> list[str]:
    out = []
    if not [b for b in bullets(section(text, "Success metrics")) if b.startswith("M")]:
        out.append("No success metrics: every brief needs at least one measurable metric")
    cap = section(text, "Capability")
    if "Extends:" not in cap and "New capability:" not in cap:
        out.append("Brief must declare 'Extends: CAP-x' or 'New capability: <why>' (VX-6)")
    return out


def _spec(text: str, ctx: dict) -> list[str]:
    out = []
    if not section(text, "Goals"):
        out.append("PRD has no goals")
    if not section(text, "Non-goals"):
        out.append("PRD has no non-goals")
    acs = parse_acs(text)
    if not acs:
        out.append("PRD has no acceptance criteria")
    for ac_id, ac in acs.items():
        if not is_testable(ac):
            out.append(f"{ac_id} is not testable: write it as Given / when / then")
    cited = {b.split()[0] for b in bullets(section(text, "Constitution articles")) if b}
    if not cited & set(ctx.get("articles", [])):
        out.append("PRD cites no constitution articles")
    return out


def _design(text: str, ctx: dict) -> list[str]:
    out = []
    if not bullets(section(text, "Screens")):
        out.append("UX spec lists no screens")
    if "WCAG" not in section(text, "Accessibility"):
        out.append("No accessibility section with WCAG 2.1 AA criteria (VX-9)")
    return out


def _architecture(text: str, ctx: dict) -> list[str]:
    out = []
    if not bullets(section(text, "ADRs")):
        out.append("HLD records no ADRs")
    repos = parse_affected_repos(text)
    from ..agents.parse import parse_new_repos

    new = parse_new_repos(text)
    known = {r["name"] for r in ctx.get("repos", []) + ctx.get("dependency_repos", [])}
    if new:
        from ..provisioning import check_proposal

        if ctx.get("repo_policy"):
            out += check_proposal(ctx["repo_policy"], new, known)
        else:
            out.append("New repos are proposed, but the platform has no repository standards (config/repos.yaml)")
    known |= {r["name"] for r in new}
    if not repos:
        out.append("HLD lists no affected repos; impact analysis is required to seed the change set")
    for r in repos:
        if r["name"] not in known:
            out.append(f"Affected repo {r['name']} is not registered for this product or a lane it depends on; "
                       "it needs a provisioning ADR or a depends_on entry")
    if not section(text, "Contracts"):
        out.append("HLD has no contracts section (VX-4)")
    out += _audit(text, repos, ctx)
    out += _events(text, repos)
    out += _acceptance(repos, ctx)
    out += _endpoints(text, repos, ctx)
    out += _diagram(text, repos)
    if ctx.get("knowledge"):
        # Against the existing design: cited decisions are current, overrides are explicit, and
        # contract changes start from the version actually merged.
        from ..knowledge import design_review

        out += design_review(text, ctx["knowledge"], ctx.get("feature_id", ""))["findings"]
    return out


RUNTIME_ROLES = ("provider", "event-worker", "consumer", "agent", "mcp-server")


EXPOSED_ROLES = RUNTIME_ROLES + ("bff", "mobile")


def _endpoints(text: str, repos: list[dict], ctx: dict) -> list[str]:
    """VX-17: the design says who may reach each component and what it calls. Only the BFF faces
    the internet; anything else external needs an ADR that says so."""
    from ..agents.parse import VISIBILITIES, parse_endpoints, section

    runtime = [r for r in repos if r["role"] in EXPOSED_ROLES]
    if not runtime:
        return []
    declared = {e["repo"]: e for e in parse_endpoints(text)}
    if not declared:
        return ["HLD has no Endpoints section: one line per component, '- <org>/<repo>: <project|namespace|"
                "internal|external|none>[; calls <component>, ...]' (VX-17)"]
    out = [f"{r['name']} has no line in Endpoints (VX-17)" for r in runtime if r["name"] not in declared]
    roles = {r["name"]: r["role"] for r in repos}
    known = {n.split("/", 1)[-1] for n in roles} | {r["name"].split("/", 1)[-1] for r in ctx.get("repos") or []}
    bffs = {r["name"].split("/", 1)[-1] for r in [*repos, *(ctx.get("repos") or [])] if r.get("role") == "bff"}
    external_adr = "external" in section(text, "ADRs").lower()
    for repo, e in declared.items():
        bad = [v for v in e["visibility"] if v not in VISIBILITIES]
        if bad:
            out.append(f"{repo}: unknown visibility {', '.join(bad)} (use {', '.join(VISIBILITIES)})")
        if "external" in e["visibility"] and roles.get(repo) != "bff":
            if roles.get(repo) == "consumer":
                out.append(f"{repo} is a web app: it reaches the internet through the product's BFF, not directly (VX-17)")
            elif not external_adr:
                out.append(f"{repo} is external but isn't the BFF: add an ADR approving the exposure (a partner or "
                           "mobile API through the gateway), or make it project (VX-17)")
        if roles.get(repo) == "mobile":
            # A store app runs on phones, not in the platform: nothing reaches it, and it reaches
            # the product only through the BFF's mobile route (VX-18).
            if e["visibility"] != ["none"]:
                out.append(f"{repo} is a mobile app: its visibility is none (it runs on phones) (VX-18)")
            if not bffs:
                out.append(f"{repo} is a mobile app but the product has no BFF: add one (template-bff) for "
                           "its /mobile/api route (VX-18)")
            direct = [c for c in e["calls"] if c in known and c not in bffs]
            if direct:
                out.append(f"{repo} calls {', '.join(direct)} directly: a mobile app calls only the product's "
                           "BFF, which passes its token on (VX-18)")
        unknown = [c for c in e["calls"] if c not in known]
        if unknown:
            out.append(f"{repo} calls {', '.join(unknown)}, which isn't in this design or the product")
    return out


def _acceptance(repos: list[dict], ctx: dict) -> list[str]:
    """A change across repos is tested end to end in the product's acceptance repo, when it has one."""
    from .. import acceptance

    acc = acceptance.acceptance_repo(ctx.get("repos") or [])
    if not acc or not acceptance.needed(repos, ctx.get("acceptance_min_repos", 2)):
        return []
    if any(r["name"] == acc for r in repos):
        return []
    return [f"The change set spans {len(acceptance.runtime_repos(repos))} runtime repos; list {acc} (acceptance) "
            "in Affected repos so the feature is tested end to end"]


def _events(text: str, repos: list[dict]) -> list[str]:
    """VX-16: a design with event workers names the events they apply."""
    from ..agents.parse import parse_events

    if not any(r["role"] == "event-worker" for r in repos) or parse_events(text):
        return []
    return ["HLD has event workers but no Events section naming the events, each as "
            "'- vrixel.<entity>.<what happened>.v1: <who publishes it, who applies it>' (VX-16)"]


def _audit(text: str, repos: list[dict], ctx: dict) -> list[str]:
    """VX-15: in PHI and payments products, the design names what gets audited."""
    from ..agents.parse import parse_audit

    if ctx.get("data_class") not in ("phi", "payments") or not any(r["role"] in RUNTIME_ROLES for r in repos):
        return []
    if not parse_audit(text):
        return ["HLD has no Audit section naming the actions that read or change records, each as "
                "'- <record>.<verb>: <record type>' (VX-15)"]
    return []


def _diagram(text: str, repos: list[dict]) -> list[str]:
    """The diagram is published for reviewers, so it must parse and must show the change set."""
    from ..integrations.diagrams import extract_mermaid, parse_flowchart

    source = extract_mermaid(section(text, "Diagram"))
    if not source:
        return ["HLD has no Diagram section with a mermaid flowchart"]
    labels = " ".join(n["label"] for n in parse_flowchart(source).nodes.values())
    if not labels:
        return ["The HLD diagram has no nodes; write a mermaid flowchart"]
    return [f"The HLD diagram does not show {r['name']}" for r in repos
            if r["name"].split("/", 1)[-1] not in labels]


def _plan(text: str, ctx: dict) -> list[str]:
    out = []
    tasks = parse_tasks(text)
    if not tasks:
        out.append("Plan has no machine-readable task list")
    acs = parse_acs(ctx.get("parent_texts", {}).get("PRD", ""))
    traced = {ac for ac, _, _ in parse_trace(text)}
    for ac in acs:
        if ac not in traced:
            out.append(f"{ac} has no task and test (VX-5)")
    affected = {r["name"] for r in parse_affected_repos(ctx.get("parent_texts", {}).get("HLD", ""))}
    for t in tasks:
        if affected and t.get("repo") not in affected:
            out.append(f"{t.get('id')} targets {t.get('repo')}, which is not in the HLD's affected repos")
    from .. import acceptance

    out += acceptance.check_plan(text, {**ctx, "plan_tasks": tasks})
    return out


def _release(text: str, ctx: dict) -> list[str]:
    out = []
    checklist = section(text, "Checklist")
    for line in checklist.splitlines():
        if line.startswith("- [ ]"):
            out.append(f"Unchecked release item: {line[6:].strip()}")
    if "Rollback plan" not in checklist:
        out.append("No rollback plan")
    return out


def _operate(text: str, ctx: dict) -> list[str]:
    return [] if bullets(section(text, "Metrics vs brief")) else ["No metrics against the brief's success criteria"]


CHECKS = {
    "discovery": _discovery,
    "spec": _spec,
    "design": _design,
    "architecture": _architecture,
    "plan": _plan,
    "release": _release,
    "operate": _operate,
}


def run_checks(phase: str, text: str, ctx: dict) -> list[str]:
    fn = CHECKS.get(phase)
    return fn(text, ctx) if fn else []

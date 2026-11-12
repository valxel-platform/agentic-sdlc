"""Deterministic checks per phase. They run before any critic, so a broken artifact never
costs a critic call's worth of judgment, and they are the evaluator layer a human can trust."""

from __future__ import annotations

import re

from ..agents.parse import (
    CASE_KINDS,
    NFR_KINDS,
    bullets,
    is_testable,
    parse_ac_goals,
    parse_acs,
    parse_cases,
    parse_deferred,
    parse_nfrs,
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
    return out + _product_goal(text, ctx)


def _product_goal(text: str, ctx: dict) -> list[str]:
    """The brief names the product goal it moves, from roadmap/goals.yaml (outcome loop, step 2)."""
    if "product_goals" not in ctx:
        return []
    from ..agents.parse import parse_product_goal

    known = [g["id"] for g in ctx["product_goals"]]
    ids, none = parse_product_goal(text)
    out = [f"Product goal {g} isn't in roadmap/goals.yaml" + (f" (goals: {', '.join(known)})" if known else
                                                              " (the product has no goals yet: write '- None: <why>')")
           for g in ids if g not in known]
    if known and not ids and not none:
        out.append("Name the product goal this moves under '## Product goal': '- GOAL-...: <how>' (goals: "
                   f"{', '.join(known)}), or '- None: <why>' for work no goal covers")
    init = ctx.get("initiative_goal")
    if init and init in known and ids and init not in ids:
        out.append(f"The feature's initiative serves {init}; name it under Product goal, or say why this feature "
                   "moves a different one")
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
    # Every criterion serves a goal the PRD has (the link from metric to test).
    goals = {g.split(":")[0].strip() for g in bullets(section(text, "Goals"))}
    for ac_id, served in parse_ac_goals(text).items():
        if not served:
            out.append(f"{ac_id} names no goal: write '- {ac_id} (G1): ...'")
        for g in served:
            if goals and g not in goals:
                out.append(f"{ac_id} serves {g}, which isn't one of the PRD's goals")
    out += _nfrs(text, goals)
    out += _instrumentation(text, ctx)
    return out


def _instrumentation(text: str, ctx: dict) -> list[str]:
    """The events that show whether the feature moves the brief's product goal (outcome loop, step 2)."""
    from ..agents.parse import parse_instrumentation, parse_product_goal
    from ..outcomes.instrumentation import event_problems

    brief = (ctx.get("parent_texts") or {}).get("OPP", "")
    goals, _ = parse_product_goal(brief)
    events = parse_instrumentation(text)
    if not goals:
        return event_problems(events, [g for e in events for g in e["goals"]], ctx.get("data_class", "")) if events else []
    if not events:
        return [f"The brief moves {', '.join(goals)}: list the events that show it under '## Instrumentation' "
                "as '- <event_name> (GOAL-...): fires when ...; properties: ...'"]
    return event_problems(events, goals, ctx.get("data_class", ""))


_MEASURABLE = re.compile(r"\d|WCAG|OWASP|HIPAA|PCI|SOC ?2|GDPR|ISO ?27001|FIPS", re.I)


def _nfrs(text: str, goals: set[str]) -> list[str]:
    """The PRD's non-functional requirements: present (or explicitly none), of a known kind, and
    measurable, so a test can prove them."""
    body = section(text, "Non-functional requirements")
    if not body:
        return ["PRD has no Non-functional requirements section (list them, or '- None: <why>')"]
    nfrs = parse_nfrs(text)
    if not nfrs and not any(b.lower().startswith("none") for b in bullets(body)):
        return ["Non-functional requirements lists none in the form '- NFR-1 (performance, G1): <target>'"]
    out = []
    for nid, n in nfrs.items():
        if n["kind"] not in NFR_KINDS:
            out.append(f"{nid}'s kind {n['kind']!r} is not one of: {', '.join(NFR_KINDS)}")
        if not _MEASURABLE.search(n["text"]):
            out.append(f"{nid} is not measurable: give a number, a percentile, a limit or a named standard")
        if n["goal"] and goals and n["goal"] not in goals:
            out.append(f"{nid} serves {n['goal']}, which isn't one of the PRD's goals")
    return out


_COVER = re.compile(r"^-\s*((?:AC|NFR)-\d+)\s*:\s*(.+?)\s*$", re.M)


def coverage(text: str, ctx: dict, what: str) -> list[str]:
    """Every PRD criterion is cited in the document's Coverage section, with where it is handled;
    none that the PRD doesn't have (borrowed from AI-DLC's upstream-coverage and traceability)."""
    prd = ctx.get("parent_texts", {}).get("PRD", "")
    acs = list(parse_acs(prd)) + (list(parse_nfrs(prd)) if what == "HLD" else [])
    if not acs:
        return []
    cited = {ac: where for ac, where in _COVER.findall(section(text, "Coverage"))}
    out = [f"{ac} is not in the {what}'s Coverage section (say where it is handled, or why not here)"
           for ac in acs if ac not in cited or not cited[ac].strip()]
    known = set(acs) | set(parse_nfrs(prd))
    out += [f"{ac} in the {what}'s Coverage section is not a requirement in the PRD" for ac in cited if ac not in known]
    return out


def _design(text: str, ctx: dict) -> list[str]:
    out = coverage(text, ctx, "UX spec")
    if not bullets(section(text, "Screens")):
        out.append("UX spec lists no screens")
    if "WCAG" not in section(text, "Accessibility"):
        out.append("No accessibility section with WCAG 2.1 AA criteria (VX-9)")
    return out + _ux_structure(text, ctx)


def _ux_structure(text: str, ctx: dict) -> list[str]:
    """What a builder needs: each screen's app, its states, how people move between screens, and the copy."""
    from ..agents.parse import SCREEN_ROLES, parse_copy, parse_flow, parse_screen_apps, parse_screens, parse_states

    screens = parse_screens(text)
    if not screens:
        return []
    out = []
    apps = {r["name"] for r in ctx.get("repos") or [] if r.get("role") in SCREEN_ROLES}
    for sid, repo in parse_screen_apps(text).items():
        if not repo:
            out.append(f"{sid}: say which app it's in, as '- {sid}: <name> (web: <org>/<repo>)' or '(mobile: ...)'")
        elif apps and repo not in apps:
            out.append(f"{sid} is in {repo}, which isn't one of the product's apps ({', '.join(sorted(apps))})")
    states = parse_states(text)
    for sid in screens:
        st = states.get(sid, "")
        if not st:
            out.append(f"{sid} has no line under States ('- {sid}: loading, empty (...), error (...), success')")
            continue
        if not re.search(r"\berror", st, re.I):
            out.append(f"{sid}: no error state: what does the person see when it fails, and what can they do?")
        if not re.search(r"\bempty|no empty", st, re.I):
            out.append(f"{sid}: no empty state (or 'no empty: <why>')")
    flow = parse_flow(text)
    if not flow:
        out.append("No Flow section: '- start -> S1: <from where>', '- S1 -> S2: <action>', '- S2 -> done'")
    else:
        reach, frontier = set(), ["start"]
        while frontier:
            at = frontier.pop()
            for a, b, _ in flow:
                if a == at and b not in reach:
                    reach.add(b)
                    frontier.append(b)
        out += [f"{sid} ({screens[sid]}) can't be reached from start in the Flow" for sid in screens if sid not in reach]
        if not any(b in ("done", "cancel") for _, b, _ in flow):
            out.append("Flow: say how the person leaves ('- S2 -> done' or '-> cancel')")
    copy = parse_copy(text)
    if not copy:
        out.append("No Copy section: each screen's button labels, headings and error messages, word for word "
                   "('- S1 save button: \"Check in\"')")
    else:
        out += [f"{sid} ({screens[sid]}) has no copy under Copy" for sid in screens if sid not in copy]
    return out


def _architecture(text: str, ctx: dict) -> list[str]:
    out = coverage(text, ctx, "HLD")
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
    out += _data(text, repos, ctx)
    out += _threats(text, repos, ctx)
    out += _events(text, repos)
    out += _emitters(text, repos, ctx)
    out += _acceptance(repos, ctx)
    out += _endpoints(text, repos, ctx)
    from ..resources import check as resources_check

    out += resources_check(text, repos, ctx)
    out += _diagram(text, repos)
    out += _flows(text, repos)
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


DATA_ROLES = ("provider", "event-worker")
_DESTRUCTIVE = re.compile(r"\b(drop|drops|dropped|rename|renames|renamed|remove[sd]? column)\b", re.I)


def _data(text: str, repos: list[dict], ctx: dict) -> list[str]:
    """G4 sees the data risk: tables, migration, backfill, retention, rollback."""
    from ..agents.parse import parse_data

    if not any(r["role"] in DATA_ROLES for r in repos):
        return []
    if not section(text, "Data"):
        return ["HLD has no Data section: each table as '- <table>: <new|changed|none>; <what changes>', then "
                "Migration, Backfill, Retention and Rollback lines (or '- None: <why no data changes>')"]
    d = parse_data(text)
    out = [f"Data: '{b[:80]}' isn't '- <table>: <new|changed|none>; ...' or a Migration, Backfill, Retention or "
           "Rollback line" for b in d["bad"]]
    changed = [t for t in d["tables"] if t["change"] != "none"]
    if changed:
        if not d["migration"]:
            out.append(f"Data: {', '.join(t['table'] for t in changed)} change, but there's no Migration line")
        if not d["rollback"]:
            out.append("Data: tables change, but there's no Rollback line saying what happens to data the new "
                       "version wrote if the release is rolled back")
        if not d["backfill"]:
            out.append("Data: tables change, but there's no Backfill line ('none' if nothing needs filling)")
        destructive = [t["table"] for t in changed if _DESTRUCTIVE.search(t["what"])]
        if destructive and not re.search(r"\b(expand|contract|later release|two releases)\b", d["migration"], re.I):
            out.append(f"Data: {', '.join(destructive)} drop or rename a column: expand now and contract in a "
                       "later release, so the version running during the rollout still works (say so on Migration)")
    if ctx.get("data_class") in ("phi", "payments") and changed and not d["retention"]:
        out.append(f"Data: a {ctx['data_class']} product's tables need a Retention line: how long, and why")
    return out


def _threats(text: str, repos: list[dict], ctx: dict) -> list[str]:
    """Every product: where untrusted input enters, who may call each component and which records it
    may touch, and each threat with how it's prevented and detected."""
    from ..agents.parse import parse_endpoints, parse_threats

    runtime = [r for r in repos if r["role"] in EXPOSED_ROLES and r["role"] != "mobile"]
    if not runtime:
        return []
    if not section(text, "Threats"):
        return ["HLD has no Threats section: a Boundary line per reachable component, an Authorization line per "
                "component that serves requests, and each threat with 'prevented:' and 'detected:'"]
    t = parse_threats(text)
    out = [f"Threats: '{b[:80]}' isn't a Boundary, Authorization or '<threat>: <who>; prevented: <how>; detected: "
           "<how>' line" for b in t["bad"]]
    bounded = {b["repo"] for b in t["boundaries"]}
    authorized = {a["repo"] for a in t["authorization"]}
    for e in parse_endpoints(text):
        reachable = [v for v in e["visibility"] if v in ("project", "namespace", "external")]
        if reachable and e["repo"] not in bounded:
            out.append(f"Threats: {e['repo']} is reachable ({', '.join(reachable)}) but has no Boundary line")
        if reachable and e["repo"] not in authorized:
            out.append(f"Threats: {e['repo']} serves requests but has no Authorization line: who may call it, and "
                       "which records they may touch")
    if not t["threats"]:
        out.append("Threats: name at least one threat, as '<threat>: <who>; prevented: <how>; detected: <how>'")
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


def _emitters(text: str, repos: list[dict], ctx: dict) -> list[str]:
    """Every event in the PRD's Instrumentation is recorded by an affected repo (outcome loop, step 2)."""
    from ..agents.parse import parse_emitters, parse_instrumentation

    wanted = [e["event"] for e in parse_instrumentation((ctx.get("parent_texts") or {}).get("PRD", ""))]
    if not wanted:
        return []
    emits = parse_emitters(text)
    names = {r["name"] for r in repos}
    out = [f"Event {e} from the PRD has no repo under '## Instrumentation' ('- {e}: <org>/<repo>')"
           for e in wanted if e not in emits]
    out += [f"Event {e} is recorded by {r}, which isn't an affected repo" for e, r in emits.items()
            if e in wanted and names and r not in names]
    out += [f"Event {e} under Instrumentation isn't in the PRD" for e in emits if e not in wanted]
    return out


def _flows(text: str, repos: list[dict]) -> list[str]:
    """When components talk to each other: a sequence per journey, and a failure path."""
    from ..agents.parse import parse_sequences

    talking = [r for r in repos if r["role"] in RUNTIME_ROLES + ("bff",)]
    if len(talking) < 2:
        return []
    seqs = parse_sequences(text)
    if not seqs:
        return ["HLD has no Flows section: a mermaid sequenceDiagram for each journey under Performance, with its "
                "failure path (alt ... end)"]
    out = ["A Flows diagram isn't a mermaid sequenceDiagram" for s in seqs if not s["ok"]][:1]
    names = {r["name"].split("/", 1)[-1] for r in repos}
    for s in seqs:
        strangers = [p for p in s["participants"] if p not in names and p.lower() not in
                     ("user", "person", "browser", "phone", "idp", "gateway", "kafka", "aws")]
        if s["ok"] and strangers:
            out.append(f"Flows{(' (' + s['title'] + ')') if s['title'] else ''}: {', '.join(strangers)} isn't an "
                       "affected repo (use the repo name without the org, or user, browser, phone, idp, gateway, kafka)")
    if seqs and not any(s["failure"] for s in seqs):
        out.append("Flows: show at least one failure path (an 'alt' block for a dependency that is slow or down)")
    return out


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
    prd = ctx.get("parent_texts", {}).get("PRD", "")
    acs = parse_acs(prd)
    reqs = list(acs) + list(parse_nfrs(prd))
    traced = {req for req, _, _ in parse_trace(text)}
    deferred = parse_deferred(text)
    for req in reqs:
        if req in deferred:
            if not deferred[req].strip():
                out.append(f"{req} is deferred without a reason")
        elif req not in traced:
            out.append(f"{req} has no task (VX-5)")
    # The other direction: every task traces to the PRD, or says why it is needed.
    for t in tasks:
        named = list(t.get("acs") or [])
        unknown = [a for a in named if reqs and a not in reqs]
        if unknown:
            out.append(f"{t.get('id')} names {', '.join(unknown)}, which the PRD doesn't have")
        if not named and not str(t.get("why") or "").strip():
            out.append(f"{t.get('id')} implements no acceptance criterion and doesn't say why it's needed "
                       "(work the PRD doesn't ask for)")
    out += _cases(text, reqs, deferred, tasks)
    affected = {r["name"] for r in parse_affected_repos(ctx.get("parent_texts", {}).get("HLD", ""))}
    for t in tasks:
        if affected and t.get("repo") not in affected:
            out.append(f"{t.get('id')} targets {t.get('repo')}, which is not in the HLD's affected repos")
    out += _screens(tasks, ctx)
    out += _permission_cases(text, tasks, ctx)
    from .. import acceptance

    out += acceptance.check_plan(text, {**ctx, "plan_tasks": tasks})
    return out


def _permission_cases(text: str, tasks: list[dict], ctx: dict) -> list[str]:
    """Each component with an Authorization line in the design gets a permission test in the plan."""
    from ..agents.parse import parse_threats

    authz = {a["repo"] for a in parse_threats((ctx.get("parent_texts") or {}).get("HLD", ""))["authorization"]}
    if not authz:
        return []
    by_task = {t.get("id"): t.get("repo") for t in tasks}
    tested = {by_task.get(c["task"]) for c in parse_cases(text) if c["kind"] == "permission"}
    return [f"{repo} has an Authorization line in the design but no permission test case "
            f"('- TC-n (AC-x, permission): <who is refused> -> TASK-n <file>::<name>')"
            for repo in sorted(authz) if repo in by_task.values() and repo not in tested]


def _screens(tasks: list[dict], ctx: dict) -> list[str]:
    """The approved UX spec reaches the build: each web or mobile task names the screens it builds,
    and every screen is built by one."""
    from ..agents.parse import SCREEN_ROLES, parse_screens

    screens = parse_screens((ctx.get("parent_texts") or {}).get("UX", ""))
    ui = [t for t in tasks if t.get("role") in SCREEN_ROLES]
    if not screens or not ui:
        return []
    out = []
    for t in ui:
        named = [str(x) for x in t.get("screens") or []]
        if not named:
            out.append(f"{t.get('id')} changes {t.get('repo')} but names no screen from the UX spec "
                       f"(\"screens\": [{', '.join(repr(k) for k in list(screens)[:3])}])")
        out += [f"{t.get('id')} names screen {x}, which the UX spec doesn't have" for x in named if x not in screens]
    built = {str(x) for t in ui for x in t.get("screens") or []}
    out += [f"Screen {k} ({v}) from the UX spec isn't built by any web or mobile task" for k, v in screens.items()
            if k not in built]
    return out


def _cases(text: str, reqs: list[str], deferred: dict, tasks: list[dict]) -> list[str]:
    """The plan's test cases: every requirement not deferred has one, each names a requirement the
    PRD has, a known kind and a test reference the pipeline can check."""
    if not reqs:
        return []
    cases = parse_cases(text)
    out = []
    by_req: dict[str, list[dict]] = {}
    ids = {t.get("id"): t for t in tasks}
    for c in cases:
        by_req.setdefault(c["req"], []).append(c)
        if c["req"] not in reqs:
            out.append(f"{c['id']} tests {c['req']}, which the PRD doesn't have")
        if c["kind"] not in CASE_KINDS:
            out.append(f"{c['id']}'s kind {c['kind']!r} is not one of: {', '.join(CASE_KINDS)}")
        if c["via"] == "unknown":
            out.append(f"{c['id']}'s test '{c['ref']}' is not 'TASK-n <file>::<name>', 'acceptance <file> \"<title>\"', "
                       "'suite <name>' or 'manual: <why>'")
        elif c["via"] == "test" and c["task"] not in ids:
            out.append(f"{c['id']} names {c['task']}, which isn't a task in the plan")
    for req in reqs:
        if req not in deferred and not by_req.get(req):
            out.append(f"{req} has no test case")
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

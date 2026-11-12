"""Deterministic stub outputs per phase. In stub mode these stand in for model output so the
whole pipeline runs offline; `defective=True` drops a required element so evaluator loops,
escalations and gates can be exercised. Live mode replaces them with real agent output."""

from __future__ import annotations

import json
import re

from ..repo_roles import REPO_ORDER as _REPO_ORDER
from .parse import parse_acs, parse_affected_repos, parse_nfrs



def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")[:40] or "feature"


def _feedback(ctx: dict) -> list[str]:
    out = []
    if ctx.get("feedback"):
        out += ["", "## Reviewer feedback addressed", f"- {ctx['feedback']}"]
    if ctx.get("prior_findings"):
        out += ["", "## Evaluator findings addressed"] + [f"- {f}" for f in ctx["prior_findings"]]
    return out


def discovery(ctx: dict, defective: bool) -> str:
    # Stub judgement from what intake found: extend the closest related feature or capability,
    # or say why this is new (the reason the creator gave when it looked like a duplicate).
    related = ctx.get("related_features") or []
    reason = ctx.get("not_duplicate_reason", "")
    if related and reason:
        cap = f"New capability: {reason} (related: {related[0]['id']} {related[0]['title']})"
    elif related:
        cap = f"Extends: {related[0]['id']} ({related[0]['title']})"
    else:
        cap = "New capability: nothing in the product covers this yet"
    lines = [
        f"# Opportunity brief: {ctx['title']}",
        f"Feature: {ctx['feature_id']} · Product: {ctx['product_name']}",
        "", "## Problem", ctx.get("description") or ctx["title"],
        "", "## Capability", cap,
        "", "## Product goal", _goal_line(ctx),
    ]
    if not defective:
        lines += ["", "## Success metrics",
                  f"- M1: Weekly active use of {ctx['title']} reaches [target] within 30 days",
                  "- M2: Support tickets for this workflow fall by [target]%"]
    lines += ["", "## Constitution articles", "- VX-6"]
    return "\n".join(lines + _feedback(ctx)) + "\n"


def _goal_line(ctx: dict) -> str:
    """Stub judgement: the initiative's goal, else the product's first; a live agent picks the one
    the request moves."""
    goals = [g["id"] for g in ctx.get("product_goals") or []]
    goal = ctx.get("initiative_goal") if ctx.get("initiative_goal") in goals else (goals[0] if goals else "")
    if not goal:
        return "- None: the product has no goals in roadmap/goals.yaml yet"
    return f"- {goal}: {ctx['title']} takes a step out of the work this goal measures"


def _instrumentation_lines(ctx: dict) -> list[str]:
    from .parse import parse_product_goal

    goals, _ = parse_product_goal((ctx.get("parent_texts") or {}).get("OPP", ""))
    if not goals:
        return ["- None: the brief names no product goal"]
    base = _slug(ctx["title"])[:32].strip("_") or "feature"
    event = ("f_" + base if not base[0].isalpha() else base) + "_completed"
    return [f"- {event} ({', '.join(goals)}): fires when someone completes {ctx['title'].lower()}; "
            "properties: source, duration_ms"]


def spec(ctx: dict, defective: bool) -> str:
    title = ctx["title"]
    if defective:
        acs = [f"- AC-1 (G1): {title} works well", f"- AC-2 (G1): {title} is fast"]
    else:
        acs = [
            f"- AC-1 (G1): Given a signed-in user, when they complete {title}, then the change is saved and confirmed",
            f"- AC-2 (G1): Given invalid input, when they submit {title}, then a specific error is shown and nothing is saved",
            f"- AC-3 (G1): Given the action succeeds, when an auditor reviews it, then an audit record shows who, what and when",
        ]
    product_article = (ctx.get("product_articles") or ["VX-2"])[0]
    lines = [
        f"# PRD: {title}", f"Feature: {ctx['feature_id']}",
        "", "## Goals", f"- G1: Deliver {title} (measured by M1, M2)",
        "", "## Non-goals", f"- Changes outside {ctx['product_name']} scope",
        "", "## Acceptance criteria", *acs,
        "", "## Non-functional requirements",
        f"- NFR-1 (performance, G1): {title} answers 95% of requests within 500 ms at the expected peak",
        f"- NFR-2 (accessibility, G1): {title} meets WCAG 2.1 AA (keyboard path, labels, 4.5:1 contrast)",
        "", "## Instrumentation", *_instrumentation_lines(ctx),
        "", "## Constitution articles", "- VX-5", f"- {product_article}",
    ]
    if not defective:
        lines += ["", "## Agent notes", "- Assumed: the people who use this already sign in to the product"]
    return "\n".join(lines + _feedback(ctx)) + "\n"


def design(ctx: dict, defective: bool) -> str:
    from .parse import SCREEN_ROLES

    app = next((r for r in ctx.get("repos") or [] if r.get("role") in SCREEN_ROLES), None)
    where = f" ({'mobile' if app and app['role'] == 'mobile' else 'web'}: {app['name']})" if app else ""
    lines = [
        f"# UX spec: {ctx['title']}",
        "", "## Screens", f"- S1: {ctx['title']} form{where}", f"- S2: Confirmation{where}",
        "", "## States",
        "- S1: loading, empty (the form with its fields blank and a short hint), error (the fields keep what was "
        "typed, the problem is named beside its field, and Save can be pressed again), success",
        "- S2: loading, no empty: it only follows a save, error (the save didn't go through: Try again), success",
        "", "## Flow", f"- start -> S1: from the {ctx.get('product_name', 'product')} menu", "- S1 -> S2: Save",
        "- S1 -> cancel: Cancel", "- S2 -> done: Done",
        "", "## Copy", '- S1 heading: "' + ctx['title'] + '"', '- S1 save button: "Save"', '- S1 cancel button: "Cancel"',
        '- S1 error: "That didn\'t save. Check the highlighted fields and try again."', '- S2 heading: "Saved"',
        '- S2 done button: "Done"',
        "", "## Coverage", *coverage_lines(ctx, "S1"),
        "", "## Handoff", "- Uses design-system tokens and existing form components",
    ]
    if not defective:
        lines += ["", "## Accessibility", "- WCAG 2.1 AA: 4.5:1 contrast, full keyboard path, labelled fields (VX-9)"]
    return "\n".join(lines + _feedback(ctx)) + "\n"


def _mentions(ctx: dict, repo_name: str) -> bool:
    """Stub impact analysis: a dependency repo joins when the request names what it provides,
    e.g. 'audit' selects acme-shared/ss-audit-framework. Live architects decide."""
    words = [w for w in repo_name.split("/", 1)[-1].split("-")[1:] if len(w) >= 5]
    text = f"{ctx['title']} {ctx.get('description', '')}".lower()
    return any(w in text for w in words)


_NEW_SERVICE = re.compile(r"\b(new|separate|dedicated) (micro)?service\b", re.I)


def _new_repo(ctx: dict) -> dict | None:
    """Stub design judgement: a request for a new or separate service gets its own repo, and so
    does a product's first feature (it has no repos yet). Live architects decide from the HLD's
    boundaries."""
    greenfield = not ctx["repos"]
    if not greenfield and not _NEW_SERVICE.search(f"{ctx['title']} {ctx.get('description', '')}"):
        return None
    if greenfield:
        org, prefix = ctx["org"], ctx.get("repo_prefix") or "app-"
    else:
        org, first = ctx["repos"][0]["name"].split("/", 1)
        prefix = first.split("-", 1)[0] + "-"
    words = [w for w in re.findall(r"[a-z0-9]+", ctx["title"].lower()) if w not in ("a", "an", "the", "for", "of", "and", "to")]
    name = f"{org}/{prefix}{'-'.join(words[:2]) or 'feature'}-service"
    return {"name": name, "role": "provider", "lane": ctx["product_id"]}


def architecture(ctx: dict, defective: bool) -> str:
    deps = [r for r in ctx.get("dependency_repos", []) if _mentions(ctx, r["name"])]
    new = _new_repo(ctx)
    known = {r["name"] for r in ctx["repos"]}
    if new and new["name"] in known:  # already provisioned for this feature (a revision of the HLD)
        new = None
    repos = sorted(ctx["repos"] + deps + ([new] if new else []),
                   key=lambda r: (_REPO_ORDER.get(r["role"], 9), r["name"]))
    own = sorted(ctx["repos"] + ([new] if new else []), key=lambda r: _REPO_ORDER.get(r["role"], 9))
    provider = next((r["name"] for r in own if r["role"] == "provider"), own[0]["name"])
    kn = ctx.get("knowledge") or {}
    names = {r["name"] for r in repos}
    accepted = [a for a in (kn.get("architecture") or {}).get("adrs", []) if a["status"] == "accepted"]
    # Stub judgement: build on the accepted decisions about the repos this design touches.
    builds_on = [a for a in accepted if set(a["repos"]) & names] or accepted[:1]
    api = ((kn.get("contracts") or {}).get("apis") or {}).get(provider) or {}
    lines = [
        f"# HLD: {ctx['title']}",
        "", "## Builds on"]
    lines += ([f"- {a['id']}: {a['title']}" for a in builds_on] if builds_on else
              ["- None: the architecture record has no accepted decisions yet"])
    lines += [
        "", "## ADRs",
        (f"- ADR-{ctx['feature_id']}-1: Start {provider} with a versioned API (/api/v1) for {ctx['title'].lower()}"
         if not ctx["repos"] else
         f"- ADR-{ctx['feature_id']}-1: Extend the {provider} API with a versioned endpoint (expand, then contract)"),
    ]
    if new:
        lines.append(f"- ADR-{ctx['feature_id']}-2: Create {new['name']} as its own service: it has its own data, "
                     f"release cadence and scaling, and no existing repo owns that boundary")
    if not defective:
        lines += ["", "## Affected repos"] + [f"- {r['name']} ({r['role']})" for r in repos]
    if new:
        lines += ["", "## New repos",
                  f"- {new['name']} ({new['role']}): Owns {ctx['title'].lower()} [ADR-{ctx['feature_id']}-2]"]
    if deps:
        lines += ["", "## Dependency lanes"] + [
            f"- {r['name']} ({r['lane']}): new minor version, adopted by this product in the same change set"
            for r in deps]
    lines += [
        "", "## Contracts", f"- {provider}: " + (
            f"{api['version']} -> {_minor_bump(api['version'])}, adds an endpoint; backward compatible (VX-4)"
            if api.get("version") else "new API, starts at 1.0.0 (VX-4)" if new and provider == new["name"]
            else "OpenAPI minor version bump, backward compatible (VX-4)"),
        "", "## Diagram", "```mermaid", _flowchart(ctx, repos), "```",
    ]
    lines += _sequence_lines(repos)
    if not defective or ctx.get("data_class") not in ("phi", "payments"):
        record = _slug(ctx["title"]).split("-", 1)[0] or "record"
        lines += ["", "## Audit",
                  f"- {record}.read: {record} (who opened which {record}, through which service)",
                  f"- {record}.change: {record} (who changed it, on whose behalf, approved by whom)"]
    lines += ["", "## Endpoints", *_endpoint_lines(repos)]
    lines += _data_lines(ctx, repos) + _threat_lines(ctx, repos)
    lines += _resource_lines(ctx, repos, provider, lines)
    if any(r["role"] == "event-worker" for r in repos):
        record = _slug(ctx["title"]).split("-", 1)[0] or "record"
        workers = ", ".join(r["name"].split("/", 1)[-1] for r in repos if r["role"] == "event-worker")
        lines += ["", "## Events",
                  f"- vrixel.{record}.changed.v1: published by {provider.split('/', 1)[-1]}, applied by {workers}"]
    from .parse import parse_instrumentation

    wanted = parse_instrumentation((ctx.get("parent_texts") or {}).get("PRD", ""))
    if wanted:
        lines += ["", "## Instrumentation"] + [f"- {e['event']}: {provider}" for e in wanted]
    lines += ["", "## Performance",
              "- Expected load: as today's peak for this product",
              "- New traffic: yes: a new endpoint that people will call" if not ctx["repos"] or new else
              "- New traffic: no: an existing journey changes",
              "- Data growth: no: no new records are kept",
              "- Journeys: none beyond the feature's own"]
    lines += ["", "## Coverage", *coverage_lines(ctx, (repos[0]["name"] if repos else "the product"), nfrs=True)]
    lines += ["", "## Constitution articles", "- VX-4", "- VX-8", "- VX-15", "- VX-16", "- VX-17"]
    lines += ["", "## Agent notes", "- Traded off: extend the existing components rather than add a service,"
              " so there are fewer parts to run", "- Open: whether this change needs a feature flag beyond"
              " the one the pipeline creates"]
    return "\n".join(lines + _feedback(ctx)) + "\n"


def _sequence_lines(repos: list[dict]) -> list[str]:
    """Stub judgement: the main journey through the components that talk, front to back, and what
    happens when the last one is down."""
    order = ("consumer", "mobile", "bff", "agent", "mcp-server", "provider", "event-worker")
    talking = sorted([r for r in repos if r["role"] in order], key=lambda r: order.index(r["role"]))
    if len(talking) < 2 or sum(r["role"] != "mobile" for r in talking) < 2:
        return []
    names = [r["name"].split("/", 1)[-1] for r in talking]
    lines = ["", "## Flows", "```mermaid", "sequenceDiagram", "  title The feature's main journey"]
    lines += [f"  participant {n}" for n in names]
    lines += [f"  {a}->>{b}: request" for a, b in zip(names, names[1:])]
    lines += [f"  alt {names[-1]} is slow or down", f"    {names[-2]}-->>{names[0]}: an error the screen shows, nothing saved",
              "  else success"]
    lines += [f"    {b}-->>{a}: result" for a, b in reversed(list(zip(names, names[1:])))]
    lines += ["  end", "```"]
    return lines


def _data_lines(ctx: dict, repos: list[dict]) -> list[str]:
    """Stub judgement: a provider or worker keeps one new table, added in one step (nothing dropped)."""
    if not any(r["role"] in ("provider", "event-worker") for r in repos):
        return []
    record = (_slug(ctx["title"]).split("-", 1)[0] or "record").replace("-", "_")
    keep = {"phi": "7 years after the last visit, as the clinic's record policy requires",
            "payments": "7 years, as payment records must be"}.get(ctx.get("data_class", ""),
                                                                  "while the account exists, then 30 days")
    return ["", "## Data",
            f"- {record}: new; id, created_at, updated_at and the fields the PRD names",
            f"- Migration: V2__{record}.sql; one step: a new table, nothing existing changes",
            "- Backfill: none",
            f"- Retention: {keep}",
            "- Rollback: the table stays; the previous version doesn't read it, so nothing breaks"]


def _threat_lines(ctx: dict, repos: list[dict]) -> list[str]:
    """Stub judgement: a boundary and an authorization rule for each reachable component, and the
    two threats every request-serving change has."""
    from .parse import parse_endpoints

    reach = [e for e in parse_endpoints("## Endpoints\n" + "\n".join(_endpoint_lines(repos)))
             if set(e["visibility"]) & {"project", "namespace", "external"}]
    if not reach:
        return []
    out = ["", "## Threats"]
    for e in reach:
        where = "requests from the internet, signed in through the BFF" if "external" in e["visibility"] else \
            "requests from the product's own components, with the caller's token"
        out.append(f"- Boundary: {e['repo']}: {where}")
    for e in reach:
        out.append(f"- Authorization: {e['repo']} all endpoints: signed-in people of the product's organization; "
                   "each request may touch only the records its caller's organization owns")
    out += ["- Disclosure: another organization's user; prevented: every query is scoped to the caller's "
            "organization; detected: audit records of reads, and an alert on refused requests",
            "- Tampering: a caller changing someone else's record; prevented: ownership checked before each change; "
            "detected: audit records of changes"]
    return out


def _resource_lines(ctx: dict, repos: list[dict], provider: str, lines: list[str]) -> list[str]:
    """Stub judgement (VX-19): the AWS services the request names, owned by the provider, with an
    ADR each and a worker reading any stream."""
    types = (ctx.get("aws_catalog") or {}).get("types") or {}
    text = f"{ctx.get('title', '')} {ctx.get('description', '')}"
    wanted = [(k, t) for k, t in (("DynamoDB", "dynamodb-table"), ("Kinesis", "kinesis-stream"), ("SQS", "sqs-queue"),
                                  ("S3", "s3-bucket")) if re.search(rf"\b{k}\b", text) and t in types]
    if not wanted:
        return []
    record = _slug(ctx["title"]).split("-", 1)[0] or "record"
    owner = provider.split("/", 1)[-1]
    workers = [r["name"].split("/", 1)[-1] for r in repos if r["role"] == "event-worker"]
    out, adrs = ["", "## Resources"], []
    for i, (label, kind) in enumerate(wanted, start=1):
        name = f"{re.sub(r'[^a-z0-9]+', '-', record.lower()).strip('-')}-{kind.split('-')[0]}"
        params = {"dynamodb-table": "; partition_key=id", "kinesis-stream": "; retention_hours=168"}.get(kind, "")
        out.append(f"- {name}: {kind}{params}")
        out.append(f"  writes, reads: {owner}" if kind != "kinesis-stream" or not workers else
                   f"  writes: {owner}; reads: {', '.join(workers)}")
        adrs.append(f"- ADR-{ctx['feature_id']}-R{i}: Keep {name} in {label} ({kind}): the request needs it and the "
                    "platform defaults don't serve that access pattern")
    at = lines.index("## ADRs") + 2 if "## ADRs" in lines else len(lines)
    lines[at:at] = adrs
    return out


def _minor_bump(version: str) -> str:
    from ..knowledge import semver

    v = semver(version)
    return f"{v[0]}.{v[1] + 1}.0" if v else version


def _node_id(repo: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", repo.split("/", 1)[-1]).strip("_")


def _flowchart(ctx: dict, repos: list[dict]) -> str:
    """Container view: one subgraph per lane, consumers call providers, providers use libraries,
    agents use MCP servers (and models through the gateway), MCP servers call providers."""
    lane_names = {ctx["product_id"]: ctx["product_name"]}
    by_lane: dict[str, list[dict]] = {}
    for r in repos:
        by_lane.setdefault(r.get("lane", ctx["product_id"]), []).append(r)
    out = ["flowchart LR"]
    for lane, members in by_lane.items():
        out.append(f'  subgraph {lane}["{lane_names.get(lane, lane)}"]')
        out += [f'    {_node_id(r["name"])}["{r["name"].split("/", 1)[-1]} ({r["role"]})"]' for r in members]
        out.append("  end")
    providers = [r for r in repos if r["role"] == "provider"]
    for r in repos:
        if r["role"] == "consumer":
            out += [f'  {_node_id(r["name"])} -->|"versioned API"| {_node_id(p["name"])}' for p in providers]
        elif r["role"] == "library":
            out += [f'  {_node_id(p["name"])} -->|"uses"| {_node_id(r["name"])}' for p in providers]
        elif r["role"] == "agent":
            out += [f'  {_node_id(r["name"])} -->|"MCP tools"| {_node_id(m["name"])}'
                    for m in repos if m["role"] == "mcp-server"]
            out.append(f'  {_node_id(r["name"])} -->|"models"| llm_gateway["LLM gateway"]')
        elif r["role"] == "mcp-server":
            out += [f'  {_node_id(r["name"])} -->|"versioned API"| {_node_id(p["name"])}' for p in providers]
        elif r["role"] == "event-worker":
            out += [f'  {_node_id(p["name"])} -->|"events (Kafka)"| {_node_id(r["name"])}' for p in providers]
        elif r["role"] == "mobile":
            out += [f'  {_node_id(r["name"])} -->|"/mobile/api (bearer)"| {_node_id(b["name"])}'
                    for b in repos if b["role"] == "bff"]
    return "\n".join(out)


def plan(ctx: dict, defective: bool) -> str:
    acs = list(parse_acs(ctx["parent_texts"].get("PRD", "")))
    repos = parse_affected_repos(ctx["parent_texts"].get("HLD", "")) or ctx["repos"]
    slug = _slug(ctx["title"])
    risky = "auth" in (ctx.get("description", "") + ctx["title"]).lower()
    from .. import acceptance

    acc = acceptance.acceptance_repo(repos)
    if acc and not acceptance.needed(repos, ctx.get("acceptance_min_repos", 2)):
        repos = [r for r in repos if r.get("role") != "acceptance"]  # one repo: its own tests suffice
        acc = None
    builders = [r for r in repos if r.get("role") != "acceptance"] or repos
    tasks = []
    for i, repo in enumerate(repos, start=1):
        files = {
            "provider": [f"src/api/{slug}.py", f"tests/test_{slug}.py"],
            "consumer": [f"src/ui/{slug}.tsx", f"tests/{slug}.test.tsx"],
            "library": [f"contracts/{slug}.yaml"],
            "mcp-server": [f"src/mcp_server/tools/{slug}.py", f"tests/test_{slug}_tools.py"],
            "agent": [f"src/agent/prompts/{slug}.md", f"evals/cases/{slug}.jsonl"],
            "event-worker": [f"src/main/java/com/vrixel/worker/{slug.replace('-', '')}/Handler.java",
                             f"src/main/resources/db/migration/V2__{slug.replace('-', '_')}.sql"],
            "acceptance": [f"tests/{slug}.spec.ts", "catalog.json"],
        }.get(repo["role"], [f"src/{slug}.py"])
        if risky and repo["role"] == "provider":
            files.append("src/auth/policy.py")
        if repo.get("role") == "acceptance":
            mine = list(acs)  # the feature end to end: every criterion
        else:
            k = builders.index(repo)
            mine = [ac for j, ac in enumerate(acs) if j % len(builders) == k]
        task = {"id": f"TASK-{i}", "repo": repo["name"], "role": repo["role"], "acs": mine, "files": files}
        from .parse import SCREEN_ROLES, parse_screens

        screens = list(parse_screens(ctx["parent_texts"].get("UX", "")))
        if screens and repo.get("role") in SCREEN_ROLES:
            task["screens"] = screens
        if not mine:  # more repos than criteria: this one supports the others
            task["why"] = f"supports {', '.join(acs) or 'the feature'} through {repo['role']} changes the others use"
        tasks.append(task)
    nfrs = parse_nfrs(ctx["parent_texts"].get("PRD", ""))
    acs_text = parse_acs(ctx["parent_texts"].get("PRD", ""))
    trace, cases = [], []
    for ac in acs:
        owner = next((t for t in tasks if ac in t["acs"] and t["role"] != "acceptance"), None) or tasks[0]
        trace.append(f"- {ac} -> {owner['id']}")
        test_file = next((f for f in owner["files"] if "test" in f or ".spec." in f), f"tests/test_{slug}.py")
        name = f"test_{ac.lower().replace('-', '')}"
        cases.append(f"- TC-{len(cases) + 1} ({ac}, happy): {acs_text.get(ac, '')[:80]} -> {owner['id']} {test_file}::{name}_happy")
        cases.append(f"- TC-{len(cases) + 1} ({ac}, error): {ac} with bad input or a failing dependency"
                     f" -> {owner['id']} {test_file}::{name}_error")
    builder = next((t for t in tasks if t["role"] != "acceptance"), tasks[0] if tasks else None)
    for nfr, spec in nfrs.items():
        if builder:
            trace.append(f"- {nfr} -> {builder['id']}")
        suite = {"performance": "performance", "accessibility": "e2e", "security": "sast"}.get(spec["kind"], "integration")
        cases.append(f"- TC-{len(cases) + 1} ({nfr}, {spec['kind']}): {spec['text'][:80]} -> suite {suite}")
    from .parse import parse_threats

    authz = {a["repo"] for a in parse_threats(ctx["parent_texts"].get("HLD", ""))["authorization"]}
    for t in tasks:  # each component the design authorizes: a caller without access is refused
        if t["repo"] in authz and acs and t["role"] != "acceptance":
            ac = (t["acs"] or acs)[0]
            test_file = next((f for f in t["files"] if "test" in f or ".spec." in f), f"tests/test_{slug}.py")
            cases.append(f"- TC-{len(cases) + 1} ({ac}, permission): a caller without access is refused"
                         f" -> {t['id']} {test_file}::test_{ac.lower().replace('-', '')}_refused")
    if defective and trace:
        trace = trace[:-1]
    lines = [
        f"# Plan: {ctx['title']}",
        "", "## Tasks", "```json", json.dumps({"tasks": tasks}, indent=2), "```",
        "", "## Traceability", *trace,
        "", "## Test cases", *cases,
    ]
    if acc:
        lines += ["", "## Acceptance tests", *_acceptance_lines(ctx, repos, acs, slug)]
    lines += ["", "## Agent notes", "- Assumed: each repo's existing tests keep running in CI as they do today"]
    return "\n".join(lines + _feedback(ctx)) + "\n"


def coverage_lines(ctx: dict, where: str, nfrs: bool = False) -> list[str]:
    """One line per PRD acceptance criterion (and NFR, for the HLD): where this document handles it."""
    prd = (ctx.get("parent_texts") or {}).get("PRD", "")
    reqs = list(parse_acs(prd)) + (list(parse_nfrs(prd)) if nfrs else [])
    return [f"- {r}: {where}" for r in reqs] or ["- None: no acceptance criteria yet"]


def _endpoint_lines(repos: list[dict]) -> list[str]:
    """Stub judgement (VX-17): only the BFF is external; each component calls what its role uses."""
    short = {r["name"]: r["name"].split("/", 1)[-1] for r in repos}
    by_role: dict[str, list[str]] = {}
    for r in repos:
        by_role.setdefault(r["role"], []).append(short[r["name"]])
    calls = {
        "bff": by_role.get("provider", []) + by_role.get("consumer", []) + by_role.get("agent", []),
        "agent": by_role.get("mcp-server", []),
        "mcp-server": by_role.get("provider", []),
        "consumer": [],
        "mobile": by_role.get("bff", []),
    }
    out = []
    for r in repos:
        if r["role"] in ("library", "acceptance"):
            continue
        vis = {"bff": "external", "event-worker": "none", "mobile": "none"}.get(r["role"], "project")
        targets = calls.get(r["role"], [])
        out.append(f"- {r['name']}: {vis}" + (f"; calls {', '.join(targets)}" if targets else ""))
    return out


def _acceptance_lines(ctx: dict, repos: list[dict], acs: list[str], slug: str) -> list[str]:
    """Stub judgement: extend the catalog's test when it already covers this feature's routes,
    else add one test per criterion that says what it covers."""
    from .. import acceptance

    provider = next((r["name"] for r in repos if r["role"] == "provider"), None)
    covers = [f"{acceptance.component(provider)} PUT /api/v1/{slug}/{{id}}"] if provider else \
             [f"{acceptance.component(r['name'])} page /{slug}" for r in repos if r["role"] == "consumer"][:1]
    existing = acceptance.overlapping(covers, "", ctx.get("acceptance_catalog") or [])
    out = []
    for ac in acs:
        if existing:
            t = existing[0]
            out.append(f'- {ac}: extend {t["file"]} "{t["title"]}"')
        else:
            out.append(f"- {ac}: add {slug}/{ac.lower()}.spec.ts covers: {', '.join(covers) or slug}")
    return out


def code_task(ctx: dict, task: dict, passed: bool) -> str:
    return "\n".join([
        f"## Patch for {task['id']}", f"Repo: {task['repo']}", f"Branch: feature/{ctx['feature_id']}",
        "Files:", *[f"- {f}" for f in task["files"]],
        f"Acceptance criteria: {', '.join(task['acs']) or 'none'}",
        *([f"Feature flag: {ctx['flag']} (default off; tests cover on and off)"]
          if ctx.get("flag") and task.get("role") in ("provider", "consumer", "agent", "mcp-server") else []),
        *([f"Audit: {', '.join(a['action'] for a in ctx['audit'])} (ids only; fails closed)"]
          if ctx.get("audit") and task.get("role") in ("provider", "agent", "mcp-server") else []),
        *([f"Events: {', '.join(e['type'] for e in ctx['events'])} "
           + ("(published to Kafka first; no database write)" if task.get("role") == "provider"
              else "(applied once, in order)")]
          if ctx.get("events") and task.get("role") in ("provider", "event-worker") else []),
        *([f"Instrumentation: {', '.join(e['event'] for e in ctx['instrumentation'] if e.get('repo') == task['repo'])}"
           " (vrixel.events counter; no personal attributes)"]
          if any(e.get("repo") == task["repo"] for e in ctx.get("instrumentation") or []) else []),
        *([f"Acceptance: {', '.join(task['acs'])} (tagged @{ctx['feature_id']}, covers declared)"]
          if task.get("role") == "acceptance" else []),
        f"Tests: {'passed' if passed else 'failed: test_' + _slug(task['id'])}",
    ]) + "\n"


def stub_tests(cases: list[dict]) -> dict[str, str]:
    """The test files a stand-in coder would push for its planned cases, by path."""
    files: dict[str, list[str]] = {}
    for c in cases:
        line = (f"def {c['test']}():\n    assert True  # {c['id']} ({c['req']}, {c['kind']})\n" if c["file"].endswith(".py")
                else f"test('{c['test']}', () => {{ /* {c['id']} ({c['req']}, {c['kind']}) */ }});\n")
        files.setdefault(c["file"], []).append(line)
    return {path: "\n".join(lines) for path, lines in files.items()}


def code_result(ctx: dict, task: dict, passed: bool) -> str:
    """The coder's structured reply in stub mode (agents/structured.py CoderReply)."""
    return json.dumps({"summary": code_task(ctx, task, passed), "files": list(task["files"]),
                       "tests": {"command": "the repo's test command", "passed": passed,
                                 "failed": [] if passed else [f"test_{_slug(task['id'])}"]},
                       "notes": [f"Assumed: {task['repo']}'s public API stays compatible for its other callers"]})


def release(ctx: dict, defective: bool) -> str:
    flag = ctx.get("flag")
    rollback = ("- [ ] Rollback plan" if defective else
                "- [x] Rollback plan: " + ("flag off, then " if flag else "") + "revert in reverse merge order")
    comps = ctx.get("components") or []
    lines = [
        f"# Release: {ctx['title']}",
        "", "## Checklist",
        "- [x] CI green on every change-set PR",
        "- [x] Database migrations reviewed",
        f"- [x] Feature flag: {flag}, off in production until G6 or its launch" if flag else
        "- [x] No feature flag: this product has no flag service; G6 releases the change directly",
        rollback,
        "- [x] SLOs, alerts and dashboards defined (VX-8)",
        "", "## Staging",
        *[f"- {d['repo']}: {d['env']}, release {d.get('release') or 'n/a'}" for d in ctx.get("deployments", [])],
        *[f"- {c}: release-candidate package" for c in comps if c not in {d['repo'] for d in ctx.get("deployments", [])}],
        *_quality_lines(ctx),
    ]
    return "\n".join(lines + _feedback(ctx)) + "\n"


def _quality_lines(ctx: dict) -> list[str]:
    """Evidence for G6: each suite that ran, by stage, and any accepted risks."""
    out = []
    for stage in ("pr", "build", "dev", "staging"):
        for r in (ctx.get("quality") or {}).get(stage, {}).get("suites", []):
            bits = [r["state"]]
            if r.get("passed") is not None:
                bits.append(f"{r['passed']} passed, {r.get('failed') or 0} failed")
            if r.get("coverage") is not None:
                bits.append(f"coverage {r['coverage']:.0f}%")
            if r["kind"] == "security":
                bits.append(f"{len(r.get('findings') or [])} findings, {len(r.get('waived') or [])} accepted")
            out.append(f"- {stage} / {r['suite']}{' / ' + r['repo'].split('/')[-1] if r.get('repo') else ''}: "
                       + "; ".join(bits))
    if not out:
        return []
    lines = ["", "## Quality evidence", *out]
    if ctx.get("waivers"):
        lines += ["", "## Accepted risks"] + [
            f"- {w['severity']} {w['title']} ({w['repo']}): {w['reason']}; accepted by {w['approver']} until "
            f"{w['expires'][:10]}" for w in ctx["waivers"]]
    return lines


def operate(ctx: dict, defective: bool) -> str:
    cap = f"CAP-{ctx['product_id']}-{_slug(ctx['title']).upper()[:24]}"
    lines = [f"# Operating report: {ctx['title']}"]
    if not defective:
        lines += ["", "## Metrics vs brief", "- M1: baseline captured; first read in 7 days",
                  "- M2: baseline captured; first read in 7 days"]
    lines += ["", "## Capability", f"- Registered {cap}",
              "", "## New intakes", "- None yet; Operate re-checks metrics on its next run"]
    return "\n".join(lines + _feedback(ctx)) + "\n"


PHASE_TEMPLATES = {
    "discovery": discovery,
    "spec": spec,
    "design": design,
    "architecture": architecture,
    "plan": plan,
    "release": release,
    "operate": operate,
}

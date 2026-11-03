"""Deterministic stub outputs per phase. In stub mode these stand in for model output so the
whole pipeline runs offline; `defective=True` drops a required element so evaluator loops,
escalations and gates can be exercised. Live mode replaces them with real agent output."""

from __future__ import annotations

import json
import re

from ..repo_roles import REPO_ORDER as _REPO_ORDER
from .parse import parse_acs, parse_affected_repos



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
    caps = ctx.get("capabilities") or []
    cap = f"Extends: {caps[0]['id']} ({caps[0]['name']})" if caps else "New capability: no existing capability covers this"
    lines = [
        f"# Opportunity brief: {ctx['title']}",
        f"Feature: {ctx['feature_id']} · Product: {ctx['product_name']}",
        "", "## Problem", ctx.get("description") or ctx["title"],
        "", "## Capability", cap,
    ]
    if not defective:
        lines += ["", "## Success metrics",
                  f"- M1: Weekly active use of {ctx['title']} reaches [target] within 30 days",
                  "- M2: Support tickets for this workflow fall by [target]%"]
    lines += ["", "## Constitution articles", "- VX-6"]
    return "\n".join(lines + _feedback(ctx)) + "\n"


def spec(ctx: dict, defective: bool) -> str:
    title = ctx["title"]
    if defective:
        acs = [f"- AC-1: {title} works well", f"- AC-2: {title} is fast"]
    else:
        acs = [
            f"- AC-1: Given a signed-in user, when they complete {title}, then the change is saved and confirmed",
            f"- AC-2: Given invalid input, when they submit {title}, then a specific error is shown and nothing is saved",
            f"- AC-3: Given the action succeeds, when an auditor reviews it, then an audit record shows who, what and when",
        ]
    product_article = (ctx.get("product_articles") or ["VX-2"])[0]
    lines = [
        f"# PRD: {title}", f"Feature: {ctx['feature_id']}",
        "", "## Goals", f"- G1: Deliver {title} (measured by M1)",
        "", "## Non-goals", f"- Changes outside {ctx['product_name']} scope",
        "", "## Acceptance criteria", *acs,
        "", "## Constitution articles", "- VX-5", f"- {product_article}",
    ]
    return "\n".join(lines + _feedback(ctx)) + "\n"


def design(ctx: dict, defective: bool) -> str:
    lines = [
        f"# UX spec: {ctx['title']}",
        "", "## Screens", f"- S1: {ctx['title']} form", "- S2: Confirmation",
        "", "## States", "- Loading, empty, error, success",
        "", "## Handoff", "- Uses design-system tokens and existing form components",
    ]
    if not defective:
        lines += ["", "## Accessibility", "- WCAG 2.1 AA: 4.5:1 contrast, full keyboard path, labelled fields (VX-9)"]
    return "\n".join(lines + _feedback(ctx)) + "\n"


def _mentions(ctx: dict, repo_name: str) -> bool:
    """Stub impact analysis: a dependency repo joins when the request names what it provides,
    e.g. 'audit' selects vrixel-shared-services/ss-audit-framework. Live architects decide."""
    words = [w for w in repo_name.split("/", 1)[-1].split("-")[1:] if len(w) >= 5]
    text = f"{ctx['title']} {ctx.get('description', '')}".lower()
    return any(w in text for w in words)


_NEW_SERVICE = re.compile(r"\b(new|separate|dedicated) (micro)?service\b", re.I)


def _new_repo(ctx: dict) -> dict | None:
    """Stub design judgement: a request for a new or separate service gets its own repo. Live
    architects decide from the HLD's boundaries."""
    if not _NEW_SERVICE.search(f"{ctx['title']} {ctx.get('description', '')}"):
        return None
    own = ctx["repos"][0]["name"]
    org, first = own.split("/", 1)
    prefix = first.split("-", 1)[0]
    words = [w for w in re.findall(r"[a-z0-9]+", ctx["title"].lower()) if w not in ("a", "an", "the", "for", "of", "and", "to")]
    name = f"{org}/{prefix}-{'-'.join(words[:2]) or 'feature'}-service"
    return {"name": name, "role": "provider", "lane": ctx["product_id"]}


def architecture(ctx: dict, defective: bool) -> str:
    deps = [r for r in ctx.get("dependency_repos", []) if _mentions(ctx, r["name"])]
    new = _new_repo(ctx)
    known = {r["name"] for r in ctx["repos"]}
    if new and new["name"] in known:  # already provisioned for this feature (a revision of the HLD)
        new = None
    repos = sorted(ctx["repos"] + deps + ([new] if new else []),
                   key=lambda r: (_REPO_ORDER.get(r["role"], 9), r["name"]))
    own = sorted(ctx["repos"], key=lambda r: _REPO_ORDER.get(r["role"], 9))
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
        f"- ADR-{ctx['feature_id']}-1: Extend the {provider} API with a versioned endpoint (expand, then contract)",
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
            if api.get("version") else "OpenAPI minor version bump, backward compatible (VX-4)"),
        "", "## Diagram", "```mermaid", _flowchart(ctx, repos), "```",
        "", "## Constitution articles", "- VX-4", "- VX-8",
    ]
    return "\n".join(lines + _feedback(ctx)) + "\n"


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
    return "\n".join(out)


def plan(ctx: dict, defective: bool) -> str:
    acs = list(parse_acs(ctx["parent_texts"].get("PRD", "")))
    repos = parse_affected_repos(ctx["parent_texts"].get("HLD", "")) or ctx["repos"]
    slug = _slug(ctx["title"])
    risky = "auth" in (ctx.get("description", "") + ctx["title"]).lower()
    tasks = []
    for i, repo in enumerate(repos, start=1):
        files = {
            "provider": [f"src/api/{slug}.py", f"tests/test_{slug}.py"],
            "consumer": [f"src/ui/{slug}.tsx", f"tests/{slug}.test.tsx"],
            "library": [f"contracts/{slug}.yaml"],
            "mcp-server": [f"src/mcp_server/tools/{slug}.py", f"tests/test_{slug}_tools.py"],
            "agent": [f"src/agent/prompts/{slug}.md", f"evals/cases/{slug}.jsonl"],
        }.get(repo["role"], [f"src/{slug}.py"])
        if risky and repo["role"] == "provider":
            files.append("src/auth/policy.py")
        mine = [ac for j, ac in enumerate(acs) if j % len(repos) == i - 1]
        tasks.append({"id": f"TASK-{i}", "repo": repo["name"], "role": repo["role"], "acs": mine, "files": files})
    trace = []
    for j, ac in enumerate(acs, start=1):
        owner = next((t["id"] for t in tasks if ac in t["acs"]), tasks[0]["id"])
        trace.append(f"- {ac} -> {owner} / TEST-{j}")
    if defective and trace:
        trace = trace[:-1]
    lines = [
        f"# Plan: {ctx['title']}",
        "", "## Tasks", "```json", json.dumps({"tasks": tasks}, indent=2), "```",
        "", "## Traceability", *trace,
    ]
    return "\n".join(lines + _feedback(ctx)) + "\n"


def code_task(ctx: dict, task: dict, passed: bool) -> str:
    return "\n".join([
        f"## Patch for {task['id']}", f"Repo: {task['repo']}", f"Branch: feature/{ctx['feature_id']}",
        "Files:", *[f"- {f}" for f in task["files"]],
        f"Acceptance criteria: {', '.join(task['acs']) or 'none'}",
        f"Tests: {'passed' if passed else 'failed: test_' + _slug(task['id'])}",
    ]) + "\n"


def release(ctx: dict, defective: bool) -> str:
    flag = f"ff_{_slug(ctx['title'])}"
    rollback = "- [ ] Rollback plan" if defective else "- [x] Rollback plan: flag off, then revert in reverse merge order"
    comps = ctx.get("components") or []
    lines = [
        f"# Release: {ctx['title']}",
        "", "## Checklist",
        "- [x] CI green on every change-set PR",
        "- [x] Database migrations reviewed",
        f"- [x] Feature flag defined: {flag}",
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

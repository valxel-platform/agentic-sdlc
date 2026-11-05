"""Phase subgraphs (produce -> evaluate -> loop or finish) and the parent-graph node that
runs each one and turns its result into a FeatureState delta.

Each phase is its own compiled subgraph with private state, so evaluator loops stay local
and only refs, scores and usage flow back to the feature thread.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph
from langgraph.types import Send

from ..agents import PHASE_SPECS
from ..agents import templates
from ..agents.context import build_layers
from ..agents.parse import parse_affected_repos, parse_tasks
from ..config import ProductConfig
from ..provisioning import naming_policy
from ..evaluators import critique, run_checks
from ..evaluators.critic import parse_verdict
from ..gateway import CallTags
from ..repo_roles import REPO_ORDER
from ..runtime import AgentResult, AgentTask
from ..state import FeatureState, PhaseState




GATES_CONTEXT = "vrixel/gates"


def gates_status(services, prs, state: str, description: str) -> None:
    """Tell GitHub where each pull request stands in the pipeline's gates (status vrixel/gates,
    required on lane repos). Best effort: GitHub's view never blocks or breaks the pipeline."""
    for pr in prs:
        if not pr.get("head_sha") or pr.get("state") == "merged":
            continue
        try:
            services.github.set_status(repo=pr["repo"], sha=pr["head_sha"], context=GATES_CONTEXT,
                                       state=state, description=description, url=pr.get("url", ""))
        except Exception:
            pass


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def event(node: str, kind: str, detail: str = "", **extra) -> dict:
    return {"at": now(), "node": node, "kind": kind, "detail": detail, **extra}


# Phases whose agents design or build, and so need the existing design (decisions, contracts,
# capabilities). Release and operate work from the change set instead.
KNOWLEDGE_PHASES = ("discovery", "spec", "design", "architecture", "plan", "build")


def design_knowledge(services, snap) -> tuple[dict, str]:
    """The existing design as data (for checks and G4) and as a prompt section (for agents)."""
    from ..knowledge import render

    kn = getattr(services, "knowledge", None)
    if kn is None:
        return {}, ""
    pack = kn.collect(snap)
    return pack, render(pack, int(kn.cfg.settings.get("max_chars", 12000)))


def feature_ctx(services, state: FeatureState, phase: str) -> dict:
    """Read-only snapshot a phase needs; artifact bodies are loaded from the store."""
    spec = PHASE_SPECS[phase]
    snap = services.feature_snapshot(state)
    product, con = snap.product, snap.constitution
    arts = state.get("artifacts", {})
    parents = {k: services.artifacts.load(arts[k]) for k in spec.parents if k in arts}
    knowledge, knowledge_text = design_knowledge(services, snap) if phase in KNOWLEDGE_PHASES else ({}, "")
    return {
        "knowledge": knowledge,
        "knowledge_text": knowledge_text,
        "feature_id": state["feature_id"],
        "product_id": state["product_id"],
        "product_name": product.name,
        "governance": state.get("governance"),
        "title": state["title"],
        "description": state.get("description", ""),
        "data_class": product.data_class,
        "articles": list(con.article_ids),
        "product_articles": list(con.product_articles()),
        "constitution_version": con.version,
        "capabilities": services.catalog.lookup(state["product_id"]),
        "repos": [{"name": r.name, "role": r.role} for r in product.repos],
        "org": product.org,
        "related_features": list(state.get("related_features") or []),
        "not_duplicate_reason": state.get("not_duplicate_reason", ""),
        "repo_prefix": product.repo_prefix,
        # Repos of the lanes this product depends on (shared services, APX): they may join the
        # change set when the architecture needs them.
        "dependency_repos": [{"name": r.name, "role": r.role, "lane": lane_id}
                             for lane_id, s in snap.dependencies.items() for r in s.product.repos],
        "parent_texts": parents,
        "parent_ids": [arts[k]["id"] for k in spec.parents if k in arts],
        "feedback": state.get("feedback", {}).get(phase),
        "simulate_faults": dict(state.get("simulate_faults") or {}),
        "tasks": list(state.get("tasks") or []),
        "components": list((state.get("change_set") or {}).get("merge_order", [])),
        "deployments": sorted((state.get("deployments") or {}).values(), key=lambda d: d["repo"]),
        "quality": dict(state.get("quality") or {}),
        "repo_policy": (naming_policy(services.settings.repos, product) if services.settings.repos else None),
        "waivers": list(state.get("waivers") or []),
        # The feature's flag (created at G4): the coder builds behind it, the reviewer checks it.
        "flag": state.get("flag", ""),
        # What the approved design says gets audited (VX-15); the build must publish those records.
        "audit": _audit_actions(services, arts),
        # The events the approved design names (VX-16): providers publish them, workers apply them.
        "events": _design_events(services, arts),
    }


def _design_events(services, arts: dict) -> list[dict]:
    from ..agents.parse import parse_events

    return parse_events(services.artifacts.load(arts["HLD"])) if "HLD" in arts else []


def _audit_actions(services, arts: dict) -> list[dict]:
    from ..agents.parse import parse_audit

    return parse_audit(services.artifacts.load(arts["HLD"])) if "HLD" in arts else []


def call_agent(services, *, role: str, phase: str, ctx: dict, skills: tuple[str, ...], tail: str,
               stub_text: str | None, max_tokens: int = 4096) -> AgentResult:
    snap = services.snapshot(ctx["product_id"], ctx.get("governance"))
    product = snap.product
    runtime = services.runtime_for(role)
    missing = PHASE_SPECS[phase].required_capabilities - runtime.capabilities
    if missing:
        raise RuntimeError(f"Runtime {runtime.name} lacks {sorted(missing)} required by phase {phase}")
    layers = build_layers(
        role=role, skills=skills, constitution=snap.constitution, product=product,
        parent_texts=ctx.get("parent_texts", {}), tail=tail, product_context=snap.product_context,
        design_knowledge=ctx.get("knowledge_text", ""),
    )
    tags = CallTags(
        product_id=ctx["product_id"], feature_id=ctx["feature_id"], phase=phase, role=role,
        data_class=product.data_class, workspace=product.workspace, prompt_version=f"{phase}@1",
    )
    return runtime.run(AgentTask(role=role, phase=phase, tags=tags, layers=layers,
                                 stub_text=stub_text, max_tokens=max_tokens))


def _finish(s: PhaseState, findings: list[str], usage: dict) -> dict:
    spent = s.get("usage", {}).get("cost_usd", 0.0) + usage.get("cost_usd", 0.0)
    if not findings:
        return {"status": "passed", "reason": ""}
    if spent >= s["budget_remaining_usd"]:
        return {"status": "escalated", "reason": "token budget exhausted (VX-10)"}
    if s["attempt"] >= s["max_loops"]:
        return {"status": "escalated", "reason": f"{s['max_loops']} evaluator loops failed"}
    return {"status": "retry", "reason": ""}


def build_phase_subgraph(phase: str, services):
    spec = PHASE_SPECS[phase]
    template = templates.PHASE_TEMPLATES[phase]

    def produce(s: PhaseState) -> dict:
        attempt = s.get("attempt", 0) + 1
        ctx = {**s["ctx"], "prior_findings": s.get("findings") or []}
        defective = attempt <= int(ctx["simulate_faults"].get(phase, 0))
        tail = [f"Produce the {spec.kind} artifact for {ctx['feature_id']}: {ctx['title']}.",
                f"Request: {ctx.get('description', '')}"]
        if ctx.get("feedback"):
            tail.append(f"Human feedback to address: {ctx['feedback']}")
        if ctx["prior_findings"]:
            tail.append("Evaluator findings to fix:\n" + "\n".join(f"- {f}" for f in ctx["prior_findings"]))
        res = call_agent(services, role=spec.role, phase=phase, ctx=ctx, skills=spec.skills,
                         tail="\n".join(tail), stub_text=template(ctx, defective))
        return {"attempt": attempt, "draft": {"kind": spec.kind, "content": res.text},
                "usage": {"tokens": res.tokens, "cost_usd": res.cost_usd}}

    def evaluate(s: PhaseState) -> dict:
        content, ctx = s["draft"]["content"], s["ctx"]
        findings = run_checks(phase, content, ctx)  # deterministic checks first
        usage, score = {}, 0.0
        if not findings:
            v = critique(services, phase=phase, rubric_skills=spec.critic_skills, content=content, ctx=ctx)
            score, usage = v.score, {"tokens": v.tokens, "cost_usd": v.cost_usd}
            if score < s["threshold"]:
                findings = [f"Critic score {score:.2f} is below {s['threshold']:.2f}"] + v.notes
        return {"findings": findings, "score": score, "usage": usage, **_finish(s, findings, usage)}

    g = StateGraph(PhaseState)
    g.add_node("produce", produce)
    g.add_node("evaluate", evaluate)
    g.add_edge(START, "produce")
    g.add_edge("produce", "evaluate")
    g.add_conditional_edges("evaluate", lambda s: "produce" if s["status"] == "retry" else END, ["produce", END])
    return g.compile(name=f"phase_{phase}")


# Repos whose code runs where users reach it: their changes go behind the feature's flag.
FLAGGED_ROLES = ("provider", "consumer", "agent", "mcp-server")


def flag_instruction(ctx: dict, task: dict) -> str:
    if not ctx.get("flag") or task.get("role") not in FLAGGED_ROLES:
        return ""
    return (f" Put the new behavior behind feature flag {ctx['flag']}: read it through OpenFeature as a boolean"
            " with default false (the repo's flags module), and keep today's behavior when it is off. Tests"
            " cover both. Name the flag key in the patch summary.")


# Repos that serve or change records: they publish the audit records the HLD names (VX-15).
AUDITED_ROLES = ("provider", "agent", "mcp-server")


def audit_instruction(ctx: dict, task: dict) -> str:
    if not ctx.get("audit") or task.get("role") not in AUDITED_ROLES:
        return ""
    actions = "; ".join(f"{a['action']} ({a['record']})" for a in ctx["audit"])
    return (f" Publish an audit record with the repo's auditor for each of these that the task touches: {actions}."
            " Ids only, never field values; if the record can't be published the request fails. List the"
            " actions after 'Audit:' in the patch summary.")


def audit_findings(ctx: dict, results: dict) -> list[str]:
    """Deterministic: the actions the design says are audited are audited somewhere in the build."""
    wanted = [a["action"] for a in ctx.get("audit") or []]
    runtime = [r for r in results.values() if r.get("role") in AUDITED_ROLES and r.get("tests_passed")]
    if not wanted or not runtime:
        return []
    said = " ".join(r.get("summary") or "" for r in runtime)
    missing = [a for a in wanted if a not in said]
    return [f"No task publishes the audit records the HLD names: {', '.join(missing)} (VX-15)"] if missing else []


# Kafka first (VX-16): providers publish what changed, event workers keep the stores.
EVENT_ROLES = ("provider", "event-worker")


def events_instruction(ctx: dict, task: dict) -> str:
    role = task.get("role")
    if role not in EVENT_ROLES:
        return ""
    named = "; ".join(e["type"] for e in ctx.get("events") or [])
    if role == "provider":
        return (" Change records only by publishing one event per change with the repo's Events (Kafka first), and"
                " answer 202 once Kafka has it; never write a database in the request (VX-16)."
                + (f" The design's events: {named}." if named else "")
                + " List the event types after 'Events:' in the patch summary.")
    return (" Apply events with one EventHandler per type: an upsert keyed by the record's id that only moves"
            " forward (keep the Kafka position), a Flyway migration for any table, InvalidEventException for"
            " an event that can't be applied; never call services or publish from a handler (VX-16)."
            + (f" The design's events: {named}." if named else "")
            + " List the event types after 'Events:' in the patch summary.")


def events_findings(ctx: dict, results: dict) -> list[str]:
    """Deterministic: every event the design names is published or applied somewhere in the build."""
    wanted = [e["type"] for e in ctx.get("events") or []]
    built = [r for r in results.values() if r.get("role") in EVENT_ROLES and r.get("tests_passed")]
    if not wanted or not built:
        return []
    said = " ".join(r.get("summary") or "" for r in built)
    missing = [t for t in wanted if t not in said]
    return [f"No task publishes or applies the events the HLD names: {', '.join(missing)} (VX-16)"] if missing else []


def flag_findings(ctx: dict, results: dict) -> list[str]:
    """Deterministic: a feature with a flag must use it in at least one repo users reach."""
    flag = ctx.get("flag")
    runtime = [r for r in results.values() if r.get("role") in FLAGGED_ROLES and r.get("tests_passed")]
    if not flag or not runtime or any(flag in (r.get("summary") or "") for r in runtime):
        return []
    return [f"No task builds behind feature flag {flag}: gate the new behavior in "
            + ", ".join(sorted({r['repo'] for r in runtime}))]


def build_build_subgraph(services):
    """Build fans out one coder agent per task (Send), then a reviewer agent per PR."""
    spec = PHASE_SPECS["build"]

    def start(s: PhaseState) -> dict:
        return {"attempt": s.get("attempt", 0) + 1}

    def dispatch(s: PhaseState):
        done = {tid for tid, r in (s.get("task_results") or {}).items() if r["tests_passed"]}
        pending = [t for t in s["ctx"]["tasks"] if t["id"] not in done]
        if not pending:
            return "review"
        return [Send("code_task", {"task": t, "ctx": s["ctx"], "attempt": s["attempt"]}) for t in pending]

    def code_task(payload: dict) -> dict:
        task, ctx, attempt = payload["task"], payload["ctx"], payload["attempt"]
        first = ctx["tasks"][0]["id"] if ctx["tasks"] else None
        fail = attempt <= int(ctx["simulate_faults"].get("build", 0)) and task["id"] == first
        res = call_agent(
            services, role="coder", phase="build", ctx=ctx, skills=spec.skills,
            tail=(f"Implement {task['id']} in {task['repo']} on branch feature/{ctx['feature_id']}. "
                  f"Cover {', '.join(task['acs']) or 'the planned behavior'}; run tests; return the patch summary."
                  + flag_instruction(ctx, task) + audit_instruction(ctx, task) + events_instruction(ctx, task)),
            stub_text=templates.code_task(ctx, task, passed=not fail),
        )
        result = {**task, "branch": f"feature/{ctx['feature_id']}", "attempt": attempt,
                  "tests_passed": "Tests: passed" in res.text, "summary": res.text}
        return {"task_results": {task["id"]: result}, "usage": {"tokens": res.tokens, "cost_usd": res.cost_usd}}

    def review(s: PhaseState) -> dict:
        ctx, reviews, tokens, cost = s["ctx"], {}, 0, 0.0
        for tid, r in sorted((s.get("task_results") or {}).items()):
            if not r["tests_passed"] or tid in (s.get("reviews") or {}):
                continue
            # The change note puts the task's trace (ACs, files, tests) in the repo, on the branch the
            # PR merges; with stub agents it is also the commit that makes the PR possible.
            services.github.put_file(
                repo=r["repo"], branch=r["branch"], path=f".vrixel/changes/{ctx['feature_id']}/{tid}.md",
                content=f"# {ctx['feature_id']} {tid}: {ctx['title']}\n\n{r['summary']}",
                message=f"{ctx['feature_id']} {tid}: change note")
            pr = services.github.open_pr(
                repo=r["repo"], branch=r["branch"], title=f"{ctx['feature_id']} {tid}: {ctx['title']}",
                body=r["summary"], files=r["files"],
                labels=[f"change-set:CS-{ctx['feature_id']}", f"product:{ctx['product_id']}"],
            )
            gates_status(services, [pr], "pending", f"{ctx['feature_id']}: waiting for CI and the pipeline's gates")
            res = call_agent(
                services, role="reviewer", phase="build", ctx=ctx, skills=spec.critic_skills,
                tail=(f"Review {pr['url']} against the plan and constitution."
                      + (f" Check that what users see changes only when feature flag {ctx['flag']} is on, and that"
                         " the code reads it with default false." if ctx.get("flag") and r.get("role") in FLAGGED_ROLES else "")
                      + (" Check that every read and change of records publishes an audit record with ids only"
                         " (no field values), that the request fails when the record can't be published, and that"
                         " new calls keep the trace context (VX-8, VX-15)."
                         if ctx.get("audit") and r.get("role") in AUDITED_ROLES else "")
                      + (" Check that the change writes no database in a request and publishes one event per change"
                         " to Kafka, or, in an event worker, that each handler applies an event once and only moves"
                         " forward (VX-16)." if r.get("role") in EVENT_ROLES else "")
                      + f" Reply with JSON score and notes.\n\n{r['summary']}"),
                stub_text=json.dumps({"score": 0.9, "notes": []}), max_tokens=1024,
            )
            score, notes = parse_verdict(res.text)
            reviews[tid] = {"pr": pr, "score": score, "notes": notes, "role": r["role"]}
            tokens, cost = tokens + res.tokens, cost + res.cost_usd
        return {"reviews": reviews, "usage": {"tokens": tokens, "cost_usd": cost}}

    def evaluate(s: PhaseState) -> dict:
        results, reviews = s.get("task_results") or {}, s.get("reviews") or {}
        findings = [f"{t['id']}: tests failing in {t['repo']}" for t in s["ctx"]["tasks"]
                    if not results.get(t["id"], {}).get("tests_passed")]
        findings += [f"{tid}: review score {rv['score']:.2f} is below {s['threshold']:.2f}"
                     for tid, rv in reviews.items() if rv["score"] < s["threshold"]]
        findings += flag_findings(s["ctx"], results)
        findings += audit_findings(s["ctx"], results)
        findings += events_findings(s["ctx"], results)
        lines = [f"# Build: {s['ctx']['title']}", "", "## Pull requests"]
        lines += [f"- {rv['pr']['repo']}#{rv['pr']['number']} ({tid}) review {rv['score']:.2f}"
                  for tid, rv in sorted(reviews.items())]
        draft = {"kind": spec.kind, "content": "\n".join(lines) + "\n"}
        return {"findings": findings, "score": min([rv["score"] for rv in reviews.values()] or [0.0]),
                "draft": draft, **_finish(s, findings, {})}

    g = StateGraph(PhaseState)
    g.add_node("start", start)
    g.add_node("code_task", code_task)
    g.add_node("review", review)
    g.add_node("evaluate", evaluate)
    g.add_edge(START, "start")
    g.add_conditional_edges("start", dispatch, ["code_task", "review"])
    g.add_edge("code_task", "review")
    g.add_edge("review", "evaluate")
    g.add_conditional_edges("evaluate", lambda s: "start" if s["status"] == "retry" else END, ["start", END])
    return g.compile(name="phase_build")


def risk_tier(lane_of_repo: dict[str, ProductConfig], product: ProductConfig, tasks: list[dict]) -> str:
    """Regulated if this product, or any lane whose repos the change set touches, handles PHI or
    payments; high if a task changes a high-risk path of the lane that owns its repo."""
    lanes = [product] + [lane_of_repo[t["repo"]] for t in tasks if t.get("repo") in lane_of_repo]
    if any(lane.data_class in ("phi", "payments") for lane in lanes):
        return "regulated"
    for task in tasks:
        lane = lane_of_repo.get(task.get("repo"), product)
        if any(f.startswith(p) for f in task.get("files", []) for p in lane.high_risk_paths):
            return "high"
    return "low"


def touched_lanes(snap, repo_names: list[str]) -> list[str]:
    """Dependency lanes (not the feature's own) whose repos appear in the given list."""
    lanes = snap.lane_of_repo()
    own = snap.product.product_id
    return sorted({lanes[r].product_id for r in repo_names if r in lanes} - {own})


def make_phase_node(phase: str, services):
    sub = build_build_subgraph(services) if phase == "build" else build_phase_subgraph(phase, services)
    spec = PHASE_SPECS[phase]

    def node(state: FeatureState, config: RunnableConfig) -> dict:
        product = services.feature_snapshot(state).product
        spent = float(state.get("cost_usd", 0.0))
        budget = float(state.get("token_budget_usd") or product.budget_usd)
        if spent >= budget:
            reason = f"token budget exhausted: ${spent:.2f} of ${budget:.2f} (VX-10)"
            return {"phase_status": {phase: "escalated"},
                    "escalations": {phase: {"phase": phase, "reason": reason, "findings": []}},
                    "events": [event(phase, "escalated", reason)]}

        ctx = feature_ctx(services, state, phase)
        out = sub.invoke({
            "phase": phase, "ctx": ctx, "attempt": 0, "max_loops": product.max_loops,
            "threshold": product.critic_threshold, "budget_remaining_usd": budget - spent,
        }, config)

        status, usage = out["status"], out.get("usage") or {}
        delta: dict = {
            "phase_status": {phase: status},
            "scores": {phase: float(out.get("score", 0.0))},
            "findings": {phase: list(out.get("findings") or [])},
            "loop_count": {phase: int(out.get("attempt", 0))},
            "tokens_used": int(usage.get("tokens", 0)),
            "cost_usd": float(usage.get("cost_usd", 0.0)),
            "events": [event(phase, status, out.get("reason", ""), attempts=out.get("attempt", 0),
                             score=out.get("score", 0.0))],
        }
        if phase == "architecture" and status == "passed" and out.get("draft"):
            content, diagram, ev = publish_diagram(services, state, out["draft"]["content"])
            out["draft"]["content"] = content
            if diagram:
                delta["diagrams"] = [diagram]
            if ev:
                delta["events"].append(ev)
        if out.get("draft"):
            ref = services.artifacts.save(
                feature_id=state["feature_id"], kind=spec.kind, phase=phase, content=out["draft"]["content"],
                parents=ctx["parent_ids"], constitution_version=ctx["constitution_version"],
            )
            delta["artifacts"] = {spec.kind: ref}
            delta["trace_links"] = [[p, ref["id"]] for p in ctx["parent_ids"]]
        if status == "escalated":
            delta["escalations"] = {phase: {"phase": phase, "reason": out.get("reason", ""),
                                            "findings": list(out.get("findings") or [])}}
            return delta

        snap = services.feature_snapshot(state)
        if phase == "architecture":
            affected = [r["name"] for r in parse_affected_repos(out["draft"]["content"])]
            delta["lanes_touched"] = touched_lanes(snap, affected)
        elif phase == "plan":
            delta["tasks"] = parse_tasks(out["draft"]["content"])
        elif phase == "build":
            reviews = out.get("reviews") or {}
            delta["pull_requests"] = {
                f"{rv['pr']['repo']}#{rv['pr']['number']}": {**rv["pr"], "task_id": tid, "role": rv["role"],
                                                             "review_score": rv["score"]}
                for tid, rv in reviews.items()
            }
            repos = sorted({(rv["role"], rv["pr"]["repo"]) for rv in reviews.values()},
                           key=lambda x: (REPO_ORDER.get(x[0], 9), x[1]))
            delta["risk_tier"] = risk_tier(snap.lane_of_repo(), product, ctx["tasks"])
            delta["change_set"] = {"id": f"CS-{state['feature_id']}", "branch": f"feature/{state['feature_id']}",
                                   "merge_order": [r for _, r in repos], "merged": False}
        elif phase == "release":
            # Services deployed in a product runtime promote at G6; shared-lane packages publish instead.
            lanes = snap.lane_of_repo()
            deployed = sorted(r for r, d in (state.get("deployments") or {}).items()
                              if d.get("env") != (d.get("pipeline") or ["dev"])[-1])
            packages = [c for c in ctx["components"] if c in lanes and lanes[c].kind == "shared"]
            delta["release"] = {"components": deployed, "packages": packages,
                                "version": f"{state['feature_id']}", "staging": bool(deployed),
                                "production": False}
        elif phase == "operate":
            delta["events"] += _knowledge_writeback(services, state, ctx)
        return delta

    return node


def capability_id(state: FeatureState) -> str:
    return f"CAP-{state['product_id']}-{templates._slug(state['title']).upper()[:24]}"


def _knowledge_writeback(services, state: FeatureState, ctx: dict) -> list[dict]:
    """After a production release: the capability goes live in the catalog, and the decisions G4
    approved are proposed to the product's architecture record (a governance pull request)."""
    cap_id = capability_id(state)
    if state.get("track") == "hotfix":  # a fix is not a new capability; the incident's postmortem records it
        return [event("operate", "hotfix_released", (state.get("hotfix") or {}).get("incident_id", ""))]
    call_agent(services, role="knowledge", phase="operate", ctx=ctx, skills=("engineering:documentation",),
               tail=f"Summarize capability {cap_id} for the catalog.", stub_text=f"{cap_id}: {state['title']}",
               max_tokens=512)
    design = dict(state.get("design") or {})
    services.catalog.register({
        "id": cap_id, "product_id": state["product_id"], "name": state["title"], "status": "live",
        "feature": state["feature_id"], "adrs": design.get("adrs", []),
        "contracts": [c for c in design.get("contracts", []) if c.get("next")],
        "artifacts": [r["id"] for r in state.get("artifacts", {}).values()],
        "release": state.get("change_set", {}).get("id"),
    })
    events = [event("operate", "capability_registered", cap_id)]
    hld_ref = state.get("artifacts", {}).get(PHASE_SPECS["architecture"].kind)
    if hld_ref and getattr(services, "knowledge", None) is not None:
        from ..knowledge.writeback import propose_record_update

        try:
            pr = propose_record_update(services, {**state, "product_name": ctx.get("product_name")},
                                       services.artifacts.load(hld_ref), design)
            if pr:
                events.append(event("operate", "architecture_record_pr", pr["url"], files=pr["files"]))
        except Exception as exc:  # the record update is a proposal; never fail a released feature on it
            events.append(event("operate", "architecture_record_failed", f"{type(exc).__name__}: {exc}"))
    return events


PROVIDER_NAMES = {"lucid": "Lucid", "drawio": "draw.io"}


def publish_diagram(services, state: FeatureState, content: str) -> tuple[str, dict | None, dict | None]:
    """Publish the HLD's diagram with the product's diagram provider and link it from the HLD.
    Publishing never fails the phase: the Mermaid source in the HLD stays the reviewed record."""
    from ..agents.parse import section
    from ..integrations.diagrams import DiagramRequest, extract_mermaid

    snap = services.feature_snapshot(state)
    cfg = snap.product.tools.get("diagrams")
    if not cfg:
        return content, None, None
    provider = cfg["provider"]
    name = PROVIDER_NAMES.get(provider, provider)
    affected = [r["name"] for r in parse_affected_repos(content)]
    lanes = snap.lane_of_repo()
    tools = services.settings.tools
    blocked = sorted({lanes[r].product_id for r in affected
                      if r in lanes and not tools.allowed(lanes[r].data_class, provider)})
    if blocked:
        note = (f"Not published to {name}: the change touches {', '.join(blocked)}, whose data class may not "
                f"be sent to {name}. The diagram source above is the record.")
        return content.rstrip() + f"\n\n## Diagrams\n- {note}\n", None, event("architecture", "diagram_withheld", note)

    version = len(state.get("diagrams") or []) + 1
    own = sorted(snap.product.repos, key=lambda r: REPO_ORDER.get(r.role, 9))
    # Before a new product's first repos exist, the diagram is committed to its governance repo.
    repo = next((r.name for r in own if r.role == "provider"), own[0].name if own else
                services.settings.entry(snap.product.product_id).governance.full_name)
    req = DiagramRequest(
        feature_id=state["feature_id"], title=f"{state['feature_id']} {state['title']}: architecture v{version}",
        mermaid=extract_mermaid(section(content, "Diagram")), repo=repo, branch=f"feature/{state['feature_id']}",
        version=version, settings=cfg)
    publisher = services.diagrams.get(provider)
    try:
        if publisher is None:
            raise RuntimeError(f"no {provider} publisher is configured")
        ref = publisher.publish(req)
    except Exception as exc:  # the gate can still review the source; the next HLD version retries
        note = f"Publishing to {name} failed ({type(exc).__name__}: {exc}). The diagram source above is the record."
        return content.rstrip() + f"\n\n## Diagrams\n- {note}\n", None, event("architecture", "diagram_failed", note)

    links = [f"- [Architecture v{version} in {name}]({ref['url']})"]
    if ref.get("file_url"):
        links.append(f"- Source file: [{ref['file']}]({ref['file_url']})")
    ref = {**ref, "at": now()}
    return (content.rstrip() + "\n\n## Diagrams\n" + "\n".join(links) + "\n", ref,
            event("architecture", "diagram_published", ref["url"], provider=provider, version=version))

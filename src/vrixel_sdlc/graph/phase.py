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
from ..evaluators import critique, run_checks
from ..evaluators.critic import parse_verdict
from ..gateway import CallTags
from ..runtime import AgentResult, AgentTask
from ..state import FeatureState, PhaseState

REPO_ORDER = {"library": 0, "provider": 1, "consumer": 2}


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def event(node: str, kind: str, detail: str = "", **extra) -> dict:
    return {"at": now(), "node": node, "kind": kind, "detail": detail, **extra}


def feature_ctx(services, state: FeatureState, phase: str) -> dict:
    """Read-only snapshot a phase needs; artifact bodies are loaded from the store."""
    spec = PHASE_SPECS[phase]
    snap = services.snapshot(state["product_id"], state.get("governance"))
    product, con = snap.product, snap.constitution
    arts = state.get("artifacts", {})
    parents = {k: services.artifacts.load(arts[k]) for k in spec.parents if k in arts}
    return {
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
    }


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
                  f"Cover {', '.join(task['acs']) or 'the planned behavior'}; run tests; return the patch summary."),
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
            res = call_agent(
                services, role="reviewer", phase="build", ctx=ctx, skills=spec.critic_skills,
                tail=f"Review {pr['url']} against the plan and constitution. Reply with JSON score and notes.\n\n{r['summary']}",
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
        product = services.snapshot(state["product_id"], state.get("governance")).product
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

        snap = services.snapshot(state["product_id"], state.get("governance"))
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
            delta["events"].append(_knowledge_writeback(services, state, ctx))
        return delta

    return node


def _knowledge_writeback(services, state: FeatureState, ctx: dict) -> dict:
    """After a production release, register the capability in the catalog (Knowledge agent)."""
    cap_id = f"CAP-{state['product_id']}-{templates._slug(state['title']).upper()[:24]}"
    call_agent(services, role="knowledge", phase="operate", ctx=ctx, skills=("engineering:documentation",),
               tail=f"Summarize capability {cap_id} for the catalog.", stub_text=f"{cap_id}: {state['title']}",
               max_tokens=512)
    services.catalog.register({
        "id": cap_id, "product_id": state["product_id"], "name": state["title"], "status": "live",
        "artifacts": [r["id"] for r in state.get("artifacts", {}).values()],
        "release": state.get("change_set", {}).get("id"),
    })
    return event("operate", "capability_registered", cap_id)


PROVIDER_NAMES = {"lucid": "Lucid", "drawio": "draw.io"}


def publish_diagram(services, state: FeatureState, content: str) -> tuple[str, dict | None, dict | None]:
    """Publish the HLD's diagram with the product's diagram provider and link it from the HLD.
    Publishing never fails the phase: the Mermaid source in the HLD stays the reviewed record."""
    from ..agents.parse import section
    from ..integrations.diagrams import DiagramRequest, extract_mermaid

    snap = services.snapshot(state["product_id"], state.get("governance"))
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
    repo = next((r.name for r in own if r.role == "provider"), own[0].name)
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

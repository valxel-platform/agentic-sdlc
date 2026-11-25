"""Phase subgraphs (produce -> evaluate -> loop or finish) and the parent-graph node that
runs each one and turns its result into a FeatureState delta.

Each phase is its own compiled subgraph with private state, so evaluator loops stay local
and only refs, scores and usage flow back to the feature thread.
"""

from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime, timezone

from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph
from langgraph.types import Send, interrupt

from ..agents import PHASE_SPECS
from ..agents import templates
from ..agents.context import task_texts
from ..agents.parse import merge_sections, parse_affected_repos, parse_cases, parse_tasks
from .. import budgets, diary, loops, performance, retrieval, safe_mode
from .. import eval_case, telemetry
from ..config import ProductConfig
from ..provisioning import naming_policy
from ..template_contract import roles_with
from ..evaluators import critique, run_checks, weakening
from ..agents import formats, structured, untrusted
from ..agent_calls import NO_LESSON_ROLES, call_agent, handoff_text  # noqa: F401  (the core's; patched and read here)
from ..knowledge import design_knowledge  # the core's; inception reads it too
from ..graph_events import event, now  # the core's; kept importable from here
from ..repo_roles import REPO_ORDER
from ..runtime import AgentResult
from ..state import FeatureState, PhaseState

log = logging.getLogger(__name__)


GATES_CONTEXT = "valxel/gates"


def gates_status(services, prs, state: str, description: str) -> None:
    """Tell GitHub where each pull request stands in the pipeline's gates (status valxel/gates,
    required on lane repos). Best effort: GitHub's view never blocks or breaks the pipeline."""
    for pr in prs:
        if not pr.get("head_sha") or pr.get("state") == "merged":
            continue
        try:
            services.github.set_status(repo=pr["repo"], sha=pr["head_sha"], context=GATES_CONTEXT,
                                       state=state, description=description, url=pr.get("url", ""))
        except Exception:
            pass


# Phases whose agents design or build, and so need the existing design (decisions, contracts,
# capabilities). Release and operate work from the change set instead.
KNOWLEDGE_PHASES = ("discovery", "spec", "design", "architecture", "plan", "build")


def released_datasets(services, product_id: str) -> list[dict]:
    """The product's data contracts already released: its governance repo's datasets/*.yaml."""
    from ..datasets import load

    try:
        gov = services.settings.entry(product_id).governance
        src = services.governance.source
        sha = src.resolve(gov.org, gov.repo, gov.ref)
        paths = [p for p in src.list(gov.org, gov.repo, "datasets", sha) if p.endswith(".yaml")]
        return [c for c in (load(src.read(gov.org, gov.repo, p, sha, optional=True) or "") for p in paths) if c]
    except Exception:  # noqa: BLE001 - no governance repo, or no datasets yet: nothing released
        return []


def feature_ctx(services, state: FeatureState, phase: str) -> dict:
    """Read-only snapshot a phase needs; artifact bodies are loaded from the store."""
    spec = PHASE_SPECS[phase]
    snap = services.feature_snapshot(state)
    product, con = snap.product, snap.constitution
    arts = state.get("artifacts", {})
    parents = {k: services.artifacts.load(arts[k]) for k in spec.parents if k in arts}
    knowledge, knowledge_text = design_knowledge(services, snap) if phase in KNOWLEDGE_PHASES else ({}, "")
    goal_ctx = {}
    if phase in GOAL_PHASES:
        goals, initiative_goal = product_goals(services, state)
        goal_ctx = {"product_goals": goals, "initiative_goal": initiative_goal}
    return {
        **goal_ctx,
        "knowledge": knowledge,
        "knowledge_text": knowledge_text,
        # The data contracts already released (governance datasets/): the design builds on them.
        "datasets": released_datasets(services, state["product_id"]) if phase in KNOWLEDGE_PHASES else [],
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
        # What the person attached at intake: the request's evidence, for the phases that read it.
        "attachments_text": attachments_text(services, state, phase),
        "repo_prefix": product.repo_prefix,
        # Repos of the lanes this product depends on (shared services, PAY): they may join the
        # change set when the architecture needs them.
        # The product's other designs, and what G0 planned this one to build on: the architecture
        # lists what it depends on (T-105, T-106).
        **({"other_designs": services.design_facts(state["product_id"], exclude=state["feature_id"]),
            "planned_needs": services.planned_needs(state["feature_id"]),
            "depends_on": list((state.get("undo") or {}).get("depends_on") or [])}
           if phase in ("architecture", "build") and callable(getattr(services, "design_facts", None)) else {}),
        "dependency_repos": [{"name": r.name, "role": r.role, "lane": lane_id}
                             for lane_id, s in snap.dependencies.items() for r in s.product.repos],
        "parent_texts": parents,
        "parent_ids": [arts[k]["id"] for k in spec.parents if k in arts],
        "feedback": state.get("feedback", {}).get(phase),
        # After the change set merged, a fix for a failure in dev or staging goes on a branch of its own.
        "branch": state.get("fix_branch") or f"feature/{state['feature_id']}",
        "pilot": bool(state.get("pilot")),
        # Safe mode on this phase: a second critic beside the critic (valxel_sdlc.safe_mode).
        "safe_mode": safe_mode.phase_on(services, state["product_id"], phase),
        "config_variant": state.get("config_variant", ""),
        "simulate_faults": dict(state.get("simulate_faults") or {}),
        "reopened": list(state.get("reopened") or []),  # documents build has sent back (gates.reopen)
        "tasks": list(state.get("tasks") or []),
        "sessions": dict(state.get("sessions") or {}),
        "cases": list(state.get("cases") or []),
        "components": list((state.get("change_set") or {}).get("merge_order", [])),
        "deployments": sorted((state.get("deployments") or {}).values(), key=lambda d: d["repo"]),
        "quality": dict(state.get("quality") or {}),
        "repo_policy": (naming_policy(services.settings.repos, product) if services.settings.repos else None),
        "waivers": list(state.get("waivers") or []),
        # The feature's flag (created at G4): the coder builds behind it, the reviewer checks it.
        "flag": state.get("flag", ""),
        "flag_off": (state.get("undo") or {}).get("flag_off", ""),
        "flagged_roles": FLAGGED_ROLES,
        # What the approved design says gets audited (VX-15); the build must publish those records.
        "audit": _audit_actions(services, arts),
        # The events the approved design names (VX-16): providers publish them, workers apply them.
        "events": _design_events(services, arts),
        # The product events that show whether the feature moves its goal, and the repo recording each.
        "instrumentation": instrumentation_events(services, arts),
        # Product acceptance tests: what exists (so plans extend rather than duplicate) and when needed.
        "acceptance_catalog": _acceptance_catalog(services, product) if phase in ("plan", "build") else [],
        "acceptance_min_repos": int(((product.quality or {}).get("acceptance") or {}).get("min_repos", 2)),
        # AWS services (VX-19): what the platform offers, where the product runs them, what exists.
        "aws_catalog": dict(snap.aws_catalog or {}),
        "aws": (product.runtime or {}).get("aws"),
        "cloud": (product.runtime or {}).get("cloud"),
        "pipeline": list((product.runtime or {}).get("pipeline") or []),
        "aws_existing": _aws_existing(services, snap) if phase == "architecture" else [],
        # The approved design, for the coder's resource bindings.
        "hld_text": services.artifacts.load(arts["HLD"]) if phase == "build" and "HLD" in arts else "",
        # The product's performance budgets and rules (valxel_sdlc.performance).
        "performance": dict(product.performance or {}),
    }


def _aws_existing(services, snap) -> list[dict]:
    """The product's resources OpenChoreo already runs, so a design reuses rather than replaces them."""
    lister = getattr(services.openchoreo, "list_resources", None)
    runtime = snap.product.runtime or {}
    if not callable(lister) or not runtime.get("openchoreo_project") or not snap.aws_catalog:
        return []
    try:
        return list(lister(project=runtime["openchoreo_project"], namespace=runtime.get("namespace")))
    except Exception:  # an unreadable inventory means no reuse hints, never a stuck design
        return []


def _acceptance_catalog(services, product) -> list[dict]:
    from .. import acceptance

    repo = acceptance.acceptance_repo([{"name": r.name, "role": r.role} for r in product.repos])
    if not repo:
        return []
    try:
        return acceptance.load_catalog(services.github.get_file(repo=repo, path="catalog.json", ref=""))
    except Exception:  # an unreadable catalog means no reuse hints, never a stuck plan
        return []


GOAL_PHASES = ("discovery",)


def product_goals(services, state: dict) -> tuple[list[dict], str]:
    """The product's goals (roadmap/goals.yaml) and the goal of the feature's initiative, if any."""
    try:
        from ..roadmap.record import read_roadmap

        road = read_roadmap(services, state["product_id"])
    except Exception:
        return [], ""
    goals = [{"id": g["id"], "title": g.get("title", ""), "measure": g.get("measure", "")} for g in road["goals"]]
    iid = state.get("initiative_id") or ""
    goal = next((i.get("goal", "") for i in road["initiatives"] if i.get("id") == iid), "") if iid else ""
    return goals, goal


def instrumentation_events(services, arts: dict) -> list[dict]:
    """The PRD's events, each with the repo the HLD says records it."""
    from ..agents.parse import parse_emitters, parse_instrumentation

    if "PRD" not in arts:
        return []
    events = parse_instrumentation(services.artifacts.load(arts["PRD"]))
    emits = parse_emitters(services.artifacts.load(arts["HLD"])) if "HLD" in arts else {}
    return [{**e, "repo": emits.get(e["event"], "")} for e in events]


def _design_events(services, arts: dict) -> list[dict]:
    from ..agents.parse import parse_events

    return parse_events(services.artifacts.load(arts["HLD"])) if "HLD" in arts else []


def _audit_actions(services, arts: dict) -> list[dict]:
    from ..agents.parse import parse_audit

    return parse_audit(services.artifacts.load(arts["HLD"])) if "HLD" in arts else []


def resources_instruction(ctx: dict, task: dict) -> str:
    from ..resources import instruction

    return instruction(ctx, task)


# Roles that don't produce or review a phase's work: they get no lessons.


def sessions_instruction() -> str:
    from ..sessions import HANDOFF_INSTRUCTION

    return HANDOFF_INSTRUCTION


def session_record(services, ctx: dict, task: dict, res, handoff) -> dict | None:
    """What the next attempt on `task` needs to resume this session or pick up from it: the
    session, and its handoff saved as an artifact (kind HANDOFF-<task>)."""
    if not getattr(res, "session_id", "") and (handoff is None or handoff.empty()):
        return None
    ref = None
    if handoff is not None and not handoff.empty():
        ref = services.artifacts.save(
            feature_id=ctx["feature_id"], kind=f"HANDOFF-{task['id']}", phase="build",
            content=handoff.markdown(f"{ctx['feature_id']} {task['id']}"), parents=[],
            constitution_version=ctx.get("constitution_version", ""))
    return {"task": task["id"], "session_id": res.session_id, "role": res.role, "model": res.model,
            "runtime": res.runtime, "at": time.time(), "fill": round(float(res.context_fill or 0), 4),
            "resumed": bool(res.resumed), **({"handoff": dict(ref)} if ref else {})}


def for_task(ctx: dict, task: dict, where: str = "") -> dict:
    """The build context with the feature's documents cut down to what this task needs (L5,
    agents.context.task_texts)."""
    return {**ctx, "parent_texts": task_texts(ctx.get("parent_texts") or {}, task, where)}


def feature_folder(box, parent_texts: dict) -> str:
    """The feature's full documents in the session's home folder, outside the repo checkout, so a
    coder can read what its prompt left out. Returns how the prompt names the folder."""
    home = getattr(box, "home", "")
    if not home or not parent_texts:
        return ""
    from pathlib import Path

    folder = Path(home) / "feature"
    folder.mkdir(parents=True, exist_ok=True)
    for kind, text in parent_texts.items():
        (folder / f"{kind}.md").write_text(text, encoding="utf-8")
    return "~/feature/"


def product_data_class(services, ctx: dict) -> str:
    return services.snapshot(ctx["product_id"], ctx.get("governance")).product.data_class


def cases_instruction(cases: list[dict]) -> str:
    if not cases:
        return ""
    return (" Write these planned test cases as tests with exactly these names (the release checks they"
            " exist and pass), and run them: " + "; ".join(
                f"{c['id']} ({c['req']}, {c['kind']}): {c['what']} -> {c['file']}::{c['test']}" for c in cases) + ".")


def written_cases(github, repo: str, branch: str, cases: list[dict]) -> dict[str, dict]:
    """Whether each case's test is on the branch: its file exists and names the test."""
    out, files = {}, {}
    for c in cases:
        if c["file"] not in files:
            try:
                files[c["file"]] = github.get_file(repo=repo, path=c["file"], ref=branch) or ""
            except Exception:
                files[c["file"]] = None  # unreadable now: not counted as missing
        text = files[c["file"]]
        found = None if text is None else bool(re.search(rf"\b{re.escape(c['test'])}\b", text))
        out[c["id"]] = {"req": c["req"], "file": c["file"], "test": c["test"], "found": found is not False}
    return out


def missing_cases(result: dict) -> list[str]:
    return [cid for cid, c in sorted((result.get("cases") or {}).items()) if not c.get("found")]


def blocking_of(review: dict | None) -> list[str]:
    if not review or review.get("approve", True):
        return []
    return list(review.get("blocking") or []) + [f"weakens a check: {w}" for w in review.get("weakened") or []]


def review_evidence(result: dict, weakened: list[str]) -> str:
    """What the reviewer must weigh besides the diff: the coder's own test run, quoted, and anything
    the change weakens."""
    tests = result.get("tests") or {}
    ran = (f" The coder's test run: {'passed' if tests.get('passed') else 'failed'}"
           + (f", failing: {', '.join(tests['failed'][:10])}" if tests.get("failed") else "")
           + (f", error: {tests['error']}" if tests.get("error") else "") + "."
           " Quote it in your notes; a claim about tests or coverage with no run behind it is a finding.")
    if not weakened:
        return ran
    return ran + (" These changes weaken a check to make it pass; each is blocking unless the plan asked for it: "
                  + "; ".join(weakened) + ".")


def second_line(review: dict) -> str:
    so = review.get("second_opinion") or {}
    if so.get("skipped"):
        return f"; no second opinion ({so['skipped']})"
    if not so:
        return ""
    verdict = "approves" if so["approve"] else "blocks: " + "; ".join(so["blocking"])
    return f"; second opinion ({so['model']}) {verdict}" + ("" if so["counts"] else " (advisory)")


def merge_second_opinion(review: dict, res: AgentResult, role: str, model: str, *, blocking: bool) -> dict:
    """Add a second model's review of the same change. Its blocking findings block too when the
    reviewer's `second_opinion_blocking` is on; otherwise they are reported beside the review."""
    reply, error = structured.parse(res.text, structured.ReviewReply)
    found = list(reply.blocking) if reply else [f"The second opinion's reply didn't fit its schema: {error}"]
    approve = bool(reply and reply.approve and not reply.blocking)
    out = dict(review)
    out["second_opinion"] = {"role": role, "model": model, "approve": approve, "blocking": found,
                             "notes": list(reply.notes) if reply else [], "agree": approve == review.get("approve"),
                             "counts": blocking, "cost_usd": round(res.cost_usd, 4)}
    if blocking and not approve:
        out["approve"] = False
        out["blocking"] = list(review.get("blocking") or []) + [f"[{model}] {b}" for b in found]
        if reply:
            out["score"] = min(float(review.get("score", 1.0)), float(reply.score))
    return out


def _finish(s: PhaseState, findings: list[str], usage: dict, *, between: str = "writer and critic") -> dict:
    spent = s.get("usage", {}).get("cost_usd", 0.0) + usage.get("cost_usd", 0.0)
    history = list(s.get("finding_history") or [])
    kept = {"finding_history": history + [list(findings)]}
    if not findings:
        return {"status": "passed", "reason": "", **kept}
    if spent >= s["budget_remaining_usd"]:
        return {"status": "escalated", "reason": "token budget exhausted (VX-10)", **kept}
    stuck = loops.stall(history, findings, default=between)
    if stuck and s["attempt"] < s["max_loops"]:  # at the cap anyway: the cap's reason is as good
        return {"status": "escalated", "reason": loops.reason(stuck, s["attempt"]), "stall": stuck, **kept}
    if s["attempt"] >= s["max_loops"]:
        return {"status": "escalated", "reason": f"{s['max_loops']} evaluator loops failed", **kept}
    return {"status": "retry", "reason": "", **kept}


ATTACHMENT_PHASES = ("discovery", "spec", "design")  # the designer sees the screenshots people attach


def attachments_text(services, state: dict, phase: str) -> str:
    """The intake attachments, for discovery, spec and design (later phases work from their documents)."""
    store = getattr(services, "attachments", None)
    metas = state.get("attachments") or []
    if store is None or not metas or phase not in ATTACHMENT_PHASES:
        return ""
    return store.for_agents(state["feature_id"], metas)


def document_tail(phase: str, ctx: dict, previous: str = "") -> str:
    """The task for a document phase's agent: the request, the format contract, people's feedback,
    the findings to fix and, on a retry, the previous draft. The evals replay it (valxel_sdlc.evals)."""
    spec = PHASE_SPECS[phase]
    tail = [f"Produce the {spec.kind} artifact for {ctx['feature_id']}: {ctx['title']}.",
            "Request:\n" + untrusted.wrap("request", ctx.get("description", "")), formats.contract(phase)]
    if ctx.get("attachments_text"):
        tail.insert(2, ctx["attachments_text"])
    if phase == "design" and "Experience brief:" in (ctx.get("description") or ""):
        tail.append("The request has an experience brief from the person who asked. Follow it: the screen or flow "
                    "it names, the main task first, the look it asks for. Where it leaves a part to you, stay within "
                    "the product's current design, and list each such choice under Agent notes as 'Traded off:'.")
    if phase == "discovery" and "product_goals" in ctx:
        if ctx["product_goals"]:
            tail.append("The product's goals (roadmap/goals.yaml); name the one this feature moves under Product goal:\n"
                        + "\n".join(f"- {g['id']}: {g['title']} (measure: {g['measure']})" for g in ctx["product_goals"])
                        + (f"\nThe feature's initiative serves {ctx['initiative_goal']}." if ctx.get("initiative_goal") else ""))
        else:
            tail.append("The product has no goals in roadmap/goals.yaml yet: write '- None: <why>' under Product goal.")
    if ctx.get("feedback"):
        tail.append(f"Human feedback to address: {ctx['feedback']}")
    if ctx.get("prior_findings"):
        tail.append("Evaluator findings to fix:\n" + "\n".join(f"- {f}" for f in ctx["prior_findings"]))
    if previous:
        tail.append("Revise your previous draft (below) to fix the findings. Reply with only the sections you "
                    "change, each starting with its '## ' heading line; sections you leave out are kept as they "
                    "are.\n\n<previous_draft>\n" + previous + "\n</previous_draft>")
    if phase == "architecture" and ctx.get("aws_catalog"):
        from ..resources import catalog_guide

        tail.append(catalog_guide(ctx["aws_catalog"], ctx.get("data_class", "")))
        if ctx.get("aws_existing"):
            tail.append("AWS resources the product already has (reuse them; never change their keys): " + ", ".join(
                f"{r['name']} ({r.get('type', '')})" for r in ctx["aws_existing"]))
    return "\n".join(t for t in tail if t)


def build_phase_subgraph(phase: str, services):
    spec = PHASE_SPECS[phase]
    template = templates.PHASE_TEMPLATES[phase]

    def produce(s: PhaseState) -> dict:
        attempt = s.get("attempt", 0) + 1
        ctx = {**s["ctx"], "prior_findings": s.get("findings") or []}
        defective = attempt <= int(ctx["simulate_faults"].get(phase, 0))
        # A retry revises: the previous draft goes in, and only the sections that change come back.
        previous = (s.get("draft") or {}).get("content", "") if attempt > 1 else ""
        left = s["budget_remaining_usd"] - float((s.get("usage") or {}).get("cost_usd", 0.0))
        co = None
        if phase == "architecture":  # the architect reads the code, read-only (valxel_sdlc.workspace)
            from .. import workspace

            co = workspace.checkout(services, [r["name"] for r in ctx.get("repos") or []],
                                    key=f"{ctx['feature_id']}-architecture")
            ctx = {**ctx, "code_read": co or {}}
        try:
            with telemetry.labels(attempt=attempt):
                tail = document_tail(phase, ctx, previous)
                if co:
                    from .. import workspace

                    tail = workspace.describe(co) + "\n\n" + tail
                res = call_agent(services, role=spec.role, phase=phase, ctx=ctx, skills=spec.skills, tail=tail,
                                 stub_text=template(ctx, defective), budget_usd=left, cwd=(co or {}).get("root"),
                                 box=workspace.session_box(co) if co else None)
        finally:
            if co:
                from .. import workspace

                workspace.release(co)
        content = merge_sections(previous, res.text) if previous else res.text
        out = {"attempt": attempt, "draft": {"kind": spec.kind, "content": content},
               "usage": {"tokens": res.tokens, "cost_usd": res.cost_usd}}
        if phase == "architecture":
            out["ctx"] = {**s["ctx"], "code_read": {k: v for k, v in (co or {}).items() if k != "root"}}
        return out

    def evaluate(s: PhaseState) -> dict:
        with telemetry.labels(attempt=s.get("attempt", 0)):
            return _evaluate(s)

    def _evaluate(s: PhaseState) -> dict:
        content, ctx = s["draft"]["content"], s["ctx"]
        findings = run_checks(phase, content, ctx)  # deterministic checks first
        usage, score, verdict = {}, 0.0, {}
        if not findings:
            # The checklist decides; the critic's 0-1 score is kept with what it would have decided.
            v = critique(services, phase=phase, rubric_skills=spec.critic_skills, content=content, ctx=ctx)
            score, usage = v.score, {"tokens": v.tokens, "cost_usd": v.cost_usd}
            verdict = {"passed": v.passed, "items": v.items, "legacy_score": v.legacy_score,
                       "legacy_passed": v.legacy_score >= s["threshold"]}
            if (ctx.get("pilot") or ctx.get("safe_mode")) and "critic_shadow" in services.settings.roles:
                # Pilot, or safe mode on this phase: another critic judges the same document; its
                # verdict is kept and, in safe mode, shown at the gate.
                sv = critique(services, phase=phase, rubric_skills=spec.critic_skills, content=content, ctx=ctx,
                              role="critic_shadow")
                verdict["shadow"] = {"model": services.settings.role("critic_shadow").model, "passed": sv.passed,
                                     "agree": sv.passed == v.passed, "cost_usd": round(sv.cost_usd, 4)}
            if not v.passed:
                findings = v.notes or ["The critic's checklist isn't met"]
        return {"findings": findings, "score": score, "usage": usage, "critic": verdict, **_finish(s, findings, usage)}

    g = StateGraph(PhaseState)
    g.add_node("produce", produce)
    g.add_node("evaluate", evaluate)
    g.add_edge(START, "produce")
    g.add_edge("produce", "evaluate")
    g.add_conditional_edges("evaluate", lambda s: "produce" if s["status"] == "retry" else END, ["produce", END])
    return g.compile(name=f"phase_{phase}")


# Repos whose template reads the feature's flag (config/template-contract.yaml `flags`): their
# changes go behind it.
FLAGGED_ROLES = roles_with("flags")


def flag_instruction(ctx: dict, task: dict) -> str:
    if not ctx.get("flag") or task.get("role") not in FLAGGED_ROLES:
        return ""
    return (f" Put the new behavior behind feature flag {ctx['flag']}: read it through OpenFeature as a boolean"
            " with default false (the repo's flags module), and keep today's behavior when it is off. Tests"
            " cover both. Name the flag key in the patch summary, and add a line 'Flag off: <what the tests"
            " show users get with the flag off>'"
            + (f"; the architecture says: {ctx['flag_off']}" if ctx.get("flag_off") else "") + ".")


# Repos that serve or change records, whose template has an auditor (the contract's `audit`):
# they publish the audit records the HLD names (VX-15).
AUDITED_ROLES = roles_with("audit")


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


SECURITY = {
    "provider": (" Every endpoint declares its permission with @PreAuthorize (deny by default); every handler"
                 " that touches a record calls AccessPolicy.require; outgoing calls forward the caller's token"
                 " inside the product and exchange it across trust boundaries (app.outbound). workload.yaml"
                 " visibility and dependencies match the HLD's Endpoints section (VX-17)."),
    "event-worker": " No endpoint but health and metrics; Kafka as this worker's own identity (VX-17).",
    "mcp-server": (" Each tool requires its scope and checks the caller's access to the records it touches;"
                   " audit on behalf of the person in the token (VX-17)."),
    "knowledge": (" Every passage carries its record's tenant and allow list from knowledge.yaml, and every search"
                  " filters by the caller inside the query; row-level security (schema.sql) enforces the same."
                  " Never index a field the data class forbids, and keep excluded fields out (VX-17)."),
    "reporting": (" reporting.yaml keeps only the fields the data contracts list for this repo; tokenize or"
                  " generalize what a de-identified destination gets, never send a secret field, and set tenant on"
                  " every table that has one; reporting-admin check must pass. Reports read through"
                  " reporting_reader with row-level security; Kafka as this component's own identity (VX-17)."),
    "transform": (" Models read only the sources.yml the tool gives; every column declares its data class and"
                  " transform-check passes; marts are published only by the run (never by hand), readable by"
                  " reporting_reader with row-level security by tenant; the tokenize key and the database URL are"
                  " mounted files, never in SQL, profiles or logs (VX-17)."),
    "job": (" job.yaml's schedule and time zone are the HLD's Schedules line for this repo; the work covers"
            " run.window_start to run.window_end, is safe to run twice over the same window (upserts keyed by"
            " the item, or a record of what was sent), calls run.check() between items and changes nothing on"
            " a dry run. It reaches other components through their APIs or events, never their databases, as"
            " its own identity: run.call(<component>, ...), with JOB_SERVICES naming exactly the components"
            " the HLD's Endpoints say it calls and the client secret a mounted file; no other HTTP client"
            " and no fixed token (VX-17)."),
    "connector": (" Only /webhooks/<name> routes take outside calls: each verifies its signature over the timestamp"
                  " and raw body before reading it and dedupes by delivery id; the vendor client calls only"
                  " integration.yaml's allowed_hosts and never follows a redirect; secrets are mounted files,"
                  " never logged or put in errors. Events in and out are checked against their contracts; audit"
                  " integration.received and integration.sent by id. Kafka as this connector's own identity (VX-17)."),
    "agent": (" Call MCP servers only through token exchange for the person behind the step (audience and"
              " scope per server); never a fixed token (VX-13, VX-17). Never run model-written code in this"
              " pod (no exec, eval or subprocess on it): send it to the product's code sandbox's run_code."),
    "channel": (" Each /channels/<kind> route checks its provider's signature or token before reading the body;"
                " unlinked people get only a sign-in link; STOP, START and HELP are honoured; replies go only to"
                " linked people who haven't opted out; addresses are hashed to find and encrypted to reply, never"
                " logged, published or audited; provider secrets are mounted files (VX-15, VX-17)."),
    "document-intake": (" Every call checks the token before reading a body and the caller's tenant on every"
                        " document (another tenant's is 404); uploads go to quarantine by presigned POST and"
                        " are sized, sniffed and scanned before anything reads them; downloads are one-minute"
                        " links; nobody reviews their own upload; audit by document id only (VX-15, VX-17)."),
    "code-sandbox": (" run_code takes calls only from the agents the design names, with the sandbox:run scope;"
                     " every run gets a fresh sandbox, deleted after; the runner keeps no network, mounts,"
                     " secrets or service account (VX-17)."),
    "consumer": (" The app never holds a token: it calls the API on its own origin through the product's BFF"
                 " and sends X-XSRF-TOKEN on writes. Its endpoint is project-visible only (VX-17)."),
    "bff": (" One route per service the HLD lets it call, each with TokenRelay and the session cookie"
            " removed; the BFF is the only external endpoint (VX-17). With a mobile app in the product,"
            " the /mobile/api route checks bearer tokens for the product's audience (BFF_MOBILE_AUDIENCE) and"
            " the minimum app version (VX-18)."),
    "mobile": (" A public client: sign-in only through Session (PKCE, tokens in the Keychain or Keystore),"
               " calls only through the BFF's /mobile/api route, and every change through the outbox with its"
               " idempotency key so it survives no signal. Store builds stay installed for months: use only API"
               " shapes the provider already serves, and never PHI in logs, notifications or crash reports"
               " (VX-17, VX-18)."),
}


def security_instruction(ctx: dict, task: dict) -> str:
    """The role's rules, and who the approved design says may call this component."""
    from ..agents.parse import parse_threats

    rules = [a for a in parse_threats(ctx.get("hld_text", "") or (ctx.get("parent_texts") or {}).get("HLD", ""))
             ["authorization"] if a["repo"] == task.get("repo")]
    said = (" The design's authorization rules for this component, each enforced in code and tested by a refused "
            "caller: " + "; ".join(f"{a['endpoint'] or 'all endpoints'}: {a['rule']}" for a in rules) + ".") if rules else ""
    return SECURITY.get(task.get("role") or "", "") + said


def ux_instruction(ctx: dict, task: dict) -> str:
    """A web or mobile task builds the approved UX spec's screens: its states, copy and accessibility."""
    from ..agents.parse import SCREEN_ROLES, parse_screens

    ux = (ctx.get("parent_texts") or {}).get("UX", "")
    if task.get("role") not in SCREEN_ROLES or not ux:
        return ""
    screens = parse_screens(ux)
    mine = [f"{k} ({screens[k]})" for k in task.get("screens") or [] if k in screens] or \
        [f"{k} ({v})" for k, v in screens.items()]
    return (f" Build {', '.join(mine)} as the approved UX spec (the UX artifact) says: every state it lists, its copy"
            " word for word, and its accessibility notes. Where you must differ from it, say why under Agent notes"
            " as 'Changed:'.")


def acceptance_instruction(ctx: dict, task: dict) -> str:
    if task.get("role") != "acceptance":
        return ""
    return (" Write the feature's acceptance tests as the plan's Acceptance tests section says: extend or replace"
            " the named tests, add only what it lists. Use the repo's fixtures (api, events, eventually) and declare"
            f" each test with spec(): proves {{\"{ctx['feature_id']}\": [criteria]}} (keep earlier features' criteria"
            " on extended tests), its capability, and covers for every route, event and page it reaches (the"
            " fixtures fail a test that reaches more). Browser tests only when a web app changes (*.ui.spec.ts)."
            " Test through the components' APIs, events and pages, never their databases, with synthetic data"
            " only. Run npm run catalog. List the criteria after 'Acceptance:' in the patch summary.")


# Kafka first (VX-16): providers publish what changed, event workers keep the stores.
EVENT_ROLES = ("provider", "event-worker")


def contracts_instruction(ctx: dict, task: dict) -> str:
    """The data contracts this task's repo publishes or reads (datasets.py)."""
    from ..datasets import instruction, parse_hld

    return instruction(parse_hld(ctx.get("hld_text") or "")[0], task.get("repo", ""))


def schedule_instruction(ctx: dict, task: dict) -> str:
    """A job's schedule from the approved HLD, for its job.yaml (jobs.py)."""
    from ..jobs import ROLES, coder_instruction

    if task.get("role") not in ROLES:
        return ""

    return coder_instruction(ctx.get("hld_text") or "", task.get("repo", ""))


def pipelines_instruction(ctx: dict, task: dict) -> str:
    """A transform's sources.yml, warehouse and marts, or a reporting repo's table names a transform
    reads, from the approved HLD (pipelines.py)."""
    if task.get("role") not in ("transform", "reporting"):
        return ""
    from ..pipelines import coder_instruction

    return coder_instruction(ctx.get("hld_text") or "", task.get("repo", ""))


def pipelines_findings(services, ctx: dict, results: dict) -> list[str]:
    """Deterministic: a transform's branch stays within its Pipelines line; a reporting branch a
    transform reads names its tables."""
    if not any(r.get("role") in ("transform", "reporting") for r in results.values()):
        return []
    from ..pipelines import build_findings

    return build_findings(services.github, ctx.get("hld_text") or "", results)


def channels_instruction(ctx: dict, task: dict) -> str:
    """A channel repo's channel.yaml from the approved HLD (channels.py)."""
    if task.get("role") != "channel":
        return ""
    from ..channels import coder_instruction

    return coder_instruction(ctx.get("hld_text") or "", task.get("repo", ""), ctx.get("data_class") or "general")


def channels_findings(services, ctx: dict, results: dict) -> list[str]:
    """Deterministic: a channel repo's channel.yaml on its branch says no more than its HLD line."""
    if not any(r.get("role") == "channel" for r in results.values()):
        return []
    from ..channels import build_findings

    return build_findings(services.github, ctx.get("hld_text") or "", results, ctx.get("data_class") or "general")


def agent_tools_instruction(ctx: dict, task: dict) -> str:
    """An agent repo's agent.yaml from the approved HLD (agent_tools.py)."""
    if task.get("role") != "agent":
        return ""
    from ..agent_tools import coder_instruction

    return coder_instruction(ctx.get("hld_text") or "", task.get("repo", ""))


def agent_tools_findings(services, ctx: dict, results: dict) -> list[str]:
    """Deterministic: an agent repo's agent.yaml on its branch allows no more than its HLD line."""
    if not any(r.get("role") == "agent" for r in results.values()):
        return []
    from ..agent_tools import build_findings

    return build_findings(services.github, ctx.get("hld_text") or "", results)


def documents_instruction(ctx: dict, task: dict) -> str:
    """A document intake's intake.yaml from the approved HLD (documents.py)."""
    if task.get("role") != "document-intake":
        return ""
    from ..documents import coder_instruction

    return coder_instruction(ctx.get("hld_text") or "", task.get("repo", ""))


def documents_findings(services, ctx: dict, results: dict) -> list[str]:
    """Deterministic: an intake's intake.yaml on its branch takes no more than its HLD lines."""
    if not any(r.get("role") == "document-intake" for r in results.values()):
        return []
    from ..documents import build_findings

    return build_findings(services.github, ctx.get("hld_text") or "", results)


def sandbox_instruction(ctx: dict, task: dict) -> str:
    """A code sandbox's allowed agents from the approved HLD (sandboxes.py)."""
    if task.get("role") != "code-sandbox":
        return ""
    from ..sandboxes import coder_instruction

    return coder_instruction(ctx.get("hld_text") or "", task.get("repo", ""))


def integration_instruction(ctx: dict, task: dict) -> str:
    """A connector's integration.yaml from the approved HLD (connectors.py)."""
    if task.get("role") != "connector":
        return ""
    from ..connectors import coder_instruction

    return coder_instruction(ctx.get("hld_text") or "", task.get("repo", ""))


def integration_findings(services, ctx: dict, results: dict) -> list[str]:
    """Deterministic: a connector's integration.yaml on its branch allows no more than its HLD line."""
    if not any(r.get("role") == "connector" for r in results.values()):
        return []
    from ..connectors import build_findings

    return build_findings(services.github, ctx.get("hld_text") or "", results)


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


# How each template records a product event (valxel-platform/template-*: counter, summary line, cohorts).
EVENT_APIS = {
    "provider": 'ProductEvents.record("<name>") or record("<name>", Map.of("<category>", "<value>")) in Java',
    "event-worker": 'ProductEvents.record("<name>") in the handler',
    "agent": 'events.record("<name>", <category>="<value>") from agent.events',
    "mcp-server": 'events.record("<name>") from mcp_server.events',
    "knowledge": 'events.record("<name>") from knowledge.events',
    "consumer": 'track("<name>") from src/events.ts; names only, no attributes',
    "mobile": 'track("<name>") from src/events.ts; names only, no attributes',
}


def instrumentation_instruction(ctx: dict, task: dict) -> str:
    mine = [e for e in ctx.get("instrumentation") or [] if e.get("repo") == task.get("repo")]
    if not mine:
        return ""
    named = "; ".join(f"{e['event']} ({e['when']}"
                      + (f"; attributes {', '.join(e['properties'])}" if e["properties"] else "") + ")" for e in mine)
    how = EVENT_APIS.get(task.get("role") or "", EVENT_APIS["provider"])
    return (f" Record these product events where they happen, with the template's product-events API ({how}):"
            f" {named}. Never a person's name, contact details or ids as attributes. Tests prove each is"
            " recorded. List the event names after 'Instrumentation:' in the patch summary.")


def instrumentation_findings(ctx: dict, results: dict) -> list[str]:
    """Deterministic: every event the PRD names is recorded by the repo the HLD gave it."""
    by_repo: dict[str, list[str]] = {}
    for e in ctx.get("instrumentation") or []:
        if e.get("repo"):
            by_repo.setdefault(e["repo"], []).append(e["event"])
    out = []
    for repo, events in sorted(by_repo.items()):
        built = [r for r in results.values() if r.get("repo") == repo and r.get("tests_passed")]
        if not built:
            continue
        said = " ".join(r.get("summary") or "" for r in built)
        missing = [e for e in events if e not in said]
        if missing:
            out.append(f"{repo} doesn't record the events the PRD names: {', '.join(missing)} (outcome loop)")
    return out


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


def flag_off_findings(ctx: dict, results: dict) -> list[str]:
    """Deterministic (T-102): turning the flag off is how an incident undoes the feature, so the
    build shows what users get with it off, tested, in each repo users reach."""
    flag = ctx.get("flag")
    runtime = [r for r in results.values() if r.get("role") in FLAGGED_ROLES and r.get("tests_passed")]
    missing = sorted({r["repo"] for r in runtime if not re.search(r"^\s*Flag off\s*:", r.get("summary") or "",
                                                                     re.IGNORECASE | re.MULTILINE)})
    if not flag or not missing:
        return []
    return [(f"{', '.join(missing)}: the patch doesn't say what users get with flag {flag} off ('Flag off: ...'), "
             "and the tests must show it's today's behavior")]


def perf_pending(services, pr: dict, level: str) -> None:
    """Open the PR's perf-smoke status as pending, so the gate waits for the template's job (it runs
    on the label) instead of finding nothing; the job sets the result."""
    try:
        services.github.set_status(repo=pr["repo"], sha=pr["head_sha"], context="perf-smoke / report", state="pending",
                                   description=f"Performance smoke test owed ({level})")
    except Exception:  # best effort: the suite reports missing, and the agents are asked to run it
        pass


DESIGN_ISSUE = (" If the approved spec, UX design or architecture can't be built as written (a contract that"
                " can't hold, an endpoint the code can't offer, criteria that contradict each other), don't work"
                " around it: set design_issue with the document, its section, the problem and the evidence, and"
                " stop. People decide whether to reopen that document. Not for preferences, or for what the"
                " code can reasonably absorb.")


def coder_tail(ctx: dict, task: dict, fix: list[str], mine: list[dict], live: bool) -> str:
    """The coder's instructions for a task: the same for a build and for its eval replay
    (valxel_sdlc.code_evals)."""
    payload = {"fix": fix}
    return (f"Implement {task['id']} in {task['repo']} on branch {ctx.get('branch') or 'feature/' + ctx['feature_id']}. "
          + ("Your working directory is its checkout on that branch: commit there and push the branch "
             "(git push origin HEAD). You can't push anywhere else, and your shell reaches only the "
             "package registries and GitHub. " if live else "")
          + f"Cover {', '.join(task['acs']) or 'the planned behavior'}; run the tests and report what they said."
          + (f" Fix this first: {ctx['feedback']}" if ctx.get("feedback") else "")
          + (" The review blocked your last attempt; fix each of these without weakening any check: "
             + "; ".join(payload["fix"]) + "." if payload.get("fix") else "")
          + cases_instruction(mine)
          + flag_instruction(ctx, task) + audit_instruction(ctx, task) + events_instruction(ctx, task)
          + instrumentation_instruction(ctx, task)
          + ux_instruction(ctx, task)
          + acceptance_instruction(ctx, task) + security_instruction(ctx, task)
          + resources_instruction(ctx, task) + retrieval.instruction(ctx, task)
          + contracts_instruction(ctx, task) + schedule_instruction(ctx, task) + pipelines_instruction(ctx, task)
          + integration_instruction(ctx, task) + sandbox_instruction(ctx, task)
          + documents_instruction(ctx, task) + channels_instruction(ctx, task) + agent_tools_instruction(ctx, task)
          + " The summary is the PR description: what changed, and every flag, audit record and event"
            " type the change uses. " + diary.CODER + " " + sessions_instruction() + DESIGN_ISSUE
          + "\n\n" + structured.instruction(structured.CoderReply))


def stub_design_issue(ctx: dict, task: dict) -> dict | None:
    """Stand-in agents (tests, demos): simulate_faults {"design_issue": "<document>"} has the first
    task's coder find that document wrong, once (until it's been reopened)."""
    doc = ctx["simulate_faults"].get("design_issue")
    if not doc or doc in ctx.get("reopened", []) or not ctx["tasks"] or task["id"] != ctx["tasks"][0]["id"]:
        return None
    return {"document": doc, "section": "## Endpoints", "problem": f"{task['repo']} can't offer the endpoint as "
            "specified: the record it returns doesn't exist until the import finishes",
            "evidence": f"test_{task['id'].lower()}_returns_record fails: 404 before the import job runs"}


def released_events(services, product_id: str) -> list[str]:
    """Event types the product's released data contracts carry."""
    from ..datasets import events

    return sorted({e for c in released_datasets(services, product_id) for e in events(c)})


def code_dependencies(services, ctx: dict, results: dict) -> list[dict]:
    """The build uses another feature's tables, events or new repos that the design's Depends on
    features doesn't list (T-109): a design issue for the architecture, so it's listed (and the
    feature isn't released before that one, and goes off with it)."""
    from ..undo import code_uses

    others = ctx.get("other_designs") or []
    if not others:
        return []
    listed = set(ctx.get("depends_on") or [])
    found: dict[str, dict] = {}
    for tid, r in sorted(results.items()):
        if not r.get("tests_passed"):
            continue
        for path in r.get("files") or []:
            try:
                text = services.github.get_file(repo=r["repo"], path=path, ref=r.get("branch") or ctx.get("branch"))
            except Exception:  # a file it can't read adds nothing
                logging.getLogger(__name__).debug("%s/%s not read", r["repo"], path, exc_info=True)
                continue
            if not isinstance(text, str) or len(text) > 400_000:
                continue
            for fid, what in code_uses(text, others).items():
                if fid in listed:
                    continue
                hit = found.setdefault(fid, {"task": tid, "what": set(), "where": set()})
                hit["what"].update(what)
                hit["where"].add(f"{r['repo']}/{path}")
    title = {o["feature_id"]: o.get("title", "") for o in others}
    return [{"task": h["task"], "by": "the tool", "document": "architecture", "section": "Depends on features",
             "problem": (f"The build uses {', '.join(sorted(h['what']))} from {fid} ({title.get(fid, '')}), which "
                         "the design doesn't list: list it, so this feature isn't released before it and goes off "
                         "with it in an incident"),
             "evidence": ", ".join(sorted(h["where"]))[:500]} for fid, h in sorted(found.items())]


def design_issues(results: dict, reviews: dict) -> list[dict]:
    """What the coder or the reviewer said is wrong with the approved documents, per task."""
    out = []
    for tid in sorted(set(results) | set(reviews)):
        for by, src in (("coder", results.get(tid) or {}), ("reviewer", reviews.get(tid) or {})):
            if src.get("design_issue"):
                out.append({"task": tid, "by": by, **src["design_issue"]})
    return out


def build_build_subgraph(services):
    """Build fans out one coder agent per task (Send), then a reviewer agent per PR."""
    spec = PHASE_SPECS["build"]

    def start(s: PhaseState) -> dict:
        return {"attempt": s.get("attempt", 0) + 1}

    def dispatch(s: PhaseState):
        reviews = s.get("reviews") or {}
        # Done: tests pass, and the review (if any) didn't block. A blocked task goes back to its
        # coder with what blocked it, so a retry fixes the review's findings too.
        done = {tid for tid, r in (s.get("task_results") or {}).items()
                if r["tests_passed"] and (reviews.get(tid) or {}).get("approve", True) and not missing_cases(r)}
        pending = [t for t in s["ctx"]["tasks"] if t["id"] not in done]
        if not pending:
            return "review"
        results = s.get("task_results") or {}
        return [Send("code_task", {"task": t, "ctx": s["ctx"], "attempt": s["attempt"],
                                   # The task's last session: this run's attempt, or the feature's (a CI fix).
                                   "session": (results.get(t["id"]) or {}).get("session")
                                   or (s["ctx"].get("sessions") or {}).get(t["id"]),
                                   "fix": blocking_of(reviews.get(t["id"])) + [
                                       f"write the test for {c} under exactly the planned name"
                                       for c in missing_cases(results.get(t["id"]) or {})]}) for t in pending]

    def code_task(payload: dict) -> dict:
        task, ctx, attempt = payload["task"], payload["ctx"], payload["attempt"]
        mine = [c for c in ctx.get("cases") or [] if c.get("via") == "test" and c.get("task") == task["id"]]
        first = ctx["tasks"][0]["id"] if ctx["tasks"] else None
        fail = attempt <= int(ctx["simulate_faults"].get("build", 0)) and task["id"] == first
        with telemetry.labels(task=task["id"], attempt=attempt, repo=task["repo"]):
            res = _code(task, ctx, mine, fail, payload)
        return _coded(task, ctx, attempt, mine, fail, res)

    def _code(task, ctx, mine, fail, payload):
        from .. import sandbox
        from ..datasets import parse_hld, repo_files

        branch = ctx.get("branch") or f"feature/{ctx['feature_id']}"
        # The schemas this repo publishes or reads, from the approved contracts: the coder validates
        # against them, and at run time nothing reads GitHub.
        for path, content in repo_files(parse_hld(ctx.get("hld_text") or "")[0], task["repo"]).items():
            if services.github.get_file(repo=task["repo"], path=path, ref=branch) != content:
                services.github.put_file(repo=task["repo"], branch=branch, path=path, content=content,
                                         message=f"{ctx['feature_id']} {task['id']}: data contracts")
        # Its own folder: in live mode a checkout of this one repo, cloned with a token for it alone.
        with sandbox.session(services, repo=task["repo"], branch=branch) as box:
            return _code_in(task, ctx, mine, fail, payload, box)

    def _code_in(task, ctx, mine, fail, payload, box):
        live = services.settings.mode == "live"
        where = feature_folder(box, ctx.get("parent_texts") or {})
        fixes = ((f" Fix this first: {ctx['feedback']}" if ctx.get("feedback") else "")
                 + (" The review blocked your last attempt; fix each of these without weakening any check: "
                    + "; ".join(payload["fix"]) + "." if payload.get("fix") else ""))
        prev = payload.get("session")
        res = call_agent(
            services, role="coder", phase="build", ctx=for_task(ctx, task, where), skills=spec.skills, box=box.spec,
            task_id=task["id"], prev_session=prev, keep_transcript=True,
            # Resumed, the session already has the task and everything it read: only what to fix now.
            resume_tail=(f"Continue {task['id']} on the same branch, in a fresh checkout of it." + fixes
                         + " Run the tests again and reply in the same JSON format, handoff included.") if prev and fixes else "",
            tail=coder_tail(ctx, task, payload.get("fix") or [], mine, live),
            stub_text=templates.code_result(ctx, task, passed=not fail,
                                            design_issue=stub_design_issue(ctx, task)),
            schema=structured.schema(structured.CoderReply),
        )
        if payload.get("attempt") == 1 and not prev and not payload.get("fix"):
            keep_code_case("coder", ctx, task, res.text)
        return res

    def keep_code_case(phase: str, ctx: dict, task: dict, output: str, tail: str = "", pr_url: str = "") -> None:
        """A coder or reviewer eval case (valxel_sdlc.code_evals), labeled when the change merges."""
        if getattr(services, "evals", None) is None:
            return
        try:
            ctx = {**ctx, "data_class": product_data_class(services, ctx)}
            version = getattr(services.gateway, "config_version", "")
            services.evals.capture(eval_case.coder_case(ctx, task, output, config_version=version) if phase == "coder"
                                   else eval_case.reviewer_case(ctx, task, tail, pr_url, output, config_version=version))
        except Exception:
            log.warning("%s %s: %s eval case not kept", ctx.get("feature_id"), task.get("id"), phase, exc_info=True)

    def _coded(task, ctx, attempt, mine, fail, res):
        # The coder's own test run gates review; CI on the pull request is the final word (ci_wait).
        reply, error = structured.parse(res.text, structured.CoderReply)
        branch = ctx.get("branch") or f"feature/{ctx['feature_id']}"
        if services.settings.mode == "stub" and mine and not fail:  # stand-in agents push no code: the tests they'd write
            for path, content in templates.stub_tests(mine).items():
                services.github.put_file(repo=task["repo"], branch=branch, path=path, content=content,
                                         message=f"{ctx['feature_id']} {task['id']}: tests")
        found = written_cases(services.github, task["repo"], branch, mine)
        from ..datasets import drift, parse_hld

        stale = drift(parse_hld(ctx.get("hld_text") or "")[0], task["repo"],
                      services.github.get_file(repo=task["repo"], path="contracts/lock.yaml", ref=branch))
        result = {**task, "branch": ctx.get("branch") or f"feature/{ctx['feature_id']}", "attempt": attempt,
                  # What the coder says it changed decides the performance test; the plan's list otherwise.
                  "files": list(reply.files) if reply and reply.files else list(task.get("files") or []),
                  "tests_passed": bool(reply and reply.tests.passed),
                  # Each planned test case: is a test with its name on the branch (traceability.py)?
                  "cases": found,
                  # Unparseable: the start of what it said, not all of it (it can be a whole file).
                  "summary": reply.summary if reply else (res.text or "")[:2000],
                  "notes": diary.entries(list(reply.notes)) if reply else [],
                  "tests": reply.tests.model_dump() if reply else {"passed": False, "failed": [],
                                                                    "error": f"no structured result: {error}"}}
        if reply and reply.design_issue:
            result["design_issue"] = reply.design_issue.model_dump()
        if stale:  # the coder changed contracts/: it goes back, whatever its tests said
            result["tests_passed"] = False
            result["tests"] = {**result["tests"], "passed": False,
                               "error": "contracts/ differs from the approved data contracts: " + "; ".join(stale)}
        record = session_record(services, ctx, task, res, reply.handoff if reply else None)
        if record:
            result["session"] = record
        return {"task_results": {task["id"]: result}, "usage": {"tokens": res.tokens, "cost_usd": res.cost_usd}}

    def review(s: PhaseState) -> dict:
        ctx, reviews, tokens, cost = s["ctx"], {}, 0, 0.0
        token = None  # the reviewer's sessions are labelled with the task and attempt they review
        for tid, r in sorted((s.get("task_results") or {}).items()):
            telemetry.pop_labels(token)
            token = None
            earlier = (s.get("reviews") or {}).get(tid)
            if not r["tests_passed"] or (earlier and earlier.get("attempt") == r.get("attempt")):
                continue  # not ready for review, or this attempt has been reviewed
            token = telemetry.push_labels(task=tid, attempt=r.get("attempt"), repo=r["repo"])
            catalog_entry(services, ctx, r)
            # The change note puts the task's trace (ACs, files, tests) in the repo, on the branch the
            # PR merges; with stub agents it is also the commit that makes the PR possible.
            services.github.put_file(
                repo=r["repo"], branch=r["branch"], path=f".valxel/changes/{ctx['feature_id']}/{tid}.md",
                content=f"# {ctx['feature_id']} {tid}: {ctx['title']}\n\n{r['summary']}",
                message=f"{ctx['feature_id']} {tid}: change note")
            # The performance test this change needs (fixed rules); its label runs the PR's smoke test.
            perf = performance.decide(r["repo"], r.get("role", ""), r["files"], hld=ctx.get("hld_text", ""),
                                      perf=ctx.get("performance"))
            labels = [f"change-set:CS-{ctx['feature_id']}", f"product:{ctx['product_id']}"]
            pr = services.github.open_pr(
                repo=r["repo"], branch=r["branch"], title=f"{ctx['feature_id']} {tid}: {ctx['title']}",
                body=pr_body(r), files=r["files"],
                labels=labels + ([performance.label(perf.level)] if performance.label(perf.level) else []),
            )
            gates_status(services, [pr], "pending", f"{ctx['feature_id']}: waiting for CI and the pipeline's gates")
            if perf.level != "none":  # the smoke test is owed: pending until the template's job reports
                perf_pending(services, pr, perf.level)
            # What the change weakens (a skipped test, a lowered threshold), read from the branch itself.
            weakened = weakening.findings(services.github, repo=r["repo"], branch=r["branch"], files=r["files"])
            res = call_agent(
                services, role="reviewer", phase="build", ctx=for_task(ctx, r), skills=spec.critic_skills,
                tail=(review_tail := f"Review {pr['url']} against the plan and constitution."
                      + (f" Check that what users see changes only when feature flag {ctx['flag']} is on, and that"
                         " the code reads it with default false." if ctx.get("flag") and r.get("role") in FLAGGED_ROLES else "")
                      + (" Check that every read and change of records publishes an audit record with ids only"
                         " (no field values), that the request fails when the record can't be published, and that"
                         " new calls keep the trace context (VX-8, VX-15)."
                         if ctx.get("audit") and r.get("role") in AUDITED_ROLES else "")
                      + (" Check that the change writes no database in a request and publishes one event per change"
                         " to Kafka, or, in an event worker, that each handler applies an event once and only moves"
                         " forward (VX-16)." if r.get("role") in EVENT_ROLES else "")
                      + (" Check VX-17: no endpoint without a permission, record access checked, no token in the"
                         " browser or in fixed configuration, exposure as the HLD's Endpoints section says."
                         if r.get("role") in SECURITY else "")
                      + (" Check that the connector's webhooks verify their signature before reading the body, that"
                         " the vendor client is used for every outbound call (no other HTTP client), and that the"
                         " mapping copies no more of a record than its contract allows." if r.get("role") == "connector" else "")
                      + (" Check that each channel route verifies its provider before reading the body, that nothing"
                         " reaches the product from an unlinked identity or goes to an opted-out one, and that no"
                         " address appears in logs, events or audit." if r.get("role") == "channel" else "")
                      + (" Check that the job calls other components only through run.call, with JOB_SERVICES"
                         " limited to what the HLD's Endpoints say it calls, and holds no token or secret in code"
                         " or environment variables." if r.get("role") == "job" else "")
                      + (" Check that the intake reads no file before its size, format and malware checks, checks"
                         " the caller's tenant on every document, and logs or audits no field value."
                         if r.get("role") == "document-intake" else "")
                      + (" Check that the agent runs no model-written code itself (no exec, eval, compile or"
                         " subprocess on it): code goes to the code sandbox's run_code." if r.get("role") == "agent" else "")
                      + (" Check VX-19: AWS services reached only behind a port, names from the environment variables"
                         " workload.yaml binds, the SDK's default credentials (no keys), and only the access the HLD's"
                         " Resources section gives this component." if resources_instruction(ctx, r) else "")
                      + (" Check that the screens match the approved UX spec: the states it lists, its copy and its"
                         " accessibility notes; a difference the coder didn't explain under Agent notes is blocking."
                         if ux_instruction(ctx, r) else "")
                      + (" Check that the acceptance tests follow the plan (extend before adding), carry the feature,"
                         " criterion, capability and component tags, declare what they cover, and use synthetic"
                         " data only." if r.get("role") == "acceptance" else "")
                      + " List performance risks the change brings (an N+1 query, a call inside a loop, an"
                        " unbounded read, a blocking call on a hot path) under performance."
                      + " Approve only if nothing must change before merge; list what must change as blocking,"
                        " each naming its evidence (a file and line, a test, a criterion, the test output)."
                      + review_evidence(r, weakened)
                      + DESIGN_ISSUE.replace("don't work around it", "don't block the code for it")
                      + f"\n\n{structured.instruction(structured.ReviewReply)}\n\nThe pull request description:\n"
                      + untrusted.wrap("pull-request-description", r["summary"])),
                stub_text=json.dumps({"approve": True, "blocking": [], "notes": [], "score": 0.9}), max_tokens=1024,
                schema=structured.schema(structured.ReviewReply),
            )
            reply, error = structured.parse(res.text, structured.ReviewReply)
            if earlier is None:  # the first review of this task: an eval case
                keep_code_case("reviewer", ctx, r, res.text, tail=review_tail, pr_url=pr["url"])
            if reply and reply.performance:  # the reviewer can only raise the level
                before = perf.level
                perf = performance.decide(r["repo"], r.get("role", ""), r["files"], hld=ctx.get("hld_text", ""),
                                          perf=ctx.get("performance"), concerns=reply.performance)
                if perf.level != before and performance.label(perf.level):
                    pr = services.github.open_pr(
                        repo=r["repo"], branch=r["branch"], title=f"{ctx['feature_id']} {tid}: {ctx['title']}",
                        body=pr_body(r), files=r["files"], labels=labels + [performance.label(perf.level)])
                    perf_pending(services, pr, perf.level)
            reviews[tid] = ({"pr": pr, "score": reply.score,
                             "approve": reply.approve and not reply.blocking and not weakened,
                             "weakened": weakened, "attempt": r.get("attempt"),
                             "coder_notes": r.get("notes") or [],
                             "blocking": reply.blocking, "notes": reply.notes, "role": r["role"],
                             "performance": perf.as_dict(),
                             **({"design_issue": reply.design_issue.model_dump()} if reply.design_issue else {})}
                            if reply else
                            {"pr": pr, "score": 0.0, "approve": False, "role": r["role"], "notes": [], "performance": perf.as_dict(),
                             "weakened": weakened, "attempt": r.get("attempt"),
                             "blocking": [f"The review reply didn't fit its schema: {error}"]})
            second = services.second_opinion("reviewer", product_data_class(services, ctx), reviewed_by=res.role)
            if second.role:
                # Another model reads the same PR with the same instructions and reply format.
                sres = call_agent(services, role=second.role, phase="build", ctx=for_task(ctx, r), skills=spec.critic_skills,
                                  tail=review_tail, max_tokens=1024, schema=structured.schema(structured.ReviewReply),
                                  stub_text=json.dumps({"approve": True, "blocking": [], "notes": [], "score": 0.9}))
                reviews[tid] = merge_second_opinion(
                    reviews[tid], sres, second.role, services.settings.role(sres.role or second.role).model,
                    blocking=services.settings.role("reviewer").second_opinion_blocking)
                tokens, cost = tokens + sres.tokens, cost + sres.cost_usd
            elif second.skipped:
                reviews[tid]["second_opinion"] = {"skipped": second.skipped}
            if (ctx.get("pilot") or ctx.get("safe_mode")) and "reviewer_shadow" in services.settings.roles:
                # Pilot, or safe mode on build: another reviewer reads the same PR; its verdict is
                # kept and, in safe mode, shown at G5.
                sres = call_agent(services, role="reviewer_shadow", phase="build", ctx=for_task(ctx, r), skills=spec.critic_skills,
                                  tail=review_tail, max_tokens=1024, schema=structured.schema(structured.ReviewReply),
                                  stub_text=json.dumps({"approve": True, "blocking": [], "notes": [], "score": 0.85}))
                sreply, _ = structured.parse(sres.text, structured.ReviewReply)
                approve = bool(sreply and sreply.approve and not sreply.blocking)
                reviews[tid]["shadow"] = {"model": services.settings.role("reviewer_shadow").model, "approve": approve,
                                          "agree": approve == reviews[tid]["approve"], "cost_usd": round(sres.cost_usd, 4)}
            tokens, cost = tokens + res.tokens, cost + res.cost_usd
        telemetry.pop_labels(token)
        return {"reviews": reviews, "usage": {"tokens": tokens, "cost_usd": cost}}

    def evaluate(s: PhaseState) -> dict:
        results, reviews = s.get("task_results") or {}, s.get("reviews") or {}
        issues = design_issues(results, reviews) or code_dependencies(services, s["ctx"], results)
        if issues:  # the approved design is wrong: more tries can't fix it, people decide (gates.py)
            first = issues[0]
            return {"findings": [f"{i['task']}: {i['document']} {i['section']}: {i['problem']}".replace("  ", " ")
                                 for i in issues],
                    "score": 0.0, "status": "escalated", "design_issues": issues,
                    "reason": f"The {first['document']} needs changing ({first['by']} on {first['task']}): "
                              f"{first['problem'][:300]}"}
        findings = [f"{t['id']}: tests failing in {t['repo']}" for t in s["ctx"]["tasks"]
                    if not results.get(t["id"], {}).get("tests_passed")]
        findings += [f"{tid}: the review blocks: " + "; ".join(rv.get("blocking") or ["not approved"])
                     for tid, rv in reviews.items() if not rv.get("approve", True)]
        # Every planned test case has its test on the branch, under the planned name.
        findings += [f"{tid}: {cid}'s test {c['file']}::{c['test']} is not on the branch"
                     for tid, r in sorted(results.items()) if r.get("tests_passed")
                     for cid, c in sorted((r.get("cases") or {}).items()) if not c.get("found")]
        # Weakening a check never passes on the reviewer's say-so (evaluators/weakening.py).
        findings += [f"{tid}: weakens a check to pass: {w}" for tid, rv in sorted(reviews.items())
                     for w in rv.get("weakened") or []]
        findings += flag_findings(s["ctx"], results)
        findings += flag_off_findings(s["ctx"], results)
        findings += audit_findings(s["ctx"], results)
        findings += events_findings(s["ctx"], results)
        findings += instrumentation_findings(s["ctx"], results)
        findings += integration_findings(services, s["ctx"], results)
        findings += documents_findings(services, s["ctx"], results)
        findings += pipelines_findings(services, s["ctx"], results)
        findings += channels_findings(services, s["ctx"], results)
        findings += agent_tools_findings(services, s["ctx"], results)
        lines = [f"# Build: {s['ctx']['title']}", "", "## Pull requests"]
        lines += [f"- {rv['pr']['repo']}#{rv['pr']['number']} ({tid}) review {rv['score']:.2f}" + second_line(rv)
                  for tid, rv in sorted(reviews.items())]
        draft = {"kind": spec.kind, "content": "\n".join(lines) + "\n"}
        return {"findings": findings, "score": min([rv["score"] for rv in reviews.values()] or [0.0]),
                "draft": draft, **_finish(s, findings, {}, between="coder and reviewer")}

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


# Files coding agents read as instructions: Claude Code loads the checkout's CLAUDE.md and .claude/
# (setting_sources=["project"]), and other agents read theirs. Agent PRs and anyone with write access
# can change them, and the next session follows them, so a change to one is a person's decision (G5).
AGENT_INSTRUCTIONS = re.compile(
    r"(^|/)(CLAUDE(\.local)?\.md|AGENTS\.md|GEMINI\.md|\.mcp\.json|\.cursorrules)$"
    r"|(^|/)\.(claude|cursor|codex|windsurf)/|(^|/)\.github/(copilot-instructions\.md|instructions/|prompts/)")


def agent_instructions(paths) -> list[str]:
    """The paths among these that agents read as instructions."""
    return sorted({p for p in paths if AGENT_INSTRUCTIONS.search(p or "")})


def risk_tier(lane_of_repo: dict[str, ProductConfig], product: ProductConfig, tasks: list[dict],
              changed: list[str] = ()) -> str:
    """Regulated if this product, or any lane whose repos the change set touches, handles PHI or
    payments; high if a task changes a high-risk path of the lane that owns its repo, a
    dependency manifest (valxel_sdlc.dependencies), or a file agents read as instructions
    (planned, or among the files its pull requests actually changed)."""
    lanes = [product] + [lane_of_repo[t["repo"]] for t in tasks if t.get("repo") in lane_of_repo]
    if any(lane.data_class in ("phi", "payments") for lane in lanes):
        return "regulated"
    from ..dependencies import is_manifest

    for task in tasks:
        lane = lane_of_repo.get(task.get("repo"), product)
        if any(f.startswith(p) for f in task.get("files", []) for p in lane.high_risk_paths):
            return "high"
        # Dependency manifests: what a change brings in is a person's decision (G5), for every product.
        if any(is_manifest(f) for f in task.get("files", [])):
            return "high"
    if agent_instructions([f for t in tasks for f in t.get("files", [])] + list(changed)):
        return "high"
    return "low"


def touched_lanes(snap, repo_names: list[str]) -> list[str]:
    """Dependency lanes (not the feature's own) whose repos appear in the given list."""
    lanes = snap.lane_of_repo()
    own = snap.product.product_id
    return sorted({lanes[r].product_id for r in repo_names if r in lanes} - {own})


def pr_body(result: dict) -> str:
    """The PR description: what changed, and the coder's notes for its reviewers."""
    notes = diary.markdown(result.get("notes") or [])
    return result["summary"] + (f"\n\n{notes}" if notes else "")


# Phases that read or change the product's repos: they wait for its GitHub connection.
CONNECTED_PHASES = ("architecture", "build")


def make_phase_node(phase: str, services):
    sub = build_build_subgraph(services) if phase == "build" else build_phase_subgraph(phase, services)
    spec = PHASE_SPECS[phase]

    def node(state: FeatureState, config: RunnableConfig) -> dict:
        if phase in CONNECTED_PHASES:  # a self-serve workspace's repos must be connected first
            from ..connections import connection_wait

            packet = connection_wait(services, state)
            if packet:
                interrupt({**packet, "phase": phase, "since": datetime.now(timezone.utc).isoformat()})
        product = services.feature_snapshot(state).product
        spent = float(state.get("cost_usd", 0.0))
        budget = float(state.get("token_budget_usd") or product.budget_usd)
        # This phase's share of the budget, with what earlier phases left (valxel_sdlc.budgets).
        left, reason = budgets.over(phase, state, spent=spent, budget=budget,
                                    base=float(state.get("budget_base_usd") or budget), shares=product.budget_shares)
        if reason:
            return {"phase_status": {phase: "escalated"},
                    "escalations": {phase: {"phase": phase, "reason": reason, "findings": []}},
                    "events": [event(phase, "escalated", reason)]}

        ctx = feature_ctx(services, state, phase)
        ctx["budget_remaining_usd"] = left
        all_tasks = list(ctx["tasks"])
        if phase == "build" and state.get("rerun_repos"):
            # CI failed in some repos: only their tasks go back to the coder, with the failures.
            ctx["tasks"] = [t for t in all_tasks if t["repo"] in set(state["rerun_repos"])] or all_tasks
        out = sub.invoke({
            "phase": phase, "ctx": ctx, "attempt": 0, "max_loops": product.max_loops,
            "threshold": product.critic_threshold, "budget_remaining_usd": left,
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
                             score=out.get("score", 0.0),
                             **({"critic": out["critic"]} if out.get("critic") else {}),
                             **({"stall": out["stall"]} if out.get("stall") else {}),
                             **({"design_issues": out["design_issues"]} if out.get("design_issues") else {}))],
        }
        if phase == "architecture":  # which repos the architect read, at which commit (G4 shows it)
            delta["code_read"] = (out.get("ctx") or {}).get("code_read") or {}
        if phase == "architecture" and status == "passed" and out.get("draft"):
            content, diagram, ev = publish_diagram(services, state, out["draft"]["content"])
            out["draft"]["content"] = content
            if diagram:
                delta["diagrams"] = [diagram]
            if ev:
                delta["events"].append(ev)
        if out.get("draft") and phase != "build":
            notes = diary.parse(out["draft"]["content"])
            delta["diaries"] = {phase: notes}
            if notes:
                delta["events"].append(event(phase, "agent_notes", diary.summary(notes), count=len(notes)))
        if out.get("draft"):
            ref = services.artifacts.save(
                feature_id=state["feature_id"], kind=spec.kind, phase=phase, content=out["draft"]["content"],
                parents=ctx["parent_ids"], constitution_version=ctx["constitution_version"],
            )
            delta["artifacts"] = {spec.kind: ref}
            delta["trace_links"] = [[p, ref["id"]] for p in ctx["parent_ids"]]
        if phase in eval_case.DOC_PHASES and out.get("draft") and getattr(services, "evals", None) is not None:
            try:  # an eval case: what the agent was given, what it wrote and the critic's verdict
                services.evals.capture(eval_case.case_from(state, phase, ctx, out,
                                                       config_version=getattr(services.gateway, "config_version", "")))
            except Exception:
                log.warning("%s %s: eval case not kept", state["feature_id"], phase, exc_info=True)
        if status == "escalated":
            delta["escalations"] = {phase: {"phase": phase, "reason": out.get("reason", ""),
                                            "findings": list(out.get("findings") or []),
                                            **({"stall": out["stall"]} if out.get("stall") else {}),
                                            **({"design_issues": out["design_issues"]}
                                               if out.get("design_issues") else {})}}
            return delta

        snap = services.feature_snapshot(state)
        if phase == "architecture":
            affected = [r["name"] for r in parse_affected_repos(out["draft"]["content"])]
            delta["lanes_touched"] = touched_lanes(snap, affected)
        elif phase == "plan":
            delta["tasks"] = parse_tasks(out["draft"]["content"])
            delta["cases"] = parse_cases(out["draft"]["content"])
        elif phase == "build":
            reviews = out.get("reviews") or {}
            delta["case_evidence"] = {cid: {**c, "task": tid} for tid, r in (out.get("task_results") or {}).items()
                                      for cid, c in (r.get("cases") or {}).items()}
            delta["sessions"] = {tid: r["session"] for tid, r in (out.get("task_results") or {}).items() if r.get("session")}
            delta["pull_requests"] = {
                f"{rv['pr']['repo']}#{rv['pr']['number']}": {**rv["pr"], "task_id": tid, "role": rv["role"],
                                                             "review_score": rv["score"]}
                for tid, rv in reviews.items()
            }
            # The change set covers every PR of the feature, also those a CI rerun didn't touch.
            prs = {**(state.get("pull_requests") or {}), **delta["pull_requests"]}
            repos = sorted({(p["role"], p["repo"]) for p in prs.values() if p.get("role")},
                           key=lambda x: (REPO_ORDER.get(x[0], 9), x[1]))
            # What the pull requests actually changed, beyond the plan: instruction files the coder touched.
            changed = [f for tid, r in (out.get("task_results") or {}).items() for f in r.get("files") or []]
            for rv in reviews.values():
                try:
                    changed += [f["path"] for f in services.github.pr_files(repo=rv["pr"]["repo"],
                                                                            number=rv["pr"]["number"]) or []]
                except Exception:  # unknown files: the plan's list still decides
                    log.warning("%s: couldn't list the files of %s", state["feature_id"], rv["pr"].get("url"))
            delta["risk_tier"] = risk_tier(snap.lane_of_repo(), product, all_tasks, changed)
            touched = agent_instructions(changed)
            if touched:
                delta["events"].append(event("build", "agent_instructions_changed",
                                             "changes files coding agents read as instructions, so people approve "
                                             "it at G5: " + ", ".join(touched[:10])))
            # The performance test each repo needs: the highest level any of its tasks needs.
            perf = dict(state.get("performance") or {})
            for rv in reviews.values():
                d = rv.get("performance")
                if d and performance.LEVELS.index(d["level"]) >= performance.LEVELS.index(
                        (perf.get(d["repo"]) or {}).get("level", "none")):
                    old = perf.get(d["repo"]) or {}
                    perf[d["repo"]] = {**d, "reasons": list(dict.fromkeys((old.get("reasons") or []) + d["reasons"])),
                                       "soak": bool(old.get("soak") or d.get("soak"))}
            delta["performance"] = perf
            needed = [d for d in perf.values() if d["level"] != "none"]
            delta["events"].append(event("build", "performance_plan", performance.summary(perf),
                                         tests=[{"repo": d["repo"], "level": d["level"]} for d in needed]))
            delta["events"][0]["reviews"] = [{"task": tid, "approve": rv.get("approve", True),
                                              **({"shadow": rv["shadow"]} if rv.get("shadow") else {})}
                                             for tid, rv in sorted(reviews.items())]
            delta["rerun_repos"] = []
            notes = [{**n, "task": tid, "repo": rv["pr"]["repo"]} for tid, rv in sorted(reviews.items())
                     for n in rv.get("coder_notes") or []]
            delta["diaries"] = {"build": notes}
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
            if (delta.get("artifacts") or {}).get(spec.kind):  # the product keeps the report (records.py)
                from .gates import record_documents

                released_by = next((e.get("approver", "") for e in reversed(state.get("events") or [])
                                    if e.get("node") == "g6" and e.get("kind") == "approved"), "")
                delta["events"].append(record_documents(
                    services, {**state, "artifacts": {**(state.get("artifacts") or {}), **delta["artifacts"]}},
                    "operate", released_by or "valxel"))
        return delta

    return node


def catalog_entry(services, ctx: dict, r: dict) -> None:
    """The repo's catalog-info.yaml on the feature branch, with this feature's capability: Git is the
    Backstage catalog's source (valxel_sdlc.catalog_info). Best effort: never fails the build."""
    from .. import catalog_info

    try:
        snap = services.snapshot(ctx["product_id"], ctx.get("governance"))
        lane = snap.lane_of_repo().get(r["repo"], snap.product)
        rt = lane.runtime or {}
        catalog_info.write(services, repo=r["repo"], branch=r["branch"], role=r.get("role", ""), product=lane,
                           component=(rt.get("components") or {}).get(r["repo"], r["repo"]),
                           project=rt.get("openchoreo_project"), capability=capability_id(ctx),
                           feature_id=ctx["feature_id"])
    except Exception:
        log.warning("catalog-info.yaml not written for %s", r["repo"], exc_info=True)


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
        # Its documents are the product's record, not the tool's copies (which go: valxel_sdlc.drafts).
        "record": {"repo": _governance_repo(services, state), "path": f"features/{state['feature_id']}/"},
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
    try:
        pr = _propose_capacity(services, state)
        if pr:
            events.append(event("operate", "capacity_pr", pr["url"], files=pr["files"]))
    except Exception as exc:  # a proposal; never fail a released feature on it
        events.append(event("operate", "capacity_failed", f"{type(exc).__name__}: {exc}"))
    ux_ref = state.get("artifacts", {}).get("UX")
    if ux_ref:
        from ..design_record import propose_after_release

        try:
            pr = propose_after_release(services, state, services.artifacts.load(ux_ref))
            if pr:
                events.append(event("operate", "design_record_pr", pr["url"], files=pr["files"]))
        except Exception as exc:
            events.append(event("operate", "design_record_failed", f"{type(exc).__name__}: {exc}"))
    return events


def _propose_capacity(services, state: dict) -> dict | None:
    """After release: the capacity G4 approved for the feature's added load, and what staging's load
    test measured per pod, as one pull request to runtime/capacity.yaml."""
    from .. import capacity

    snap = services.feature_snapshot(state)
    cap = getattr(snap, "capacity", None) or {}
    if not cap:
        return None
    new = capacity.apply_change(cap, state.get("capacity_change") or [])
    measured = []
    staging = ((state.get("quality") or {}).get("staging") or {}).get("suites") or []
    for row in staging:
        for repo, m in (((row.get("details") or {}).get("per_pod")) or {}).items():
            new = capacity.calibrate(new, repo, float(m))
            measured.append(f"{repo}: {float(m):g} per pod")
    if new == cap:
        return None
    lines = [f"- {c['repo']}: maximum {c['max_from']} -> {c['max_to']} pods (busy hour {c['busy_from']:g} -> "
             f"{c['busy_to']:g} {c['unit']}/s after a year)" for c in state.get("capacity_change") or [] if not c.get("error")]
    why = (f"Released {state['feature_id']} ({state['title']}).\n\n"
           + ("Capacity G4 approved for its added load:\n" + "\n".join(lines) + "\n\n" if lines else "")
           + ("Throughput measured by staging's load test:\n" + "\n".join(f"- {m}" for m in measured) if measured else ""))
    return capacity.propose(services, state["product_id"], new, branch=f"valxel/{state['feature_id']}-capacity",
                            title=f"{state['feature_id']}: capacity", why=why.strip())


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


def _governance_repo(services, state) -> str:
    try:
        return services.settings.entry(state["product_id"]).governance.full_name
    except Exception:  # noqa: BLE001 - the catalog entry still names the feature
        return ""

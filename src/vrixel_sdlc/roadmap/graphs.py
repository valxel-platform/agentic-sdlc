"""The initiative and release workflows, each a LangGraph thread like a feature.

Initiative:  draft -> check (loop) -> G0 (product owners, then each other lane involved)
             -> record (governance PR adding roadmap/initiatives/<id>.md) -> approved
Release:     plan (governance PR) -> wait until its features are ready -> notes and evidence
             -> release gate (G6 approvers of every lane its features touch)
             -> launch: turn the features' flags on | train: let the features promote to production
             -> record
"""

from __future__ import annotations

import operator
from datetime import datetime, timezone
from typing import Annotated, Any, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from ..agents.context import build_layers
from ..gateway import CallTags
from ..runtime import AgentTask
from ..state import last, merge_dict
from . import governance_pr, read_roadmap
from .initiative import check_draft, parse_draft, stub_draft
from .record import render_initiative, render_release

INITIATIVE_DECISIONS = ("approve", "revise", "abort")
RELEASE_DECISIONS = ("approve", "abort")


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def event(node: str, kind: str, detail: str = "", **extra) -> dict:
    return {"at": now(), "node": node, "kind": kind, "detail": detail, **extra}


def _decide(raw, allowed: tuple[str, ...]) -> dict:
    from ..graph.pipeline import normalize

    return normalize(raw, allowed)


def run_agent(services, *, role: str, product_id: str, thread_id: str, phase: str, skills: tuple[str, ...],
              tail: str, stub_text: str, max_tokens: int = 3000):
    snap = services.snapshot(product_id)
    layers = build_layers(role=role, skills=skills, constitution=snap.constitution, product=snap.product,
                          parent_texts={}, tail=tail, product_context=snap.product_context)
    tags = CallTags(product_id=product_id, feature_id=thread_id, phase=phase, role=role,
                    data_class=snap.product.data_class, workspace=snap.product.workspace, prompt_version=f"{phase}@1")
    return services.runtime_for(role).run(AgentTask(role=role, phase=phase, tags=tags, layers=layers,
                                                    stub_text=stub_text, max_tokens=max_tokens))


def lane_approvers(lane, gate: str) -> list[str]:
    """G0 falls back to G1's approvers (product owners); a release gate uses G6's."""
    return list(lane.approvers.get(gate) or lane.approvers.get({"g0": "g1", "release": "g6"}.get(gate, gate)) or [])


# --------------------------------------------------------------------------- initiative


class InitiativeState(TypedDict, total=False):
    initiative_id: str
    product_id: str
    title: str
    request: str
    measure_hint: str
    horizon_hint: str
    created_by: str
    content: Annotated[str, last]
    draft: Annotated[dict, last]
    findings: Annotated[list[str], last]
    attempt: Annotated[int, last]
    feedback: Annotated[str, last]
    status: Annotated[str, last]  # drafting | awaiting_approval | approved | dropped
    approvals: Annotated[dict, merge_dict]
    gate_decisions: Annotated[list[dict], operator.add]
    events: Annotated[list[dict], operator.add]
    record_pr: Annotated[dict, last]
    cost_usd: Annotated[float, operator.add]
    tokens_used: Annotated[int, operator.add]


def initiative_context(services, state: dict) -> dict:
    pid = state["product_id"]
    snap = services.snapshot(pid)
    try:
        record = read_roadmap(services, pid)
    except Exception:
        record = {"goals": [], "initiatives": []}
    in_tool = services.initiatives(pid) if callable(getattr(services, "initiatives", None)) else []
    known = {i["id"] for i in record["initiatives"]}
    return {
        "product_id": pid, "initiative_id": state["initiative_id"], "title": state["title"],
        "request": state.get("request", ""), "measure": state.get("measure_hint", ""),
        "horizon": state.get("horizon_hint", ""), "feedback": state.get("feedback", ""),
        "goals": record["goals"], "initiatives": record["initiatives"] + [i for i in in_tool if i["id"] not in known],
        "features": services.features(pid) if callable(getattr(services, "features", None)) else [],
        "dependency_lanes": list(snap.product.depends_on), "feature_budget": snap.product.budget_usd,
        "max_budget_usd": services.settings.limits.max_initiative_budget_usd,
    }


def build_initiative_graph(services, checkpointer=None):
    def draft(state: InitiativeState) -> dict:
        ctx = initiative_context(services, state)
        findings = state.get("findings") or []
        tail = "\n".join([
            f"Draft initiative {state['initiative_id']} for product {state['product_id']}: {state['title']}.",
            f"Request: {state.get('request', '')}",
            "Write Markdown with these sections: ## Goal (an existing goal '- GOAL-...: title', or "
            "'- New goal: <title> (measure: <how measured>)'), ## Outcome, ## Success measure, ## Horizon (now, "
            "next or later), ## Budget (USD envelope for all its features), ## Lanes (the product and any lane it "
            "depends on that must change), ## Release model (launch: features ship dark and launch together; train: "
            "regulated or contractual shipments approved together), ## Candidate features ('- Title: what it "
            "delivers'; don't repeat features the product already has), ## Risks.",
            f"Existing goals: {ctx['goals']}",
            f"Existing initiatives: {[(i['id'], i['title'], i.get('status')) for i in ctx['initiatives']]}",
            f"Features the product has: {[(f['feature_id'], f['title'], f['status']) for f in ctx['features']]}",
            *([f"Human feedback to address: {state['feedback']}"] if state.get("feedback") else []),
            *(["Checker findings to fix:\n" + "\n".join(f"- {f}" for f in findings)] if findings else []),
        ])
        res = run_agent(services, role="strategist", product_id=state["product_id"],
                        thread_id=state["initiative_id"], phase="initiative",
                        skills=("product-management:roadmap-update", "product-management:write-spec"),
                        tail=tail, stub_text=stub_draft(ctx))
        return {"content": res.text, "draft": parse_draft(res.text), "attempt": state.get("attempt", 0) + 1,
                "status": "drafting", "cost_usd": res.cost_usd, "tokens_used": res.tokens,
                "events": [event("initiative", "drafted", f"attempt {state.get('attempt', 0) + 1}")]}

    def check(state: InitiativeState) -> Command:
        ctx = initiative_context(services, state)
        findings = check_draft(state["draft"], ctx)
        max_loops = services.snapshot(state["product_id"]).product.max_loops
        if findings and state.get("attempt", 0) < max_loops:
            return Command(goto="draft", update={"findings": findings,
                                                 "events": [event("initiative", "checks_failed", "; ".join(findings))]})
        return Command(goto="gate", update={"findings": findings, "status": "awaiting_approval"})

    def gate(state: InitiativeState) -> Command:
        snap = services.snapshot(state["product_id"])
        lanes = {snap.product.product_id: snap.product, **{k: s.product for k, s in snap.dependencies.items()}}
        d = state["draft"]
        order = [state["product_id"]] + [lane for lane in d.get("lanes", []) if lane != state["product_id"] and lane in lanes]
        records, decision = [], None
        for lane_id in order:
            if (state.get("approvals") or {}).get(lane_id):
                continue
            packet = {
                "type": "gate", "gate": "g0", "phase": "initiative", "kind": "initiative", "lane": lane_id,
                "lanes": order, "feature_id": state["initiative_id"], "product_id": state["product_id"],
                "title": state["title"], "preview": state["content"][:4000], "findings": state.get("findings") or [],
                "initiative": {k: d.get(k) for k in ("goal", "new_goal", "measure", "horizon", "budget_usd", "lanes",
                                                     "release_model", "candidates")},
                "approvers": lane_approvers(lanes[lane_id], "g0"),
                "cost_usd": round(float(state.get("cost_usd", 0.0)), 4), "allowed": list(INITIATIVE_DECISIONS),
            }
            decision = _decide(interrupt(packet), INITIATIVE_DECISIONS)
            records.append({"gate": "g0", "lane": lane_id, "at": now(), **decision})
            if decision["decision"] != "approve":
                break
        if decision and decision["decision"] == "revise":
            return Command(goto="draft", update={"gate_decisions": records, "feedback": decision.get("feedback", ""),
                                                 "attempt": 0, "findings": [], "approvals": {k: False for k in order},
                                                 "events": [event("g0", "revise", decision.get("feedback", ""),
                                                                  approver=decision.get("approver", ""))]})
        if decision and decision["decision"] == "abort":
            return Command(goto=END, update={"gate_decisions": records, "status": "dropped",
                                             "events": [event("g0", "dropped", decision.get("feedback", ""),
                                                              approver=decision.get("approver", ""))]})
        approvers = ", ".join(r.get("approver", "") for r in records)
        return Command(goto="record", update={"gate_decisions": records, "approvals": {k: True for k in order},
                                              "events": [event("g0", "approved", approver=approvers, lanes=order)]})

    def record(state: InitiativeState) -> dict:
        d = state["draft"]
        approvers = ", ".join(sorted({g.get("approver", "") for g in state.get("gate_decisions") or []
                                      if g.get("decision") == "approve"}))
        initiative = {"id": state["initiative_id"], "title": state["title"],
                      "goal": d["goal"] or f"new: {d['new_goal']['title']}", "horizon": d["horizon"],
                      "status": "approved", "budget_usd": d["budget_usd"], "lanes": d["lanes"],
                      "release_model": d["release_model"], "measure": d["measure"], "owner": state.get("created_by", ""),
                      "scope": "product", "approved_by": approvers, "approved_at": now()[:10]}
        body = state["content"].split("\n", 1)[1] if state["content"].startswith("# ") else state["content"]
        files = {f"roadmap/initiatives/{state['initiative_id']}.md": render_initiative(initiative, body=body)}
        if d.get("new_goal"):
            goals_path = "roadmap/goals.yaml"
            try:
                gov = services.settings.entry(state["product_id"]).governance
                src = services.governance.source
                current = src.read(gov.org, gov.repo, goals_path, src.resolve(gov.org, gov.repo, gov.ref), optional=True) or ""
            except Exception:
                current = ""
            gid = f"GOAL-{state['product_id']}-{state['initiative_id'].rsplit('-', 1)[-1]}"
            entry = (f"  - id: {gid}\n    title: {d['new_goal']['title']}\n    measure: {d['new_goal']['measure']}\n"
                     f"    period: \"\"\n")
            files[goals_path] = (current.rstrip() + "\n" if current.strip() else "goals:\n") + entry
            initiative["goal"] = gid
            files[f"roadmap/initiatives/{state['initiative_id']}.md"] = render_initiative(initiative, body=body)
        events = []
        try:
            pr = governance_pr(services, state["product_id"], branch=f"vrixel/{state['initiative_id']}",
                               files=files, title=f"{state['initiative_id']}: {state['title']}",
                               body=(f"Initiative approved at G0 by {approvers}.\n\n- Horizon: {d['horizon']}\n"
                                     f"- Budget envelope: {d['budget_usd']:.0f} USD\n- Lanes: {', '.join(d['lanes'])}\n"
                                     f"- Release model: {d['release_model']}\n\nCandidate features:\n"
                                     + "\n".join(f"- {c['title']}" for c in d["candidates"])
                                     + "\n\nMerge to record it in the roadmap."),
                               labels=["vrixel", "roadmap"])
            events.append(event("record", "roadmap_pr", pr["url"]))
        except Exception as exc:  # the approval stands; the record can be retried from the initiative page
            pr = {"error": f"{type(exc).__name__}: {exc}"}
            events.append(event("record", "roadmap_pr_failed", pr["error"]))
        return {"status": "approved", "record_pr": pr, "events": events}

    g = StateGraph(InitiativeState)
    g.add_node("draft", draft)
    g.add_node("check", check, destinations=("draft", "gate"))
    g.add_node("gate", gate, destinations=("draft", "record", END))
    g.add_node("record", record)
    g.add_edge(START, "draft")
    g.add_edge("draft", "check")
    g.add_edge("record", END)
    return g.compile(checkpointer=checkpointer, name="vrixel_initiative")


# --------------------------------------------------------------------------- release


class ReleaseState(TypedDict, total=False):
    release_id: str
    product_id: str
    title: str
    model: str  # launch | train
    target_date: str
    initiative: str
    created_by: str
    features: Annotated[list[str], last]
    dropped: Annotated[list[str], last]  # members stopped before the release
    status: Annotated[str, last]  # planned | ready | approved | shipped | cancelled
    notes: Annotated[str, last]
    evidence: Annotated[str, last]
    lanes: Annotated[list[str], last]
    flags: Annotated[dict, last]
    approved_by: Annotated[str, last]
    gate_decisions: Annotated[list[dict], operator.add]
    events: Annotated[list[dict], operator.add]
    record_pr: Annotated[dict, last]
    cost_usd: Annotated[float, operator.add]
    tokens_used: Annotated[int, operator.add]


def _release_files(state: dict, status: str) -> dict[str, str]:
    r = {"id": state["release_id"], "title": state["title"], "model": state["model"], "status": status,
         "target_date": state.get("target_date", ""), "initiative": state.get("initiative", ""),
         "features": state.get("features") or [], "approved_by": state.get("approved_by", ""),
         "approved_at": now()[:10] if state.get("approved_by") else ""}
    return {f"roadmap/releases/{state['release_id']}.md": render_release(r, notes=state.get("notes", ""),
                                                                         evidence=state.get("evidence", ""))}


def build_release_graph(services, checkpointer=None):
    def record(state: ReleaseState, status: str, note: str) -> list[dict]:
        try:
            pr = governance_pr(services, state["product_id"], branch=f"vrixel/{state['release_id']}",
                               files=_release_files(state, status), title=f"{state['release_id']}: {state['title']}",
                               body=note, labels=["vrixel", "release"])
            return [event("record", "release_pr", pr["url"], status=status)]
        except Exception as exc:
            return [event("record", "release_pr_failed", f"{type(exc).__name__}: {exc}")]

    def plan(state: ReleaseState) -> dict:
        kind = "launch group" if state["model"] == "launch" else "release train"
        return {"status": "planned", "events": [event("plan", "planned", f"{kind} with {', '.join(state['features'])}")]
                + record(state, "planned", f"Plans {state['release_id']}, a {kind}: "
                         + ", ".join(state["features"]) + (f". Target date {state['target_date']}." if state.get("target_date") else "."))}

    def wait_ready(state: ReleaseState) -> dict:
        label = ("every feature is in production with its flag off" if state["model"] == "launch"
                 else "every feature has passed staging")
        res = interrupt({"type": "external", "wait": "release_ready", "label": f"Waiting until {label}",
                         "release_id": state["release_id"], "model": state["model"], "feature_id": state["release_id"],
                         "features": state["features"], "since": now()})
        dropped = list(res.get("dropped") or [])
        members = [f for f in state["features"] if f not in dropped]
        if not members:
            return {"status": "cancelled", "dropped": dropped,
                    "events": [event("wait", "cancelled", "every feature in the release was stopped")]}
        return {"status": "ready", "features": members, "dropped": dropped, "lanes": list(res.get("lanes") or []),
                "flags": dict(res.get("flags") or {}), "evidence": res.get("evidence", ""),
                "events": [event("wait", "ready", ", ".join(members),
                                 dropped=dropped)]}

    def route_after_wait(state: ReleaseState) -> str:
        return END if state.get("status") == "cancelled" else "notes"

    def notes(state: ReleaseState) -> dict:
        titles = services.feature_titles(state["features"]) if callable(getattr(services, "feature_titles", None)) else {}
        stub = "\n".join([f"# Release notes: {state['title']}", ""] +
                         [f"- {fid}: {titles.get(fid, fid)}" for fid in state["features"]])
        res = run_agent(services, role="release", product_id=state["product_id"], thread_id=state["release_id"],
                        phase="release_notes", skills=("engineering:deploy-checklist",),
                        tail=(f"Write user-facing release notes for {state['release_id']} ({state['model']}), covering: "
                              + "; ".join(f"{f} {titles.get(f, '')}" for f in state["features"])
                              + ". No patient data, identifiers or internal hostnames."),
                        stub_text=stub, max_tokens=1500)
        return {"notes": res.text, "cost_usd": res.cost_usd, "tokens_used": res.tokens}

    def gate(state: ReleaseState) -> Command:
        snap = services.snapshot(state["product_id"])
        lanes = {snap.product.product_id: snap.product, **{k: s.product for k, s in snap.dependencies.items()}}
        order = [state["product_id"]] + [lane for lane in state.get("lanes") or [] if lane in lanes and lane != state["product_id"]]
        records, decision = [], None
        what = ("turns on the flags of" if state["model"] == "launch" else "promotes to production")
        for lane_id in order:
            packet = {
                "type": "gate", "gate": "release", "phase": "release", "kind": "release", "lane": lane_id,
                "lanes": order, "feature_id": state["release_id"], "product_id": state["product_id"],
                "title": state["title"], "model": state["model"], "features": state["features"],
                "dropped": state.get("dropped") or [], "preview": state.get("notes", "")[:4000],
                "evidence": state.get("evidence", ""), "target_date": state.get("target_date", ""),
                "summary": f"Approving {what} {', '.join(state['features'])}.",
                "approvers": lane_approvers(lanes[lane_id], "release"), "allowed": list(RELEASE_DECISIONS),
            }
            decision = _decide(interrupt(packet), RELEASE_DECISIONS)
            records.append({"gate": "release", "lane": lane_id, "at": now(), **decision})
            if decision["decision"] != "approve":
                break
        if decision and decision["decision"] == "abort":
            return Command(goto=END, update={"gate_decisions": records, "status": "cancelled",
                                             "events": [event("release", "cancelled", decision.get("feedback", ""),
                                                              approver=decision.get("approver", ""))]
                                             + record(state, "cancelled", f"{state['release_id']} was cancelled at its gate.")})
        approvers = ", ".join(r.get("approver", "") for r in records)
        events = [event("release", "approved", approver=approvers, lanes=order)]
        status = "approved"
        note = " Its features now promote to production."
        if state["model"] == "launch":
            failed = []
            for fid, key in (state.get("flags") or {}).items():
                try:
                    services.flags.set(state["product_id"], key, True, actor=approvers)
                    events.append(event("release", "flag_on", key, feature=fid))
                except Exception as exc:
                    failed.append(key)
                    events.append(event("release", "flag_failed", f"Couldn't turn on {key}: {exc}", feature=fid))
            held = [f for f in state.get("features") or [] if f not in (state.get("flags") or {})
                    and f not in (state.get("dropped") or [])]
            on = [(fid, key) for fid, key in (state.get("flags") or {}).items() if key not in failed]
            if on and getattr(services, "changes", None) is not None:
                from ..ops.changes import new as new_change

                try:
                    stored = services.changes.record(new_change(
                        key=f"{state['release_id']}:launch", product_id=state["product_id"], kind="release",
                        type="standard", action="flag_on", summary=f"{state['release_id']} launch: {', '.join(f for f, _ in on)}",
                        targets=[{"kind": "flag", "name": k, "on": True, "feature_id": f} for f, k in on],
                        release_id=state["release_id"], approved_by=[a for a in approvers.split(", ") if a],
                        approvals=[{"gate": "release", "lane": r.get("lane", ""), "approver": r.get("approver", ""),
                                    "at": r.get("at", "")} for r in records]))
                    events.append(event("release", "change_recorded", f"{stored['id']}: launch"))
                except Exception as exc:
                    events.append(event("release", "change_not_recorded", f"{type(exc).__name__}: {exc}"))
            status = "shipped"
            note = " Flags are on: the features are live."
            if held:
                note += f" {', '.join(held)} had no flag and now promote{'s' if len(held) == 1 else ''} to production."
            if failed:
                note += f" Turn on {', '.join(failed)} in Flipt: the pipeline couldn't."
        done = {**state, "approved_by": approvers}
        events += record(done, status, f"{state['release_id']} approved by {approvers}." + note)
        return Command(goto=END, update={"gate_decisions": records, "status": status, "approved_by": approvers,
                                         "events": events})

    g = StateGraph(ReleaseState)
    g.add_node("plan", plan)
    g.add_node("wait_ready", wait_ready)
    g.add_node("notes", notes)
    g.add_node("gate", gate, destinations=(END,))
    g.add_edge(START, "plan")
    g.add_edge("plan", "wait_ready")
    g.add_conditional_edges("wait_ready", route_after_wait, ["notes", END])
    g.add_edge("notes", "gate")
    return g.compile(checkpointer=checkpointer, name="vrixel_release")

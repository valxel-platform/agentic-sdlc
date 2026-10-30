"""The feature pipeline: supervisor routing, eight phase subgraphs, six human gates.

discovery -> G1 -> spec -> G2 -> (design -> G3 | architecture -> G4) -> join -> plan -> build
  -> [G5 if high-risk] -> merge_train -> release -> G6 -> operate -> finalize

Gates and escalations are `interrupt()` nodes; a durable checkpointer keeps each thread
waiting for as long as a human needs. Any phase escalates after its loop cap or budget.
"""

from __future__ import annotations

from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from ..agents import GATE_APPROVE_TARGET, GATE_PHASE, NEXT_AFTER_PHASE, PHASE_SPECS
from ..agents.parse import parse_tasks
from ..state import GATES, PHASES, FeatureState, GateDecision
from .phase import event, make_phase_node, now

GATE_DECISIONS = ("approve", "revise", "takeover", "abort")
HANDBACK_DECISIONS = ("handback", "abort")
ESCALATION_DECISIONS = ("resolved", "retry", "abort")


class InvalidDecision(ValueError):
    pass


def normalize(raw, allowed: tuple[str, ...]) -> dict:
    d = {"decision": raw} if isinstance(raw, str) else dict(raw or {})
    decision = str(d.get("decision", "")).lower()
    if decision not in allowed:
        raise InvalidDecision(f"decision must be one of {allowed}, got {decision!r}")
    d["decision"] = decision
    d.setdefault("approver", "unknown")
    d.setdefault("feedback", "")
    d["active_minutes"] = float(d.get("active_minutes") or 0.0)
    return d


def _record(gate: str, phase: str, d: dict) -> GateDecision:
    return GateDecision(gate=gate, phase=phase, decision=d["decision"], approver=d["approver"],
                        feedback=d["feedback"], active_minutes=d["active_minutes"], at=now())


def after_phase(phase: str, state: FeatureState) -> str:
    if phase == "build":
        return "g5" if state.get("risk_tier") in ("high", "regulated") else "merge_train"
    return NEXT_AFTER_PHASE[phase]


def _route(phase: str):
    def route(state: FeatureState) -> str:
        if state.get("phase_status", {}).get(phase) == "escalated":
            return f"escalate_{phase}"
        return after_phase(phase, state)

    return route


def _route_targets(phase: str) -> list[str]:
    nxt = ["g5", "merge_train"] if phase == "build" else [NEXT_AFTER_PHASE[phase]]
    return [f"escalate_{phase}", *nxt]


def make_intake(services):
    def intake(state: FeatureState) -> dict:
        # Pins were resolved by the engine before the thread started; reading them here fails
        # fast if the product's governance repo is unreachable or invalid.
        snap = services.snapshot(state["product_id"], state.get("governance"))
        product = snap.product
        budget = min(float(state.get("token_budget_usd") or product.budget_usd), product.budget_usd)
        return {
            "status": "running",
            "data_class": product.data_class,
            "constitution_version": snap.version,
            "token_budget_usd": budget,
            "events": [event("intake", "started", state["title"], product=product.product_id,
                             governance=f"{snap.pins['product']['org']}/{snap.pins['product']['repo']}"
                                        f"@{snap.pins['product']['sha'][:7]}")],
        }

    return intake


def make_gate(gate: str, services):
    phase = GATE_PHASE[gate]
    kind = PHASE_SPECS[phase].kind
    target = GATE_APPROVE_TARGET[gate]

    def gate_node(state: FeatureState) -> Command:
        product = services.snapshot(state["product_id"], state.get("governance")).product
        ref = state.get("artifacts", {}).get(kind)
        packet = {
            "type": "gate", "gate": gate, "phase": phase,
            "feature_id": state["feature_id"], "product_id": state["product_id"], "title": state["title"],
            "constitution_version": state.get("constitution_version"),
            "artifact": ref, "preview": services.artifacts.load(ref)[:2000] if ref else "",
            "score": state.get("scores", {}).get(phase), "loops": state.get("loop_count", {}).get(phase),
            "cost_usd": round(float(state.get("cost_usd", 0.0)), 4),
            "budget_usd": state.get("token_budget_usd"),
            "approvers": product.approvers.get(gate, []),
            "risk_tier": state.get("risk_tier"),
            "pull_requests": sorted(state.get("pull_requests", {})) if gate == "g5" else [],
            "allowed": list(GATE_DECISIONS),
        }
        d = normalize(interrupt(packet), GATE_DECISIONS)
        records = [_record(gate, phase, d)]

        if d["decision"] == "takeover":
            hb = normalize(interrupt({"type": "handback", "gate": gate, "phase": phase,
                                      "feature_id": state["feature_id"], "allowed": list(HANDBACK_DECISIONS)}),
                           HANDBACK_DECISIONS)
            records.append(_record(gate, phase, hb))
            d = {**hb, "decision": "abort" if hb["decision"] == "abort" else "approve"}

        if d["decision"] == "revise":
            return Command(goto=phase, update={
                "gate_decisions": records, "feedback": {phase: d["feedback"]}, "approvals": {gate: False},
                "events": [event(gate, "revise", d["feedback"], approver=d["approver"])],
            })
        if d["decision"] == "abort":
            return Command(goto=END, update={
                "gate_decisions": records, "status": "aborted",
                "events": [event(gate, "aborted", d["feedback"], approver=d["approver"])],
            })

        update: dict = {"gate_decisions": records, "approvals": {gate: True},
                        "events": [event(gate, "approved", d["feedback"], approver=d["approver"])]}
        if gate == "g6":
            # VX-3: production promotion is a human action. In production the approver promotes in
            # the OpenChoreo portal and a watcher resumes this thread; the stub records it here.
            for comp in state.get("release", {}).get("components", []):
                services.openchoreo.promote(component=comp, env="production", actor=d["approver"],
                                            actor_type="human")
            update["release"] = {**state.get("release", {}), "production": True, "promoted_by": d["approver"]}
        return Command(goto=target, update=update)

    dest = tuple(target) if isinstance(target, list) else (target,)
    return gate_node, (*dest, phase, END)


def make_escalation(phase: str, services):
    spec = PHASE_SPECS[phase]

    def escalate(state: FeatureState) -> Command:
        esc = state.get("escalations", {}).get(phase, {})
        packet = {
            "type": "escalation", "phase": phase, "feature_id": state["feature_id"],
            "product_id": state["product_id"], "title": state["title"],
            "reason": esc.get("reason", ""), "findings": esc.get("findings", []),
            "artifact": state.get("artifacts", {}).get(spec.kind),
            "cost_usd": round(float(state.get("cost_usd", 0.0)), 4),
            "budget_usd": state.get("token_budget_usd"),
            "allowed": list(ESCALATION_DECISIONS),
        }
        d = normalize(interrupt(packet), ESCALATION_DECISIONS)
        rec = _record(f"escalation:{phase}", phase, d)
        update: dict = {"gate_decisions": [rec]}
        if d.get("budget_usd"):  # a human may raise the budget, up to the platform ceiling
            update["token_budget_usd"] = min(float(d["budget_usd"]), services.settings.limits.max_budget_usd)

        if d["decision"] == "abort":
            update |= {"status": "aborted", "events": [event(f"escalate_{phase}", "aborted", d["feedback"])]}
            return Command(goto=END, update=update)
        if d["decision"] == "retry":
            update |= {"feedback": {phase: d["feedback"]},
                       "events": [event(f"escalate_{phase}", "retry", d["feedback"], approver=d["approver"])]}
            return Command(goto=phase, update=update)

        # resolved: the human took the phase over and hands back a finished artifact.
        update |= {"phase_status": {phase: "resolved"}, "escalations": {phase: {**esc, "resolved": True}},
                   "events": [event(f"escalate_{phase}", "resolved", d["feedback"], approver=d["approver"])]}
        if d.get("artifact_content"):
            ref = services.artifacts.save(
                feature_id=state["feature_id"], kind=spec.kind, phase=phase, content=d["artifact_content"],
                parents=[r["id"] for k, r in state.get("artifacts", {}).items() if k in spec.parents],
                constitution_version=state.get("constitution_version", ""),
            )
            update["artifacts"] = {spec.kind: ref}
        if phase == "plan":
            ref = update.get("artifacts", {}).get("PLAN") or state.get("artifacts", {}).get("PLAN")
            update["tasks"] = parse_tasks(services.artifacts.load(ref)) if ref else []
        if spec.gate:  # the human's resolution stands in for the gate approval
            update["approvals"] = {spec.gate: True}
            target = GATE_APPROVE_TARGET[spec.gate]
            return Command(goto=target, update=update)
        return Command(goto=after_phase(phase, state), update=update)

    targets = {phase, END}
    if spec.gate:
        t = GATE_APPROVE_TARGET[spec.gate]
        targets |= set(t) if isinstance(t, list) else {t}
    else:
        targets.add(NEXT_AFTER_PHASE[phase])
    return escalate, tuple(sorted(targets))


def join(state: FeatureState) -> Command:
    """Plan starts only when both G3 (design) and G4 (architecture) are approved."""
    a = state.get("approvals", {})
    if a.get("g3") and a.get("g4"):
        return Command(goto="plan", update={"events": [event("join", "design_and_architecture_approved")]})
    return Command(goto=END, update={"events": [event("join", "waiting_for_other_gate")]})


def make_merge_train(services):
    def merge_train(state: FeatureState) -> dict:
        cs = dict(state.get("change_set") or {})
        risk = state.get("risk_tier", "low")
        g5 = [g for g in state.get("gate_decisions", []) if g["gate"] == "g5" and g["decision"] in ("approve", "handback")]
        actor = g5[-1]["approver"] if (risk != "low" and g5) else "agent:merge-train"
        merged: dict = {}
        for repo in cs.get("merge_order", []):  # dependency order: libraries, providers, consumers
            for key, pr in sorted(state.get("pull_requests", {}).items()):
                if pr["repo"] == repo and pr.get("state") != "merged":
                    res = services.github.merge(repo=repo, number=pr["number"], actor=actor)
                    merged[key] = {**pr, "state": res["state"], "merged_by": actor}
            services.openchoreo.run_workflow(component=repo, ref="main", kind="main")
        cs["merged"] = True
        return {"pull_requests": merged, "change_set": cs,
                "events": [event("merge_train", "merged", f"{len(merged)} PRs by {actor}",
                                 order=cs.get("merge_order", []))]}

    return merge_train


def finalize(state: FeatureState) -> dict:
    minutes = sum(g.get("active_minutes", 0.0) for g in state.get("gate_decisions", []))
    return {"status": "done", "events": [event("finalize", "done", "feature released and registered",
                                               cost_usd=round(float(state.get("cost_usd", 0.0)), 4),
                                               human_minutes=minutes)]}


def build_pipeline(services, checkpointer=None):
    g = StateGraph(FeatureState)
    g.add_node("intake", make_intake(services))
    for phase in PHASES:
        g.add_node(phase, make_phase_node(phase, services))
        esc, esc_dest = make_escalation(phase, services)
        g.add_node(f"escalate_{phase}", esc, destinations=esc_dest)
    for gate in GATES:
        node, dest = make_gate(gate, services)
        g.add_node(gate, node, destinations=dest)
    g.add_node("join", join, destinations=("plan", END))
    g.add_node("merge_train", make_merge_train(services))
    g.add_node("finalize", finalize)

    g.add_edge(START, "intake")
    g.add_edge("intake", "discovery")
    for phase in PHASES:
        g.add_conditional_edges(phase, _route(phase), _route_targets(phase))
    g.add_edge("merge_train", "release")
    g.add_edge("finalize", END)
    return g.compile(checkpointer=checkpointer, name="vrixel_feature")

"""Shared by the pipeline's nodes: decisions, routing between phases, and the records a gate leaves."""

from __future__ import annotations


from ..agents import GATE_APPROVE_TARGET, NEXT_AFTER_PHASE, PHASE_SPECS
from ..agents.parse import parse_affected_repos, parse_cases, parse_tasks
from ..state import FeatureState, GateDecision
from .. import store
from .. import profiles
from .phase import now, touched_lanes


GATE_DECISIONS = ("approve", "revise", "takeover", "abort")


HANDBACK_DECISIONS = ("handback", "abort")


ESCALATION_DECISIONS = ("resolved", "retry", "abort")


SECURITY_DECISIONS = ("retry", "accept_risk", "abort")


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


def _record(gate: str, phase: str, d: dict, lane: str = "") -> GateDecision:
    rec = GateDecision(gate=gate, phase=phase, lane=lane, decision=d["decision"], approver=d["approver"],
                       feedback=d["feedback"], active_minutes=d["active_minutes"], at=now())
    if d.get("reason"):
        rec["reason"] = str(d["reason"])
    if d.get("waited_minutes") is not None:
        rec["waited_minutes"] = round(float(d["waited_minutes"]), 2)
    if d.get("via"):
        rec["via"] = str(d["via"])
    return rec


# Phases whose work is a document a person can edit when they take it over.
DOC_PHASES = ("discovery", "spec", "design", "architecture", "plan")


SENT_BACK = ("revise", "retry")


def label_eval_case(services, state: FeatureState, phase: str, rec: dict, *, human: str | None, reference: str) -> None:
    """What people decided about the document an eval case holds (valxel_sdlc.evals); the version
    they approved becomes its reference. Best effort: never stops a gate."""
    store = getattr(services, "evals", None)
    if store is None or phase not in DOC_PHASES:
        return
    try:
        store.label(state["feature_id"], phase, {
            "decision": "takeover" if human is not None and rec.get("decision") == "takeover" else rec.get("decision", ""),
            "reason": rec.get("reason", ""), "feedback": str(rec.get("feedback") or "")[:2000],
            "taken_over": human is not None}, reference=reference or "")
    except Exception:
        pass


def correction_history(state: FeatureState, phase: str) -> list[dict]:
    """People's earlier corrections to this phase in this feature (sent back, taken over), oldest first."""
    return [{k: g.get(k, "") for k in ("gate", "decision", "approver", "reason", "feedback", "at")}
            for g in state.get("gate_decisions", []) or []
            if g.get("phase") == phase and g.get("decision") in SENT_BACK + ("takeover", "handback", "resolved")
            and (g.get("decision") in SENT_BACK or g.get("feedback"))]


def times_sent_back(state: FeatureState, phase: str) -> int:
    return sum(1 for g in state.get("gate_decisions", []) or [] if g.get("phase") == phase and g.get("decision") in SENT_BACK)


def human_artifact(services, state: FeatureState, phase: str, content: str) -> dict:
    """A person's own version of a phase's document (after a takeover or an escalation): saved as
    the next version, and read like an agent's (tasks from a plan, lanes from an HLD)."""
    spec = PHASE_SPECS[phase]
    parents = [r["id"] for k, r in (state.get("artifacts") or {}).items() if k in spec.parents]
    ref = services.artifacts.save(feature_id=state["feature_id"], kind=spec.kind, phase=phase, content=content,
                                  parents=parents, constitution_version=state.get("constitution_version", ""))
    update: dict = {"artifacts": {spec.kind: ref}, "trace_links": [[p, ref["id"]] for p in parents]}
    if phase == "plan":
        update["tasks"] = parse_tasks(content)
        update["cases"] = parse_cases(content)
    if phase == "architecture":
        update["lanes_touched"] = touched_lanes(services.feature_snapshot(state),
                                                [r["name"] for r in parse_affected_repos(content)])
    return update


def correction(state: FeatureState, gate: str, phase: str, kind: str, d: dict, n: int = 0,
               changes: dict | None = None) -> dict:
    """One person's correction, for learning (valxel_sdlc.lessons)."""
    return {"product_id": state["product_id"], "feature_id": state["feature_id"], "gate": gate, "phase": phase,
            "kind": kind, "reason": str(d.get("reason") or ""), "feedback": d.get("feedback", ""),
            "lesson": str(d.get("lesson") or ""), "remember": bool(d.get("remember") or d.get("lesson")),
            "approver": d.get("approver", ""), "n": n, "changes": changes or {}}


def after_ci(state: FeatureState) -> str:
    return "g5" if state.get("risk_tier") in ("high", "regulated") else "merge_train"


def after_phase(phase: str, state: FeatureState) -> str:
    if phase == "build":
        return "ci_wait"
    if phase == "spec" and "g2" in profiles.get(state.get("profile")).skips_gates:
        return "architecture"  # express: G4 approves the spec with the design
    return NEXT_AFTER_PHASE[phase]


def approve_target(gate: str, state: FeatureState):
    """Where an approved gate sends the work: G2 skips UX design when the profile does."""
    target = GATE_APPROVE_TARGET[gate]
    if isinstance(target, list):
        skipped = set(profiles.get(state.get("profile")).skips)
        if state.get("ui") is False:  # nothing people see changes: no UX design, no G3
            skipped.add("design")
        target = [t for t in target if t not in skipped]
        return target[0] if len(target) == 1 else target
    return target


def _route(phase: str):
    def route(state: FeatureState) -> str:
        if state.get("phase_status", {}).get(phase) == "escalated":
            return f"escalate_{phase}"
        return after_phase(phase, state)

    return route


def _route_targets(phase: str) -> list[str]:
    nxt = ["ci_wait"] if phase == "build" else [NEXT_AFTER_PHASE[phase]]
    if phase == "spec":
        nxt.append("architecture")  # express
    return [f"escalate_{phase}", *nxt]


PHASE_SPECS_GATE = {"g1": "discovery", "g2": "spec", "g3": "design", "g4": "architecture", "g5": "build", "g6": "release"}


CROSS_LANE_GATES = ("g4", "g5", "g6")


def bump_for(state: FeatureState, repo: str) -> str:
    """A hotfix is a patch; a breaking contract change in the repo is a major version; else minor."""
    if state.get("track") == "hotfix":
        return "patch"
    contracts = (state.get("design") or {}).get("contracts") or []
    return "major" if any(c.get("repo") == repo and c.get("breaking") for c in contracts) else "minor"


def release_store_app(services, state: FeatureState, repo: str, sha: str, stage: str, *, actor: str) -> dict:
    """Ask a store app's repo to release this commit for the stage (valxel_sdlc.store), once per
    attempt: a step run again from the same checkpoint (after a crash, or on another replica) doesn't
    start a second release of the build, while a retry people or agents asked for does. The thread's
    events mark the attempt: the same checkpoint has the same number of them, a retry more."""
    key = f"store-release|{state['feature_id']}|{repo}|{sha}|{stage}|{len(state.get('events') or [])}"
    once, unask = getattr(services, "ask_once", None), getattr(services, "unask", None)
    if once is None or once(key):
        try:
            services.github.dispatch(repo=repo, event_type="valxel-release", payload={
                "stage": stage, "sha": sha, "feature_id": state["feature_id"], "actor": actor})
        except Exception:
            if unask is not None:
                unask(key)  # it didn't go: the next attempt may ask
            raise
    return {"repo": repo, "kind": "store", "component": repo, "sha": sha, "env": stage,
            "pipeline": list(store.STAGES), "release": store.release_context(stage), "released_by": actor}

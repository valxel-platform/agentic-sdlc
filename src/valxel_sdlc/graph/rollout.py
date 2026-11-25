"""Staging checks, the production rollout and finishing a feature."""

from __future__ import annotations

from langgraph.graph import END
from langgraph.types import Command, interrupt

from ..integrations.openchoreo import PIPELINE
from ..state import FeatureState
from .. import store
from .. import profiles
from .phase import event, now
from .infra import env_for
from .merge_train import _people_said, ask_release_managers, external
from .routing import _record


def _goal_events(services, state: FeatureState) -> list[dict]:
    from .phase import instrumentation_events

    try:
        return instrumentation_events(services, state.get("artifacts") or {})
    except Exception:
        return []


def datasets_staging(services, state: FeatureState) -> dict:
    """The design's data contracts and its connectors' pull sources, reconciled in staging
    (valxel_sdlc.reconcile)."""
    from ..connectors import pull_sources
    from ..datasets import parse_hld
    from ..reconcile import staging_check

    ref = (state.get("artifacts") or {}).get("HLD")
    hld = services.artifacts.load(ref) if ref else ""
    contracts = parse_hld(hld)[0] if hld else []
    rt = services.feature_snapshot(state).product.runtime or {}
    env = env_for("staging", tuple(rt.get("pipeline") or PIPELINE)) or "staging"
    return staging_check(services, state, contracts, env, pulls=pull_sources(hld) if hld else {})


def instrumentation_staging(services, state: FeatureState) -> dict:
    """The feature's product events, counted in staging (valxel_sdlc.outcomes.instrumentation)."""
    from ..outcomes.instrumentation import staging_check
    from .phase import instrumentation_events

    events = instrumentation_events(services, state.get("artifacts") or {})
    rt = services.feature_snapshot(state).product.runtime or {}
    env = env_for("staging", tuple(rt.get("pipeline") or PIPELINE)) or "staging"
    return staging_check(services, state, events, env)


def make_rollout(services):
    """Widen store apps' production rollout step by step, each after a wait and a health check.
    A failed check halts the rollout and goes to the release managers."""

    def rollout(state: FeatureState) -> Command:
        apps = sorted((d for d in (state.get("deployments") or {}).values()
                       if d.get("kind") == "store" and d.get("env") == "production"), key=lambda d: d["repo"])
        if not apps:
            return Command(goto="flag_rollout")
        snap = services.feature_snapshot(state)
        policy = store.rollout_policy(snap.product.repo_standards)
        ro = dict(state.get("rollout") or {})
        step, attempt = int(ro.get("step", 0)), int(ro.get("attempt", 0))
        if step >= len(policy["steps"]):
            return Command(goto="flag_rollout")
        fraction = policy["steps"][step]
        since = ro.get("at") or next((e.get("at") for e in reversed(state.get("events") or [])
                                      if e.get("node") == "production_wait"), None)
        targets = [{"repo": a["repo"], "sha": a["sha"], "fraction": fraction, "context": store.rollout_context(fraction, attempt),
                    "not_before": store.not_before(since, policy["min_hours"]) if not attempt else "",
                    "recheck_hours": policy["recheck_hours"], "feature_id": state["feature_id"]} for a in apps]
        packet = external(state, "rollout", targets)
        packet["timeout_s"] = int((float(policy["min_hours"]) + float(policy["step_timeout_hours"])) * 3600)
        res = interrupt(packet)
        history = list(ro.get("history") or [])
        if res["status"] == "success":
            history.append({"fraction": fraction, "at": res.get("at", now()), "attempt": attempt})
            done = step + 1 >= len(policy["steps"])
            return Command(goto="flag_rollout" if done else "rollout", update={
                "rollout": {"step": step + 1, "attempt": 0, "at": res.get("at", now()), "history": history},
                "events": [event("rollout", "widened", f"{', '.join(a['repo'] for a in apps)} at {store.percent(fraction)}")]})
        # Unhealthy (or stuck): stop the Play rollout where it is, then let people decide.
        for a in apps:
            try:
                services.github.dispatch(repo=a["repo"], event_type="valxel-rollout", payload={
                    "sha": a["sha"], "halt": True, "feature_id": state["feature_id"]})
            except Exception:
                pass
        return _people_said(state, "rollout", res, ok="flag_rollout", retry="rollout", extra={
            "rollout": {"step": step, "attempt": attempt + 1, "at": ro.get("at") or since, "history": history,
                        "halted": True},
            "events": [event("rollout", "halted", res.get("reason", ""))]})

    return rollout, ("rollout", "flag_rollout", END)


def make_flag_rollout(services):
    """Widen a feature's flag step by step, per the plan G6 approved (ops/rollout.py)."""
    from ..ops import rollout as ro
    from ..ops.changes import new as new_change
    from ..ops.rollout import ROLLOUT_AGENT

    def record(state: FeatureState, release: dict, pct: float, *, by: str, approved: list[str], n: int,
               note: str) -> list[dict]:
        log = getattr(services, "changes", None)
        if log is None:
            return []
        try:
            stored = log.record(new_change(
                key=f"{state['feature_id']}:rollout:{n}:{pct:g}", product_id=state["product_id"], kind="flag",
                type="standard", action="rollout_step", summary=f"{state['feature_id']} {state['title']}: on for {ro.percent_label(pct)}",
                targets=[{"kind": "flag", "name": release.get("flag", ""), "on": True, "percent": pct}],
                feature_id=state["feature_id"], release_id=release.get("release_id", ""), requested_by=by,
                approved_by=approved, evidence={"plan": (release.get("rollout_plan") or {}).get("steps"), "why": note}))
            return [event("flag_rollout", "change_recorded", f"{stored['id']}: {stored['summary']}")]
        except Exception as exc:
            return [event("flag_rollout", "change_not_recorded", f"{type(exc).__name__}: {exc}")]

    def flag_rollout(state: FeatureState) -> Command:
        release = dict(state.get("release") or {})
        plan = dict(release.get("rollout_plan") or {})
        key = release.get("flag", "")
        if not plan or not key:
            return Command(goto="operate")
        steps, step = list(plan["steps"]), int(plan.get("step", 0))
        if step >= len(steps) - 1:
            return Command(goto="operate")
        components = sorted({d["repo"] for d in (state.get("deployments") or {}).values()
                             if d.get("env") == "production" and d.get("kind") not in ("resource", "store") and d.get("repo")})
        res = interrupt({"type": "external", "wait": "flag_step", "feature_id": state["feature_id"],
                         "product_id": state["product_id"], "title": state["title"], "flag": key,
                         "percent": steps[step], "components": components, "since": plan.get("at") or ro._clock().isoformat(),
                         "bake_minutes": plan["bake_minutes"], "compare": plan.get("compare") or {},
                         # The events that measure the feature's goal: compared on against off, reported only.
                         "goal_events": [e["event"] for e in _goal_events(services, state)],
                         "label": f"Gradual release: on for {ro.percent_label(steps[step])}, watching"})
        approved = [a for a in str(plan.get("approved_by") or release.get("promoted_by") or "").split(", ") if a]
        history = list(plan.get("history") or [])
        if res.get("status") == "success":
            nxt = steps[step + 1]
            try:
                services.flags.set_percent(state["product_id"], key, nxt, actor=ROLLOUT_AGENT)
            except Exception as exc:
                res = {"status": "failure", "reason": f"couldn't widen {key}: {type(exc).__name__}: {exc}"}
            else:
                from ..ops.cohorts import describe_goal, describe_groups

                cmp_ = (res.get("cohorts") or {}).get(key)
                goal = res.get("goal_cohorts") or {}
                history.append({"percent": steps[step], "until": res.get("at", now()), "quiet_since": res.get("quiet_since", ""),
                                "cohorts": describe_groups(key, cmp_) if cmp_ else "no comparison (no summary lines): time alone",
                                **({"goal": describe_goal(goal), "goal_cohorts": goal} if goal else {})})
                plan |= {"step": step + 1, "at": ro._clock().isoformat(), "history": history}
                release |= {"rollout_plan": plan, "flag_percent": nxt, "flag_on": True}
                events = [event("flag_rollout", "widened", f"{key} on for {ro.percent_label(nxt)}")]
                events += record(state, release, nxt, by=ROLLOUT_AGENT, approved=approved, n=plan.get("attempt", 0),
                                 note=f"quiet for {plan['bake_minutes']} minutes at {ro.percent_label(steps[step])}")
                return Command(goto="flag_rollout" if step + 1 < len(steps) - 1 else "operate",
                               update={"release": release, "events": events})
        # Stopped: its users fail more, the flag went off (an incident's flag off), or it couldn't be widened.
        pre: list[dict] = []
        if res.get("regression"):
            pre, said = ro.on_regression(services, product_id=state["product_id"], thread_id=state["feature_id"],
                                         title=f"{state['feature_id']} {state['title']}", flags={state["feature_id"]: key},
                                         res=res, policy=getattr(services.feature_snapshot(state), "remediation", None),
                                         release_id=release.get("release_id", ""))
            res = {**res, "reason": f"{res.get('reason', '')}. {said}"}
        d = ask_release_managers(state, "flag_rollout", {
            "reason": f"The gradual release stopped at {ro.percent_label(steps[step])}: {res.get('reason', '')}. "
                      "Retry starts again from the first step; resolved leaves the flag as it is."})
        rec = _record("escalation:delivery:flag_rollout", "delivery", d)
        if d["decision"] == "abort":
            return Command(goto=END, update={"gate_decisions": [rec], "status": "aborted",
                                             "events": pre + [event("flag_rollout", "aborted", d.get("feedback", ""), approver=d["approver"])]})
        if d["decision"] == "retry":
            try:
                services.flags.set_percent(state["product_id"], key, steps[0], actor=d["approver"])
            except Exception as exc:
                return Command(goto="operate", update={"gate_decisions": [rec], "events": [
                    event("flag_rollout", "flag_failed", f"Couldn't turn {key} back on: {exc}", approver=d["approver"])]})
            attempt = int(plan.get("attempt", 0)) + 1
            plan |= {"step": 0, "at": ro._clock().isoformat(), "attempt": attempt, "history": history + [{"stopped_at": steps[step], "reason": res.get("reason", "")}]}
            release |= {"rollout_plan": plan, "flag_percent": steps[0], "flag_on": True}
            events = pre + [event("flag_rollout", "restarted", f"{key} on for {ro.percent_label(steps[0])} again", approver=d["approver"])]
            events += record(state, release, steps[0], by=d["approver"], approved=[d["approver"]], n=attempt,
                             note=f"restarted after: {res.get('reason', '')}")
            return Command(goto="flag_rollout", update={"gate_decisions": [rec], "release": release, "events": events})
        off_now = services.flags.percent(state["product_id"], key) in (0.0, None)
        release |= {"rollout_plan": {**plan, "stopped": True}, "flag_percent": 0.0 if off_now else release.get("flag_percent"),
                    "flag_on": not off_now}
        return Command(goto="operate", update={"gate_decisions": [rec], "release": release, "events": pre + [
            event("flag_rollout", "left_off" if off_now else "left_as_is", d.get("feedback", "") or res.get("reason", ""),
                  approver=d["approver"])]})

    return flag_rollout, ("flag_rollout", "operate", END)


def acceptance_targets(services, state: FeatureState, stage: str, deployed: list[dict]) -> list[dict]:
    """The product acceptance run for this stage, when the change set has one: the acceptance
    repo's merged commit, the criteria to prove, which tests to select and where each component
    runs (filled in from the deployments when the run starts)."""
    from .. import acceptance
    from ..agents.parse import parse_acs

    acc = (state.get("change_set") or {}).get("acceptance")
    if not acc or not acc.get("sha") or stage not in ("change_set", "dev", "staging"):
        return []
    repos = [{"name": t["repo"], "role": t.get("role")} for t in state.get("tasks") or []]
    arts = state.get("artifacts") or {}
    acs = list(parse_acs(services.artifacts.load(arts["PRD"]))) if "PRD" in arts else []
    return [{"kind": "acceptance", "repo": acc["repo"], "sha": acc["sha"], "feature_id": state["feature_id"],
             "acs": acs, "flag": state.get("flag", ""), "components": sorted({d["repo"] for d in deployed}),
             **acceptance.selection(state["feature_id"], repos, stage)}]


def first_step(state: FeatureState) -> str:
    if state.get("track") == "hotfix":
        return "hotfix_brief"
    return "discovery" if profiles.get(state.get("profile")).starts_at == "discovery" else "profile_brief"


def finalize(state: FeatureState) -> dict:
    minutes = sum(g.get("active_minutes", 0.0) for g in state.get("gate_decisions", []))
    return {"status": "done", "events": [event("finalize", "done", "feature released and registered",
                                               cost_usd=round(float(state.get("cost_usd", 0.0)), 4),
                                               human_minutes=minutes)]}

"""The feature pipeline: supervisor routing, eight phase subgraphs, six human gates.

discovery -> G1 -> spec -> G2 -> (design -> G3 | architecture -> G4) -> join -> plan -> build
  -> ci_wait -> [G5 if high-risk] -> merge_train -> build_wait -> deploy_dev -> dev_wait
  -> deploy_staging -> staging_wait -> release -> G6 (promotes production) -> production_wait
  -> rollout (store apps only) -> operate -> finalize

Gates and escalations are `interrupt()` nodes; a durable checkpointer keeps each thread
waiting for as long as a human needs. Any phase escalates after its loop cap or budget.
The *_wait nodes pause the same way on GitHub CI and OpenChoreo (see vrixel_sdlc.waits); when
one fails, the release managers decide whether to retry, carry on, or stop.
"""

from __future__ import annotations

from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from ..agents import GATE_APPROVE_TARGET, GATE_PHASE, NEXT_AFTER_PHASE, PHASE_SPECS
from ..agents.parse import parse_affected_repos, parse_new_repos, parse_tasks
from ..integrations.openchoreo import PIPELINE
from ..state import GATES, PHASES, FeatureState, GateDecision
from .. import store
from ..waits import LABELS, STORE_TIMEOUT_S, TIMEOUT_S
from .. import lessons
from .phase import capability_id, design_knowledge, event, gates_status, make_phase_node, now, touched_lanes

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
    return rec


# Phases whose work is a document a person can edit when they take it over.
DOC_PHASES = ("discovery", "spec", "design", "architecture", "plan")
SENT_BACK = ("revise", "retry")


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
    if phase == "architecture":
        update["lanes_touched"] = touched_lanes(services.feature_snapshot(state),
                                                [r["name"] for r in parse_affected_repos(content)])
    return update


def correction(state: FeatureState, gate: str, phase: str, kind: str, d: dict, n: int = 0,
               changes: dict | None = None) -> dict:
    """One person's correction, for learning (vrixel_sdlc.lessons)."""
    return {"product_id": state["product_id"], "feature_id": state["feature_id"], "gate": gate, "phase": phase,
            "kind": kind, "reason": str(d.get("reason") or ""), "feedback": d.get("feedback", ""),
            "lesson": str(d.get("lesson") or ""), "remember": bool(d.get("remember") or d.get("lesson")),
            "approver": d.get("approver", ""), "n": n, "changes": changes or {}}


def after_ci(state: FeatureState) -> str:
    return "g5" if state.get("risk_tier") in ("high", "regulated") else "merge_train"


def after_phase(phase: str, state: FeatureState) -> str:
    if phase == "build":
        return "ci_wait"
    return NEXT_AFTER_PHASE[phase]


def _route(phase: str):
    def route(state: FeatureState) -> str:
        if state.get("phase_status", {}).get(phase) == "escalated":
            return f"escalate_{phase}"
        return after_phase(phase, state)

    return route


def _route_targets(phase: str) -> list[str]:
    nxt = ["ci_wait"] if phase == "build" else [NEXT_AFTER_PHASE[phase]]
    return [f"escalate_{phase}", *nxt]


def make_intake(services):
    def intake(state: FeatureState) -> dict:
        # Pins were resolved by the engine before the thread started; reading them here fails
        # fast if the product's governance repo is unreachable or invalid.
        snap = services.feature_snapshot(state)
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


def make_hotfix_brief(services):
    """An incident's hotfix skips discovery to architecture: on-call already decided it is needed.
    It starts at planning from a short spec and design written from the incident, so planning,
    tests, security and the merge and release gates still apply."""

    def hotfix_brief(state: FeatureState) -> dict:
        h = state.get("hotfix") or {}
        snap = services.feature_snapshot(state)
        repo = h.get("repo", "")
        role = next((r.role for lane in snap.lanes().values() for r in lane.repos if r.name == repo), "provider")
        inc = h.get("incident_id", "the incident")
        prd = "\n".join([
            f"# Hotfix spec: {state['title']}", "", "## Goals", f"- Stop the failure behind {inc}", "",
            "## Non-goals", "- New behaviour beyond the fix", "", "## Acceptance criteria",
            f"- AC-1: Given the conditions of {inc}, when the fix is deployed, then the failure no longer occurs "
            "and its alerts resolve",
            "- AC-2: Given the existing tests, when the fix is built, then they still pass", "",
            "## Constitution articles", "- VX-5", "", "## Change", h.get("change", ""), ""])
        hld = "\n".join([
            f"# Hotfix design: {state['title']}", "", "## Builds on", f"- None: a fix within the existing design ({inc})",
            "", "## ADRs", f"- ADR-{state['feature_id']}-1: Fix forward in {repo} for {inc}; no design change", "",
            "## Affected repos", f"- {repo} ({role})", "", "## Contracts", f"- {repo}: unchanged", ""])
        arts, version = {}, snap.version
        for kind, phase, content in (("PRD", "spec", prd), ("HLD", "architecture", hld)):
            arts[kind] = services.artifacts.save(feature_id=state["feature_id"], kind=kind, phase=phase,
                                                 content=content, parents=[], constitution_version=version)
        by = h.get("approved_by", "")
        return {
            "artifacts": arts,
            "approvals": {g: True for g in ("g1", "g2", "g3", "g4")},
            "phase_status": {p: "skipped" for p in ("discovery", "spec", "design", "architecture")},
            "lanes_touched": touched_lanes(snap, [repo]),
            "events": [event("hotfix", "expedited", f"Hotfix for {inc}, approved by on-call {by}", approver=by)],
        }

    return hotfix_brief


CROSS_LANE_GATES = ("g4", "g5", "g6")


def quality_summary(state: FeatureState) -> dict:
    stages = state.get("quality") or {}
    suites = [{"stage": st, "suite": r["suite"], "kind": r["kind"], "repo": r.get("repo", ""), "state": r["state"],
               "coverage": r.get("coverage"), "passed": r.get("passed"), "failed": r.get("failed"),
               "findings": len(r.get("findings") or []), "waived": len(r.get("waived") or [])}
              for st, q in stages.items() for r in q.get("suites", [])]
    return {"suites": suites, "waivers": list(state.get("waivers") or [])}


def make_gate(gate: str, services):
    phase = GATE_PHASE[gate]
    kind = PHASE_SPECS[phase].kind
    target = GATE_APPROVE_TARGET[gate]

    def gate_node(state: FeatureState) -> Command:
        snap = services.feature_snapshot(state)
        lanes = snap.lanes()
        # G4 to G6 need every lane whose repos the change set touches: each owner signs off.
        approving = [state["product_id"]]
        if gate in CROSS_LANE_GATES:
            approving += [lane for lane in state.get("lanes_touched", []) if lane in lanes]
        ref = state.get("artifacts", {}).get(kind)
        # G4 reviewers see the design against what exists now: the decisions it builds on or
        # overrides, the contract changes from the versions actually merged, related decisions it
        # doesn't mention, and other approved designs in flight that change the same APIs.
        review = design_review_for(services, snap, ref, state) if gate == "g4" and ref else None

        # A feature in a release train is approved with its train: it waits here until the train's
        # release gate decides, then promotes like any approved G6 (a cancelled train falls back to G6).
        preset = None
        if gate == "g6":
            rel = services.release_for(state["feature_id"]) if callable(getattr(services, "release_for", None)) else None
            # A launch-group member without a flag can't go to production dark: it waits like a train.
            if rel and (rel["model"] == "train" or (rel["model"] == "launch" and not state.get("flag"))):
                res = interrupt({"type": "external", "wait": "release_train", "release_id": rel["id"],
                                 "label": (f"Waiting for release train {rel['id']} to be approved" if rel["model"] == "train" else
                          f"Waiting for launch {rel['id']} to be approved: no feature flag, so it can't ship dark"),
                                 "feature_id": state["feature_id"], "product_id": state["product_id"], "since": now()})
                if res.get("status") == "success":
                    preset = {"decision": "approve", "approver": res.get("approver", ""), "active_minutes": 0.0,
                              "feedback": f"Approved with release {rel['id']}"}

        limits = services.settings.limits
        history, sent_back = correction_history(state, phase), times_sent_back(state, phase)
        allowed = [x for x in GATE_DECISIONS if not (x == "revise" and sent_back >= limits.max_gate_revisions)]
        records, d = [], None
        corrections: list[dict] = []
        taken: list[tuple[dict, str, str]] = []  # (decision, agents' draft, person's version)
        human: str | None = None
        for lane_id in approving:
            lane = lanes[lane_id]
            if preset is not None:
                d = dict(preset)
                records.append(_record(gate, phase, d, lane_id))
                continue
            packet = {
                "type": "gate", "gate": gate, "phase": phase, "lane": lane_id, "lanes": approving,
                "feature_id": state["feature_id"], "product_id": state["product_id"], "title": state["title"],
                "constitution_version": state.get("constitution_version"),
                "artifact": ref, "preview": services.artifacts.load(ref)[:2000] if ref else "",
                "score": state.get("scores", {}).get(phase), "loops": state.get("loop_count", {}).get(phase),
                "cost_usd": round(float(state.get("cost_usd", 0.0)), 4),
                "budget_usd": state.get("token_budget_usd"),
                # Every gate sees what the feature has cost to build so far, by category (finops).
                "build_cost": _build_cost(services, state),
                # On-call decides an incident's hotfix at G5 and G6.
                "approvers": (list(lane.approvers.get("oncall") or lane.approvers.get(gate, []))
                              if state.get("track") == "hotfix" and gate in ("g5", "g6")
                              else lane.approvers.get(gate, [])),
                "track": state.get("track", "standard"),
                "risk_tier": state.get("risk_tier"),
                # G5 and G6 see the evidence: every suite run so far, and any accepted risks.
                "quality": quality_summary(state) if gate in ("g5", "g6") else None,
                # G5 and G6 see which performance tests the change needed, and why.
                "performance": (sorted((state.get("performance") or {}).values(), key=lambda d: d["repo"])
                                if gate in ("g5", "g6") else None),
                # Approving G4 approves creating these repos, with these settings.
                "new_repos": repo_plans(services, state) if gate == "g4" and lane_id == state["product_id"] else [],
                # Approving G4 approves these AWS services, who may use them and their cost (VX-19);
                # G6 approves binding them in production.
                "aws_resources": (aws_summary(services, state, snap, ref)
                                  if gate in ("g4", "g6") and lane_id == state["product_id"] else None),
                "design_review": review,
                # G6 sees the CloudWatch alarms firing before production on what the feature touches.
                "alarms_firing": _alarms_firing(services, state) if gate == "g6" else [],
                # G1 decides whether this intent is new: what the product already has that looks like it.
                "related_features": list(state.get("related_features") or []) if gate == "g1" else [],
                # G1 also decides whether the work fits the roadmap: its initiative and what's left of
                # the initiative's budget, or that it is unplanned.
                "roadmap_fit": (services.roadmap_fit(state) if gate == "g1" and callable(getattr(services, "roadmap_fit", None))
                                else None),
                "not_duplicate_reason": state.get("not_duplicate_reason", "") if gate == "g1" else "",
                # Each lane reviews the PRs in its own org.
                "pull_requests": sorted(k for k, pr in state.get("pull_requests", {}).items()
                                        if pr["repo"].split("/", 1)[0] == lane.org) if gate == "g5" else [],
                # A phase going round in circles: what people already asked for, how often it went
                # back, and (from takeover_after_revisions) a suggestion to take it over instead.
                # At max_gate_revisions it can't be sent back again.
                "history": history, "revisions": sent_back, "stuck": sent_back >= limits.takeover_after_revisions,
                "allowed": allowed,
            }
            if human is not None:  # an earlier lane took it over: later lanes decide on that version
                packet["preview"] = human[:2000]
            d = normalize(interrupt(packet), tuple(allowed))
            records.append(_record(gate, phase, d, lane_id))
            if state.get("pilot"):  # what the trial rule would have done, beside what the person did
                from ..pilot import shadow_auto_approval

                shadow = shadow_auto_approval(state, gate, phase, services.feature_snapshot(state).product.data_class)
                if shadow:
                    records[-1]["shadow_auto"] = shadow
            if d["decision"] == "takeover":
                draft = human if human is not None else (services.artifacts.load(ref) if ref else "")
                hb = normalize(interrupt({
                    "type": "handback", "gate": gate, "phase": phase, "lane": lane_id,
                    "feature_id": state["feature_id"], "product_id": state["product_id"], "title": state["title"],
                    "artifact": ref, "history": history, "approvers": packet["approvers"],
                    # The document to edit: hand it back with your version, or as it is.
                    "content": draft if phase in DOC_PHASES else "", "allowed": list(HANDBACK_DECISIONS)}),
                    HANDBACK_DECISIONS)
                records.append(_record(gate, phase, hb, lane_id))
                said = {**hb, "feedback": hb["feedback"] or d["feedback"], "reason": hb.get("reason") or d.get("reason"),
                        "remember": hb.get("remember") or d.get("remember"), "lesson": hb.get("lesson") or d.get("lesson")}
                mine = hb.get("artifact_content")
                if hb["decision"] == "handback" and mine and mine.strip() != draft.strip():
                    taken.append((said, draft, mine))
                    human = mine
                else:
                    corrections.append(correction(state, gate, phase, "takeover", said, len(records)))
                d = {**hb, "decision": "abort" if hb["decision"] == "abort" else "approve"}
            elif d["decision"] in ("revise", "abort"):
                corrections.append(correction(state, gate, phase, d["decision"], d, len(records)))
            if d["decision"] != "approve":
                break  # one lane's revise or abort decides the gate

        # After the last question (code here runs once): keep what people wrote, and learn from it.
        human_update: dict = {}
        for said, draft, mine in taken:
            corrections.append(correction(state, gate, phase, "takeover", said, len(records),
                                          changes=lessons.changes(draft, mine)))
        if human is not None:
            human_update = human_artifact(services, state, phase, human)
            ref = human_update["artifacts"][kind]
            if gate == "g4":
                review = design_review_for(services, snap, ref, state)
        # Aborts teach only when someone asked to remember why.
        learned = lessons.learn(services, state, [c for c in corrections if c["kind"] != "abort" or c["remember"]])

        if d["decision"] == "revise":
            # Earlier feedback stays with the agents too, so a second round doesn't undo the first.
            earlier = list(dict.fromkeys(h["feedback"] for h in history if h["feedback"] and h["feedback"] != d["feedback"]))
            said = d["feedback"] + ("\n\nEarlier feedback on this phase, still to keep:\n"
                                    + "\n".join(f"- {f}" for f in earlier) if earlier else "")
            return Command(goto=phase, update=human_update | {
                "gate_decisions": records, "feedback": {phase: said}, "approvals": {gate: False},
                "events": [event(gate, "revise", d["feedback"], approver=d["approver"], round=sent_back + 1)] + learned,
            })
        if d["decision"] == "abort":
            gates_status(services, (state.get("pull_requests") or {}).values(), "failure",
                         f"{state['feature_id']} aborted at {gate.upper()} by {d['approver']}")
            if state.get("design"):  # an approved design is no longer in flight
                services.catalog.register({**_design_entry(state, state["design"]), "status": "abandoned"})
            return Command(goto=END, update={
                "gate_decisions": records, "status": "aborted",
                "events": [event(gate, "aborted", d["feedback"], approver=d["approver"])] + learned,
            })

        approvers = ", ".join(r["approver"] for r in records if r["decision"] in ("approve", "handback"))
        update: dict = human_update | {"gate_decisions": records, "approvals": {gate: True},
                                       "events": [event(gate, "approved", d["feedback"], approver=approvers, lanes=approving)]}
        if human is not None:
            update["events"].append(event(gate, "taken_over", f"{kind} written by {approvers}", approver=approvers,
                                          artifact=ref["id"]))
        update["events"] += learned
        if gate == "g4" and review is not None:
            # Approved designs are visible to every other feature of the product while in flight.
            design = {k: review[k] for k in ("builds_on", "supersedes", "contracts", "record")}
            design["adrs"] = _hld_adrs(services.artifacts.load(ref))
            services.catalog.register({**_design_entry(state, design), "status": "in_design"})
            update["design"] = design
            update["resources"] = aws_plan(services, state, snap, ref)
            # The approved cost is what the feature is held to after release (ops.cost).
            from ..ops.cost import plan_from_g4

            update["cost_plan"] = plan_from_g4(aws_summary(services, state, snap, ref), state, approvers)
            # The feature's flag exists before any code does, so the coder builds behind it.
            update["flag"], note = assign_flag(services, state, approvers)
            update["events"].append(note)
        if gate == "g6":
            release = dict(state.get("release", {}))
            # Every feature reaches production behind a flag. In a launch group it stays off until the
            # launch is approved; otherwise (alone, or in an approved train) it goes on now.
            rel = services.release_for(state["feature_id"]) if callable(getattr(services, "release_for", None)) else None
            key = state.get("flag", "")
            dark = bool(rel and rel["model"] == "launch" and rel["status"] != "cancelled")
            flag_on = False
            from ..ops.rollout import percent_label, plan_for

            # A gradual release when the platform asks for one: G6 approves the whole plan.
            plan = plan_for(getattr(snap, "remediation", None), state.get("risk_tier")) if key and not dark else None
            if key:
                try:
                    if not dark and plan:
                        services.flags.set_percent(state["product_id"], key, plan["steps"][0], actor=d["approver"])
                        update["events"].append(event(gate, "gradual_release", f"{key} on for {percent_label(plan['steps'][0])}; "
                                                      f"then {', '.join(percent_label(s) for s in plan['steps'][1:])}, each after "
                                                      f"{plan['bake_minutes']} quiet minutes", approver=approvers))
                    elif not dark:
                        services.flags.set(state["product_id"], key, True, actor=d["approver"])
                    flag_on = not dark
                except Exception as exc:  # the release still goes out, dark; say so plainly
                    plan = None
                    update["events"].append(event(gate, "flag_failed",
                                                  f"Couldn't turn on {key}: {exc}. Turn it on in Flipt.",
                                                  approver=d["approver"]))
            release |= {"flag": key, "flag_on": flag_on if key else None, "release_id": rel["id"] if rel else ""}
            if plan and flag_on:
                from ..ops.rollout import _clock as rollout_clock

                release |= {"rollout_plan": {**plan, "at": rollout_clock().isoformat(), "approved_by": approvers},
                            "flag_percent": plan["steps"][0]}
            # VX-3: production promotion is a human action, in each product's own runtime. In
            # production the approver promotes in the OpenChoreo portal and a watcher resumes this
            # thread; the stub records it here.
            lanes_by_repo = snap.lane_of_repo()
            # Managed services first, so the components find them ready (VX-19).
            for key, dep in sorted((state.get("deployments") or {}).items()):
                if dep.get("kind") == "resource":
                    update.setdefault("deployments", {})[key] = bind_resource(
                        services, state, dep, "production", actor=d["approver"], actor_type="human")
            for repo in release.get("components", []):
                prior = (state.get("deployments") or {}).get(repo) or {}
                if prior.get("kind") == "resource":
                    continue  # bound above
                if prior.get("kind") == "store":
                    # A store app: the approver's decision submits it to the stores (Play at 1%).
                    update.setdefault("deployments", {})[repo] = release_store_app(
                        services, state, repo, prior["sha"], "production", actor=d["approver"])
                    continue
                t = oc_target(lanes_by_repo.get(repo, snap.product), repo)
                env = env_for("production", t["pipeline"])
                rec = services.openchoreo.promote(component=t["component"], env=env, actor=d["approver"],
                                                  actor_type="human", project=t["project"],
                                                  namespace=t["namespace"], pipeline=t["pipeline"])
                update.setdefault("deployments", {})[repo] = {"repo": repo, **t, "env": env,
                                                              "release": rec.get("release", ""),
                                                              "pipeline": list(t["pipeline"])}
            # Shared lanes own no runtime: their packages are marked stable for consumers to adopt.
            for repo in release.get("packages", []):
                services.packages.publish(repo=repo, version=release.get("version", state["feature_id"]),
                                          channel="stable", actor=d["approver"])
            release |= {"production": bool(release.get("components")), "stable": release.get("packages", []),
                        "promoted_by": d["approver"]}
            update["release"] = release
            if release["production"] or release.get("flag_on"):
                record_change(services, release_record_for(state, update, records, approvers), update, gate)
        return Command(goto="production_wait" if gate == "g6" else target, update=update)

    dest = ("production_wait",) if gate == "g6" else (tuple(target) if isinstance(target, list) else (target,))
    return gate_node, (*dest, phase, END)


def release_record_for(state: FeatureState, update: dict, records: list[dict], approvers: str) -> dict:
    """The change record for a G6 release, from the feature and what this approval just did."""
    from ..ops.changes import release_record

    n = sum(1 for g in state.get("gate_decisions") or [] if g.get("gate") == "g6")
    now_state = {**state, "deployments": {**(state.get("deployments") or {}), **(update.get("deployments") or {})},
                 "release": update.get("release") or state.get("release") or {},
                 "gate_decisions": [*(state.get("gate_decisions") or []), *records]}
    return release_record(now_state, approvers, n)


def record_change(services, rec: dict, update: dict, node: str) -> None:
    """Write a production change to the change log. A log that can't be written is said plainly:
    the change happened, and an auditor will look for it."""
    log = getattr(services, "changes", None)
    if log is None:
        return
    try:
        stored = log.record(rec)
        update.setdefault("events", []).append(event(node, "change_recorded", f"{stored['id']}: {rec['summary']}"))
    except Exception as exc:
        update.setdefault("events", []).append(event(node, "change_not_recorded", f"{type(exc).__name__}: {exc}"))


def assign_flag(services, state: FeatureState, actor: str) -> tuple[str, dict]:
    """Create the feature's flag (off in production, on before it) and return its key and an event.
    No flag service, or a failure, means no flag: the feature ships without one, and a launch
    holds it after staging instead of shipping it dark."""
    from ..integrations.flags import flag_key

    pid = state["product_id"]
    if state.get("flag"):
        return state["flag"], event("g4", "flag", f"Keeps feature flag {state['flag']}")
    if not services.flags.available(pid):
        return "", event("g4", "flag", "No flag service for this product: the feature ships without a flag")
    key = flag_key(pid, state["feature_id"], state["title"])
    try:
        services.flags.ensure(pid, key, name=f"{state['feature_id']} {state['title']}",
                              description=f"Vrixel feature {state['feature_id']}: {state['title']}", actor=actor)
    except Exception as exc:
        return "", event("g4", "flag_failed", f"Couldn't create feature flag {key}: {exc}. "
                         "The feature continues without a flag.")
    return key, event("g4", "flag", f"Created feature flag {key}: off in production, on before it")


def design_review_for(services, snap, ref, state: FeatureState) -> dict | None:
    from ..knowledge import design_review

    pack, _ = design_knowledge(services, snap)
    if not pack:
        return None
    return design_review(services.artifacts.load(ref), pack, state["feature_id"])


def _hld_adrs(hld: str) -> list[dict]:
    """The HLD's own ADR lines as {id, title, supersedes}."""
    import re

    from ..agents.parse import bullets, section

    out = []
    for line in bullets(section(hld, "ADRs")):
        m = re.match(r"(ADR-[A-Za-z0-9-]+):\s*(.*)", line)
        if m:
            sup = re.search(r"[Ss]upersedes\s+(ADR-[A-Z0-9-]+)", line)
            out.append({"id": m.group(1), "title": m.group(2).strip(), "supersedes": sup.group(1) if sup else ""})
    return out


def _design_entry(state: FeatureState, design: dict) -> dict:
    return {"id": capability_id(state), "product_id": state["product_id"], "name": state["title"],
            "feature": state["feature_id"], "adrs": design.get("adrs", []),
            "contracts": [c for c in design.get("contracts", []) if c.get("next")]}


def make_escalation(phase: str, services):
    spec = PHASE_SPECS[phase]

    def escalate(state: FeatureState) -> Command:
        esc = state.get("escalations", {}).get(phase, {})
        last = (state.get("artifacts") or {}).get(spec.kind)
        draft = services.artifacts.load(last) if last and phase in DOC_PHASES else ""
        packet = {
            "type": "escalation", "phase": phase, "feature_id": state["feature_id"],
            "product_id": state["product_id"], "title": state["title"],
            "reason": esc.get("reason", ""), "findings": esc.get("findings", []),
            "artifact": state.get("artifacts", {}).get(spec.kind),
            "cost_usd": round(float(state.get("cost_usd", 0.0)), 4),
            "budget_usd": state.get("token_budget_usd"),
            # What people already asked for, and the agents' last draft to finish yourself.
            "history": correction_history(state, phase), "revisions": times_sent_back(state, phase),
            "content": draft,
            "allowed": list(ESCALATION_DECISIONS),
        }
        d = normalize(interrupt(packet), ESCALATION_DECISIONS)
        rec = _record(f"escalation:{phase}", phase, d)
        update: dict = {"gate_decisions": [rec]}
        mine = d.get("artifact_content") or ""
        changed = d["decision"] == "resolved" and bool(mine.strip()) and mine.strip() != draft.strip()
        said = correction(state, f"escalation:{phase}", phase, "resolved" if changed else d["decision"], d,
                          len(state.get("gate_decisions") or []) + 1,
                          changes=lessons.changes(draft, mine) if changed else None)
        teaches = d["decision"] == "retry" or changed or said["remember"]
        learned = lessons.learn(services, state, [said] if teaches else [])
        if d.get("budget_usd"):  # a human may raise the budget, up to the platform ceiling
            update["token_budget_usd"] = min(float(d["budget_usd"]), services.settings.limits.max_budget_usd)

        if d["decision"] == "abort":
            update |= {"status": "aborted", "events": [event(f"escalate_{phase}", "aborted", d["feedback"])] + learned}
            return Command(goto=END, update=update)
        if d["decision"] == "retry":
            update |= {"feedback": {phase: d["feedback"]},
                       "events": [event(f"escalate_{phase}", "retry", d["feedback"], approver=d["approver"])] + learned}
            return Command(goto=phase, update=update)

        # resolved: the human took the phase over and hands back a finished artifact.
        update |= {"phase_status": {phase: "resolved"}, "escalations": {phase: {**esc, "resolved": True}},
                   "events": [event(f"escalate_{phase}", "resolved", d["feedback"], approver=d["approver"])] + learned}
        if mine.strip() and (changed or not draft):
            update |= human_artifact(services, state, phase, mine)
        if phase == "plan":
            ref = update.get("artifacts", {}).get("PLAN") or state.get("artifacts", {}).get("PLAN")
            update["tasks"] = parse_tasks(services.artifacts.load(ref)) if ref else []
        if phase == "architecture":
            ref = update.get("artifacts", {}).get("HLD") or state.get("artifacts", {}).get("HLD")
            snap = services.feature_snapshot(state)
            affected = [r["name"] for r in parse_affected_repos(services.artifacts.load(ref))] if ref else []
            update["lanes_touched"] = touched_lanes(snap, affected)
        if spec.gate in CROSS_LANE_GATES:
            # Other lanes may be affected, so their owners still decide at the gate.
            return Command(goto=spec.gate, update=update)
        if spec.gate:  # the human's resolution stands in for the gate approval
            update["approvals"] = {spec.gate: True}
            target = GATE_APPROVE_TARGET[spec.gate]
            return Command(goto=target, update=update)
        nxt = after_ci(state) if phase == "build" else after_phase(phase, state)  # a person vouched for CI
        return Command(goto=nxt, update=update)

    targets = {phase, END}
    if spec.gate in CROSS_LANE_GATES:
        targets.add(spec.gate)
    elif spec.gate:
        t = GATE_APPROVE_TARGET[spec.gate]
        targets |= set(t) if isinstance(t, list) else {t}
    elif phase == "build":
        targets |= {"g5", "merge_train"}
    else:
        targets.add(NEXT_AFTER_PHASE[phase])
    return escalate, tuple(sorted(targets))


def join(state: FeatureState) -> Command:
    """Plan starts only when both G3 (design) and G4 (architecture) are approved."""
    a = state.get("approvals", {})
    if a.get("g3") and a.get("g4"):
        return Command(goto="provision", update={"events": [event("join", "design_and_architecture_approved")]})
    return Command(goto=END, update={"events": [event("join", "waiting_for_other_gate")]})


PROVISION_DECISIONS = ("retry", "resolved", "abort")


def proposed_repos(services, state: FeatureState) -> list[dict]:
    ref = (state.get("artifacts") or {}).get("HLD")
    return parse_new_repos(services.artifacts.load(ref)) if ref else []


def repo_plans(services, state: FeatureState) -> list[dict]:
    """What G4 approves and the provision step applies, resolved from the golden path."""
    import os

    from ..provisioning import plan

    product = services.snapshot(state["product_id"], state.get("governance")).product
    out = []
    for p in proposed_repos(services, state):
        rp = plan(product, p, product.repo_standards or {"defaults": {}, "roles": {}})
        if os.environ.get("VRIXEL_GITHUB_APP_ID"):
            rp["ruleset"]["bypass_app_id"] = os.environ["VRIXEL_GITHUB_APP_ID"]
        out.append(rp)
    return out


def make_provision(services):
    """Create the repos G4 approved, on the golden path, before planning assigns work to them."""

    def provision(state: FeatureState) -> Command:
        plans = repo_plans(services, state)
        if not plans:
            return Command(goto="plan")
        snap = services.snapshot(state["product_id"], state.get("governance"))
        entry = services.settings.entry(state["product_id"])
        gov = entry.governance
        src = services.governance.source
        manifest = src.read(gov.org, gov.repo, "product.yaml", src.resolve(gov.org, gov.repo, gov.ref)) or ""
        done = {r["repo"]: r for r in state.get("new_repos") or [] if r.get("status") == "provisioned"}
        results = [done[p["repo"]] if p["repo"] in done else
                   services.provisioner.provision(snap.product, p, feature_id=state["feature_id"],
                                                  governance_repo=gov.full_name, manifest_text=manifest)
                   for p in plans]
        events = [event("provision", "repo_provisioned", r["repo"], url=r["url"], governance_pr=r["governance_pr"])
                  for r in results if r["status"] == "provisioned" and r["repo"] not in done]
        failed = [r for r in results if r["status"] != "provisioned"]
        if not failed:
            return Command(goto="plan", update={"new_repos": results, "events": events})

        reason = "; ".join(f"{r['repo']}: " + next((f"{s['step']} failed ({s['error']})" for s in r["steps"]
                                                    if not s["ok"]), "failed") for r in failed)
        d = normalize(interrupt({
            "type": "escalation", "phase": "provisioning", "feature_id": state["feature_id"],
            "product_id": state["product_id"], "title": state["title"],
            "reason": f"Couldn't finish setting up new repositories: {reason}",
            "findings": [f"{r['repo']}: {s['step']}: {s['error']}" for r in failed for s in r["steps"] if not s["ok"]],
            "new_repos": results, "cost_usd": round(float(state.get("cost_usd", 0.0)), 4),
            "budget_usd": state.get("token_budget_usd"), "allowed": list(PROVISION_DECISIONS)}), PROVISION_DECISIONS)
        rec = _record("escalation:provisioning", "provisioning", d)
        if d["decision"] == "abort":
            return Command(goto=END, update={"new_repos": results, "gate_decisions": [rec], "status": "aborted",
                                             "events": events + [event("provision", "aborted", d["feedback"],
                                                                       approver=d["approver"])]})
        if d["decision"] == "retry":
            return Command(goto="provision", update={"new_repos": results, "gate_decisions": [rec],
                                                     "events": events + [event("provision", "retry", d["feedback"],
                                                                               approver=d["approver"])]})
        # resolved: a person finished the setup by hand
        fixed = [{**r, "status": "provisioned", "completed_by": d["approver"]} if r["status"] != "provisioned" else r
                 for r in results]
        return Command(goto="plan", update={"new_repos": fixed, "gate_decisions": [rec],
                                            "events": events + [event("provision", "resolved", d["feedback"],
                                                                      approver=d["approver"])]})

    return provision, ("plan", "provision", END)


def oc_target(lane, repo: str) -> dict:
    """Where a repo builds and runs in OpenChoreo, from the owning product's runtime block."""
    rt = (lane.runtime if lane is not None else None) or {}
    return {"component": (rt.get("components") or {}).get(repo, repo), "project": rt.get("openchoreo_project"),
            "namespace": rt.get("namespace"), "pipeline": tuple(rt.get("pipeline") or PIPELINE)}


def _resource_owner(snap):
    """component short name -> (project, namespace): the component's lane runtime, or the product's
    for shared lanes (they run inside the product)."""
    lanes = snap.lane_of_repo()
    product_rt = snap.product.runtime or {}

    def target(component: str):
        repo = next((r for r in lanes if r.split("/", 1)[-1] == component), None)
        rt = ((lanes[repo].runtime if repo else None) or product_rt)
        return rt.get("openchoreo_project"), rt.get("namespace")

    return target


def _ensure_alert_rules(services, snap, target: dict) -> list[str]:
    """Give a component the rules it lacks. A failure here never stops a deployment; the next
    deployment tries again."""
    from ..ops.alert_rules import traits_for

    traits = traits_for(getattr(snap, "alert_rules", None) or {}, target["component"].rsplit("/", 1)[-1])
    ensure = getattr(services.openchoreo, "ensure_traits", None)
    if not traits or not callable(ensure):
        return []
    try:
        return ensure(component=target["component"], traits=traits, project=target["project"],
                      namespace=target["namespace"])
    except Exception:
        return []


def _alarms_firing(services, state: FeatureState) -> list[dict]:
    from ..ops.alarms import for_feature

    try:
        return for_feature(getattr(services, "alarms", None), state)
    except Exception:
        return []


def _build_cost(services, state: FeatureState) -> dict | None:
    from ..finops.views import build_so_far

    try:
        return build_so_far(getattr(services, "cost_ledger", None), state)
    except Exception:
        return None


def aws_summary(services, state: FeatureState, snap, ref) -> dict | None:
    """G4 and G6 see the AWS services, each component's access and the cost per environment (VX-19)."""
    from .. import resources

    if not ref or not snap.aws_catalog:
        return None
    rt = snap.product.runtime or {}
    return resources.summary(services.artifacts.load(ref) if isinstance(ref, dict) else "",
                             snap.aws_catalog, list(rt.get("pipeline") or PIPELINE), rt.get("aws"))


def aws_plan(services, state: FeatureState, snap, ref) -> list[dict]:
    from .. import resources

    if not ref or not snap.aws_catalog:
        return []
    return resources.plan(services.artifacts.load(ref), snap.aws_catalog, _resource_owner(snap),
                          product_id=state["product_id"], feature_id=state["feature_id"],
                          data_class=snap.product.data_class)


def bind_resource(services, state: FeatureState, res: dict, stage: str, *, actor: str, actor_type: str) -> dict:
    """Create or update an approved Resource and bind it to the stage's environment with its size."""
    from ..resources import size_for

    snap = services.feature_snapshot(state)
    pipeline = tuple((snap.product.runtime or {}).get("pipeline") or PIPELINE)
    env = env_for(stage, pipeline)
    spec = res.get("spec") or res
    applied = services.openchoreo.apply_resource(name=spec["name"], type=spec["type"], parameters=spec["parameters"],
                                                 project=spec["project"], namespace=spec.get("namespace"))
    t = (snap.aws_catalog.get("types") or {}).get(spec["type"]) or {}
    size = (size_for({"type": spec["type"], "sizes": spec.get("sizes") or {}}, env, snap.aws_catalog)
            if t.get("sizes") else "small")
    rec = services.openchoreo.bind_resource(name=spec["name"], env=env, size=size, actor=actor, actor_type=actor_type,
                                            project=spec["project"], namespace=spec.get("namespace"), pipeline=pipeline,
                                            # Stored data outlives its binding: OpenChoreo keeps it (VX-19).
                                            retain=bool(spec.get("stateful")))
    return {"repo": f"resource:{spec['name']}", "kind": "resource", "component": spec["name"], "project": spec["project"],
            "namespace": spec.get("namespace"), "pipeline": list(pipeline), "env": env, "size": size,
            "release": rec.get("release") or applied.get("release", ""), "change": applied.get("change", ""),
            "spec": spec, "bound_by": actor}


def env_for(stage: str, pipeline: tuple[str, ...]) -> str | None:
    """dev is the first environment, production the last, staging the one in between (if any)."""
    if stage == "dev":
        return pipeline[0]
    if stage == "production":
        return pipeline[-1]
    return pipeline[1] if len(pipeline) > 2 else None


def make_merge_train(services):
    def merge_train(state: FeatureState) -> dict:
        cs = dict(state.get("change_set") or {})
        risk = state.get("risk_tier", "low")
        lanes = services.feature_snapshot(state).lane_of_repo()
        g5 = [g for g in state.get("gate_decisions", []) if g["gate"] == "g5" and g["decision"] in ("approve", "handback")]
        actor = g5[-1]["approver"] if (risk != "low" and g5) else "agent:merge-train"
        merged, published, runs = {}, list(cs.get("published") or []), dict(cs.get("runs") or {})
        passed = ", ".join(sorted({g["gate"].upper() for g in state.get("gate_decisions", [])
                                   if g["decision"] in ("approve", "handback")})) or "gates"
        failed_runs = {r for r, v in runs.items() if v.get("state") == "failure"}
        roles = {t["repo"]: t.get("role") for t in state.get("tasks") or []}
        for repo in cs.get("merge_order", []):  # dependency order: libraries, providers, consumers
            sha = ""
            for key, pr in sorted(state.get("pull_requests", {}).items()):
                if pr["repo"] != repo:
                    continue
                if pr.get("state") != "merged":  # idempotent: a retry skips what is merged
                    # Every gate this change needs is approved: say so on the commit the gates saw,
                    # and merge exactly that commit.
                    gates_status(services, [pr], "success", f"{state['feature_id']}: {passed} approved")
                    res = services.github.merge(repo=repo, number=pr["number"], actor=actor,
                                                sha=pr.get("head_sha") or "")
                    pr = {**pr, "state": res["state"], "merged_by": actor, "merge_sha": res.get("merge_sha")}
                    merged[key] = pr
                sha = pr.get("merge_sha") or sha
            lane = lanes.get(repo)
            if roles.get(repo) == "acceptance":
                # Not deployed: the pipeline runs these tests against each environment (env waits).
                cs["acceptance"] = {"repo": repo, "sha": sha}
                continue
            if roles.get(repo) in store.STORE_ROLES:
                # Released through the stores by its own workflow at each stage (vrixel_sdlc.store).
                cs.setdefault("store", {})[repo] = {"repo": repo, "sha": sha}
                continue
            if lane is not None and lane.kind == "shared":
                # No runtime of its own: publish a release candidate that consumers build against.
                if repo not in published:
                    services.packages.publish(repo=repo, version=state["feature_id"], channel="rc", actor=actor)
                    published.append(repo)
            elif repo not in runs or repo in failed_runs:  # build the merge commit in OpenChoreo
                t = oc_target(lane, repo)
                run = services.openchoreo.run_workflow(component=t["component"], ref=sha or "main", kind="main",
                                                       project=t["project"], namespace=t["namespace"])
                runs[repo] = {"repo": repo, "sha": sha, **t, "pipeline": list(t["pipeline"]),
                              "run_id": run["run_id"], "url": run.get("url", "")}
        cs |= {"merged": True, "published": published, "runs": runs}
        return {"pull_requests": merged, "change_set": cs,
                "events": [event("merge_train", "merged", f"{len(merged)} PRs by {actor}",
                                 order=cs.get("merge_order", []), published=published)]}

    return merge_train


def external(state: FeatureState, wait: str, targets: list[dict], quality: dict | None = None) -> dict:
    packet = {"type": "external", "wait": wait, "label": LABELS[wait], "feature_id": state["feature_id"],
              "product_id": state["product_id"], "title": state["title"], "targets": targets, "since": now(),
              "timeout_s": TIMEOUT_S[wait]}
    if quality:
        packet["quality"] = quality
    return packet


def quality_packet(services, state: FeatureState, wait: str, targets: list[dict]) -> dict | None:
    """The product's suites for this stage, travelling with the wait so the checker needs no state."""
    from ..quality import WAIT_STAGE

    stage = WAIT_STAGE[wait]
    policy = services.feature_snapshot(state).product.quality or {}
    mine = {k: v for k, v in policy.items() if v.get("stage") == stage}
    if not mine or not targets:
        return None
    return {"stage": stage, "policy": mine, "targets": targets, "waivers": list(state.get("waivers") or [])}


def _quality_update(res: dict) -> dict:
    q = res.get("quality")
    return {"quality": {q["stage"]: {**q, "at": res.get("at", now())}}} if q else {}


def security_review(services, state: FeatureState, wait: str, res: dict, *, ok: str, retry: str,
                    retry_update: dict | None = None, extra: dict | None = None) -> Command:
    """Blocking findings go to the lane's security approvers (approvers.security, else G5's).
    retry: fix (before merge, agents get the findings) or scan again; accept_risk: a waiver with a
    reason and an expiry; abort: stop the feature."""
    q = res["quality"]
    packet = {"type": "escalation", "phase": "security", "wait": wait, "stage": q["stage"],
              "feature_id": state["feature_id"], "product_id": state["product_id"], "title": state["title"],
              "reason": res.get("reason", ""), "blocking": q["blocking"],
              "findings": [f"{f['severity'].upper()} {f['title']} ({f['repo']}, {f['tool']})" for f in q["blocking"]],
              "max_waiver_days": services.settings.quality.waiver_max_days if services.settings.quality else 90,
              "cost_usd": round(float(state.get("cost_usd", 0.0)), 4), "budget_usd": state.get("token_budget_usd"),
              "allowed": list(SECURITY_DECISIONS)}
    d = normalize(interrupt(packet), SECURITY_DECISIONS)
    update = {"gate_decisions": [_record(f"escalation:security:{q['stage']}", "security", d)],
              "delivery": {wait: res}, **_quality_update(res), **(extra or {})}
    if d["decision"] == "abort":
        return Command(goto=END, update=update | {"status": "aborted", "events": [
            event("security", "aborted", d["feedback"], approver=d["approver"])]})
    if d["decision"] == "accept_risk":
        from ..quality import waiver

        max_days = packet["max_waiver_days"]
        waivers = [waiver(f, approver=d["approver"], reason=d["feedback"], days=d.get("expires_days") or max_days,
                          max_days=max_days) for f in q["blocking"]]
        accepted = {w["fingerprint"] for w in waivers}
        q = {**q, "status": "passed", "blocking": [], "suites": [
            {**r, "waived": sorted(set(r.get("waived") or []) | {f["fingerprint"] for f in r.get("findings") or []
                                                               if f["fingerprint"] in accepted})}
            for r in q["suites"]]}
        update |= {"quality": {q["stage"]: {**q, "at": now()}}, "delivery": {wait: {**res, "status": "accepted"}}}
        return Command(goto=ok, update=update | {"waivers": waivers, "events": [
            event("security", "accepted_risk", d["feedback"], approver=d["approver"], findings=len(waivers),
                  expires=waivers[0]["expires"] if waivers else "")]})
    return Command(goto=retry, update=update | (retry_update or {}) | {"events": [
        event("security", "retry", d["feedback"] or res.get("reason", ""), approver=d["approver"])]})


def ask_release_managers(state: FeatureState, wait: str, res: dict) -> dict:
    """A failed CI-to-production step goes to people, like a stuck phase."""
    packet = {"type": "escalation", "phase": "delivery", "wait": wait, "feature_id": state["feature_id"],
              "product_id": state["product_id"], "title": state["title"], "reason": res.get("reason", ""),
              "findings": [f"{r.get('repo') or r.get('component')}: {r.get('message') or r.get('state')}"
                           for r in res.get("results", []) if r.get("state") != "success"],
              "results": res.get("results", []), "cost_usd": round(float(state.get("cost_usd", 0.0)), 4),
              "budget_usd": state.get("token_budget_usd"), "allowed": list(ESCALATION_DECISIONS)}
    return normalize(interrupt(packet), ESCALATION_DECISIONS)


def _people_said(state, wait: str, res: dict, *, ok: str, retry: str, extra: dict | None = None) -> Command:
    d = ask_release_managers(state, wait, res)
    update = {"gate_decisions": [_record(f"escalation:delivery:{wait}", "delivery", d)],
              "delivery": {wait: res}, **(extra or {})}
    if d["decision"] == "abort":
        return Command(goto=END, update=update | {"status": "aborted",
                                                  "events": [event(f"{wait}_wait", "aborted", d["feedback"],
                                                                   approver=d["approver"])]})
    kind = "retry" if d["decision"] == "retry" else "resolved"
    return Command(goto=retry if kind == "retry" else ok,
                   update=update | {"events": [event(f"{wait}_wait", kind, d["feedback"] or res.get("reason", ""),
                                                     approver=d["approver"])]})


def report_to_github(services, state: FeatureState, wait: str, res: dict) -> None:
    """Post OpenChoreo's build and deploy results on the merge commit, so GitHub shows how far each
    commit got (contexts openchoreo/build, openchoreo/dev, ...). Best effort: never blocks delivery."""
    runs = (state.get("change_set") or {}).get("runs") or {}
    for r in res.get("results", []):
        run = runs.get(r.get("repo"), {})
        if not run.get("sha"):
            continue
        ok = r.get("state") == "success"
        try:
            services.github.set_status(
                repo=r["repo"], sha=run["sha"], context=f"openchoreo/{wait}",
                state="success" if ok else "failure",
                description=(f"{LABELS[wait]}: done" if ok else (r.get("message") or f"{LABELS[wait]} failed")),
                url=r.get("url") or run.get("url", ""))
        except Exception:
            pass


def make_ci_wait(services):
    def ci_wait(state: FeatureState) -> Command:
        prs = [pr for pr in (state.get("pull_requests") or {}).values()
               if pr.get("state") != "merged" and pr.get("head_sha")]
        nxt = after_ci(state)
        if not prs:
            return Command(goto=nxt)
        perf = state.get("performance") or {}
        targets = [{"repo": p["repo"], "number": p["number"], "sha": p["head_sha"], "base": p.get("base") or "main",
                    "url": p["url"], "perf": (perf.get(p["repo"]) or {}).get("level", "none")} for p in prs]
        res = interrupt(external(state, "ci", targets, quality_packet(services, state, "ci", targets)))
        if res["status"] == "success":
            return Command(goto=nxt, update={"delivery": {"ci": res}, **_quality_update(res), "ci_retries": 0,
                                             "events": [event("ci_wait", "passed", f"CI green on {len(prs)} PRs")]})
        if res["status"] == "security":
            fix = "Fix these security findings:\n" + "\n".join(
                f"- {f['severity']} {f['title']} in {f['repo']} ({f['url']})" for f in res["quality"]["blocking"])
            return security_review(services, state, "ci", res, ok=nxt, retry="build",
                                   retry_update={"feedback": {"build": fix}})
        findings = (list((res.get("quality") or {}).get("test_failures") or []) or
                    [f"{r['repo']}#{r['number']}: {r.get('message') or 'checks failed'}"
                     for r in res["results"] if r["state"] == "failure"])
        # Red CI goes back to the agents first, with what failed, and only the failing repos' tasks
        # are redone. People see it only when the retries run out (the product's max_loops).
        tries = int(state.get("ci_retries") or 0)
        limit = services.feature_snapshot(state).product.max_loops
        failing = sorted({r["repo"] for r in res.get("results") or [] if r.get("state") == "failure"})
        if tries < limit and failing:
            fix = "CI failed on the pull requests. Fix these failures:\n" + "\n".join(f"- {f}" for f in findings)
            return Command(goto="build", update={
                "delivery": {"ci": res}, **_quality_update(res), "ci_retries": tries + 1, "rerun_repos": failing,
                "feedback": {"build": fix},
                "events": [event("ci_wait", "retry", f"CI red in {', '.join(failing)}: agents fix it "
                                 f"(try {tries + 1} of {limit})", findings=findings)]})
        # The retries ran out: people decide (let agents try again, vouch for it, or stop).
        return Command(goto="escalate_build", update={
            "delivery": {"ci": res}, **_quality_update(res), "phase_status": {"build": "escalated"},
            "escalations": {"build": {"phase": "build", "reason": res["reason"], "findings": findings}},
            "events": [event("ci_wait", "failed", res["reason"])]})

    return ci_wait, ("g5", "merge_train", "escalate_build", "build", END)


def make_build_wait(services):
    def build_wait(state: FeatureState) -> Command:
        runs = (state.get("change_set") or {}).get("runs") or {}
        todo = [r for r in runs.values() if r.get("state") != "success"]
        if not todo:
            return Command(goto="deploy_dev")
        res = interrupt(external(state, "build", todo, quality_packet(services, state, "build", todo)))
        report_to_github(services, state, "build", res)
        cs = dict(state.get("change_set") or {})
        cs["runs"] = {**runs, **{r["repo"]: {**runs[r["repo"]], "state": r["state"]} for r in res["results"]}}
        if res["status"] == "security":
            return security_review(services, state, "build", res, ok="deploy_dev", retry="build_wait",
                                   extra={"change_set": cs})
        if res["status"] == "success":
            return Command(goto="deploy_dev", update={"delivery": {"build": res}, "change_set": cs, **_quality_update(res),
                                                      "events": [event("build_wait", "passed",
                                                                       f"{len(todo)} OpenChoreo builds succeeded")]})
        return _people_said(state, "build", res, ok="deploy_dev", retry="merge_train",
                            extra={"change_set": cs, **_quality_update(res)})

    return build_wait, ("deploy_dev", "merge_train", "build_wait", END)


def make_deploy(stage: str, services, nxt: str):
    """Agents deploy to dev and staging only (VX-3); each component in its own product's runtime."""

    def deploy(state: FeatureState) -> Command:
        snap = services.feature_snapshot(state)
        lanes = snap.lane_of_repo()
        deps, failures, notes = {}, [], []
        # Managed services first, so the components that use them find them (VX-19).
        for spec in state.get("resources") or []:
            try:
                deps[f"resource:{spec['name']}"] = bind_resource(services, state, spec, stage,
                                                                 actor=f"agent:{stage}", actor_type="agent")
            except Exception as exc:
                failures.append({"repo": f"resource:{spec['name']}", "component": spec["name"], "state": "failure",
                                 "message": f"{type(exc).__name__}: {exc}"})
        for repo, run in ((state.get("change_set") or {}).get("runs") or {}).items():
            t = oc_target(lanes.get(repo), repo)
            env = env_for(stage, t["pipeline"])
            if env is None:
                continue
            # The platform's default alert rules, before the component runs anywhere (alert-rules.yaml).
            added = _ensure_alert_rules(services, snap, t)
            if added:
                notes.append(f"{t['component']}: alert rules {', '.join(added)}")
            try:
                rec = services.openchoreo.promote(component=t["component"], env=env, actor=f"agent:{stage}",
                                                  actor_type="agent", project=t["project"],
                                                  namespace=t["namespace"], pipeline=t["pipeline"])
                deps[repo] = {"repo": repo, **t, "pipeline": list(t["pipeline"]), "env": env,
                              "release": rec.get("release", "")}
            except Exception as exc:
                failures.append({"repo": repo, "component": t["component"], "state": "failure",
                                 "message": f"{type(exc).__name__}: {exc}"})
        for repo, app in ((state.get("change_set") or {}).get("store") or {}).items():
            try:
                deps[repo] = release_store_app(services, state, repo, app["sha"], stage, actor=f"agent:{stage}")
            except Exception as exc:
                failures.append({"repo": repo, "component": repo, "state": "failure",
                                 "message": f"{type(exc).__name__}: {exc}"})
        if failures:
            res = {"status": "failure", "wait": stage, "results": failures, "at": now(),
                   "reason": f"Couldn't deploy to {stage}: " + "; ".join(f["message"] for f in failures)}
            return _people_said(state, stage, res, ok=nxt, retry=f"deploy_{stage}", extra={"deployments": deps})
        events = [event(f"deploy_{stage}", "deployed", ", ".join(sorted(deps)))] if deps else []
        events += [event(f"deploy_{stage}", "alert_rules", n) for n in notes]
        return Command(goto=f"{stage}_wait", update={"deployments": deps, "events": events})

    return deploy, (f"{stage}_wait", nxt, f"deploy_{stage}", END)


def make_env_wait(services, stage: str, ok: str, retry: str):
    def env_wait(state: FeatureState) -> Command:
        targets = [{"repo": d["repo"], "component": d["component"], "project": d.get("project"),
                    "namespace": d.get("namespace"), "env": d["env"],
                    **({"kind": "store", "sha": d.get("sha", "")} if d.get("kind") == "store" else {}),
                    **({"kind": "resource"} if d.get("kind") == "resource" else {})}
                   for d in (state.get("deployments") or {}).values()
                   if d.get("env") == env_for(stage, tuple(d.get("pipeline") or PIPELINE))]
        if not targets:
            return Command(goto=ok)
        runs = (state.get("change_set") or {}).get("runs") or {}
        roles = {t["repo"]: t.get("role") for t in state.get("tasks") or []}
        perf = state.get("performance") or {}
        qtargets = [{**t, "sha": t.get("sha") or runs.get(t["repo"], {}).get("sha", ""), "role": roles.get(t["repo"], ""),
                     "perf": (perf.get(t["repo"]) or {}).get("level", "none")}
                    for t in targets
                    # In production a store app is only submitted: its health is the rollout's to judge.
                    if not (t.get("kind") == "store" and stage == "production") and t.get("kind") != "resource"]
        qtargets += acceptance_targets(services, state, stage, [t for t in targets if t.get("kind") not in ("store", "resource")])
        packet = external(state, stage, targets, quality_packet(services, state, stage, qtargets))
        if stage == "staging" and any(t.get("perf") == "full" for t in qtargets):
            # Staging is smaller than production: the load test runs at a share of the expected peak.
            s = services.feature_snapshot(state).product.performance or {}
            packet["performance"] = {"perf_settings": {
                "peak_rps": s.get("peak_rps"), "load_factor": s.get("staging_load_factor"),
                "regression_pct": s.get("staging_regression_pct"), "budgets": s.get("budgets")}}
        if any(t.get("kind") == "store" for t in targets):
            packet["timeout_s"] = max(packet["timeout_s"], STORE_TIMEOUT_S[stage])
        res = interrupt(packet)
        report_to_github(services, state, stage, res)
        if res["status"] == "security":
            return security_review(services, state, stage, res, ok=ok, retry=f"{stage}_wait")
        if res["status"] == "success":
            return Command(goto=ok, update={"delivery": {stage: res}, **_quality_update(res),
                                            "events": [event(f"{stage}_wait", "passed",
                                                             f"{len(targets)} components ready in {stage}")]})
        return _people_said(state, stage, res, ok=ok, retry=retry, extra=_quality_update(res))

    return env_wait, (ok, retry, f"{stage}_wait", END)


def release_store_app(services, state: FeatureState, repo: str, sha: str, stage: str, *, actor: str) -> dict:
    """Ask a store app's repo to release this commit for the stage (vrixel_sdlc.store)."""
    services.github.dispatch(repo=repo, event_type="vrixel-release", payload={
        "stage": stage, "sha": sha, "feature_id": state["feature_id"], "actor": actor})
    return {"repo": repo, "kind": "store", "component": repo, "sha": sha, "env": stage,
            "pipeline": list(store.STAGES), "release": store.release_context(stage), "released_by": actor}


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
                services.github.dispatch(repo=a["repo"], event_type="vrixel-rollout", payload={
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
    from ..ops.remediation import ROLLOUT_AGENT

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
                from ..ops.cohorts import describe_groups

                cmp_ = (res.get("cohorts") or {}).get(key)
                history.append({"percent": steps[step], "until": res.get("at", now()), "quiet_since": res.get("quiet_since", ""),
                                "cohorts": describe_groups(key, cmp_) if cmp_ else "no comparison (no summary lines): time alone"})
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
    if not acc or not acc.get("sha") or stage not in ("dev", "staging"):
        return []
    repos = [{"name": t["repo"], "role": t.get("role")} for t in state.get("tasks") or []]
    arts = state.get("artifacts") or {}
    acs = list(parse_acs(services.artifacts.load(arts["PRD"]))) if "PRD" in arts else []
    return [{"kind": "acceptance", "repo": acc["repo"], "sha": acc["sha"], "feature_id": state["feature_id"],
             "acs": acs, "flag": state.get("flag", ""), "components": sorted({d["repo"] for d in deployed}),
             **acceptance.selection(state["feature_id"], repos, stage)}]


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
    g.add_node("join", join, destinations=("provision", END))
    node, dest = make_provision(services)
    g.add_node("provision", node, destinations=dest)
    g.add_node("merge_train", make_merge_train(services))
    node, dest = make_ci_wait(services)
    g.add_node("ci_wait", node, destinations=dest)
    node, dest = make_build_wait(services)
    g.add_node("build_wait", node, destinations=dest)
    for stage, after in (("dev", "deploy_staging"), ("staging", "release")):
        node, dest = make_deploy(stage, services, after)
        g.add_node(f"deploy_{stage}", node, destinations=dest)
        node, dest = make_env_wait(services, stage, ok=after, retry=f"deploy_{stage}")
        g.add_node(f"{stage}_wait", node, destinations=dest)
    node, dest = make_env_wait(services, "production", ok="rollout", retry="g6")
    g.add_node("production_wait", node, destinations=dest)
    node, dest = make_rollout(services)
    g.add_node("rollout", node, destinations=dest)
    node, dest = make_flag_rollout(services)
    g.add_node("flag_rollout", node, destinations=dest)
    g.add_node("finalize", finalize)

    g.add_node("hotfix_brief", make_hotfix_brief(services))
    g.add_edge(START, "intake")
    g.add_conditional_edges("intake", lambda s: "hotfix_brief" if s.get("track") == "hotfix" else "discovery",
                            ["hotfix_brief", "discovery"])
    g.add_edge("hotfix_brief", "plan")
    for phase in PHASES:
        g.add_conditional_edges(phase, _route(phase), _route_targets(phase))
    g.add_edge("merge_train", "build_wait")
    g.add_edge("finalize", END)
    return g.compile(checkpointer=checkpointer, name="vrixel_feature")

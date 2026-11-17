"""The human gates (G1-G6), their packets and records, and escalations."""

from __future__ import annotations

from langgraph.graph import END
from langgraph.types import Command, interrupt

from ..agents import GATE_APPROVE_TARGET, GATE_PHASE, NEXT_AFTER_PHASE, PHASE_SPECS
from ..agents.parse import parse_affected_repos, parse_cases, parse_tasks
from ..state import FeatureState
from .. import lessons, profiles
from .phase import capability_id, design_knowledge, event, gates_status, now, touched_lanes
from .briefs import hotfix_approvers
from .infra import _alarms_firing, _apply_capacity, _build_cost, _capacity_change, aws_plan, aws_summary, bind_resource, env_for, oc_target
from .provision import repo_plans
from .routing import CROSS_LANE_GATES, DOC_PHASES, ESCALATION_DECISIONS, GATE_DECISIONS, HANDBACK_DECISIONS, _record, after_ci, after_phase, approve_target, correction, correction_history, human_artifact, label_eval_case, normalize, release_store_app, times_sent_back


def quality_summary(state: FeatureState) -> dict:
    stages = state.get("quality") or {}
    suites = [{"stage": st, "suite": r["suite"], "kind": r["kind"], "repo": r.get("repo", ""), "state": r["state"],
               "coverage": r.get("coverage"), "passed": r.get("passed"), "failed": r.get("failed"),
               "findings": len(r.get("findings") or []), "waived": len(r.get("waived") or [])}
              for st, q in stages.items() for r in q.get("suites", [])]
    return {"suites": suites, "waivers": list(state.get("waivers") or [])}


def gate_notes(state: FeatureState, gate: str, phase: str) -> list[dict]:
    diaries = state.get("diaries") or {}
    notes = [{**n, "phase": phase} for n in diaries.get(phase) or []]
    if gate == "g4" and "g2" in profiles.get(state.get("profile")).skips_gates:
        notes += [{**n, "phase": "spec"} for n in diaries.get("spec") or []]
    if gate == "g6":  # the release decision sees what the coders assumed too
        notes += [{**n, "phase": "build"} for n in diaries.get("build") or []]
    from ..diary import ORDER

    return sorted(notes, key=lambda n: ORDER.index(n["kind"]))


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
            h = state.get("hotfix") or {}
            if h.get("source") == "finding" and h.get("hold"):
                # A scheduled security fix waits for the next release inside its deadline, or goes to
                # its own G6 on its release-by date (valxel_sdlc.upkeep). The answer decides `rel`,
                # so a resumed node takes the same path.
                win = interrupt({"type": "external", "wait": "fix_window", "feature_id": state["feature_id"],
                                 "product_id": state["product_id"], "release_by": h.get("release_by", ""),
                                 "since": now(), "label": (f"Waiting for the next release before {h.get('release_by')}, "
                                                           "or that date" if h.get("release_by") else
                                                           "Waiting for the next release")})
                rel = win.get("release") if win.get("status") == "joined" else None
            else:
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

        # A lite product hosted while it's built has no production yet: G6 waits for its go-live,
        # then asks for approval as usual (valxel_sdlc.environments.hosted).
        if gate == "g6" and preset is None:
            from ..environments import hosted

            if hosted(snap.product.runtime):
                interrupt({"type": "external", "wait": "go_live", "product_id": state["product_id"],
                           "feature_id": state["feature_id"], "since": now(),
                           "label": (f"Waiting for {state['product_id']} to go live: Valxel hosts it while it's "
                                     "built, so it has no production until it moves to the customer's VM")})

        limits = services.settings.limits
        history, sent_back = correction_history(state, phase), times_sent_back(state, phase)
        allowed = [x for x in GATE_DECISIONS if not (x == "revise" and sent_back >= limits.max_gate_revisions)]
        records, d = [], None
        trace_decision: tuple[dict, dict] | None = None
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
                "approvers": hotfix_approvers(state, lane, gate),
                "track": state.get("track", "standard"),
                "profile": state.get("profile", "feature"),
                # Express: no G2, so G4 approves the spec together with the design.
                "spec_preview": (services.artifacts.load(state["artifacts"]["PRD"])[:2000]
                                 if gate == "g4" and "g2" in profiles.get(state.get("profile")).skips_gates
                                 and (state.get("artifacts") or {}).get("PRD") else ""),
                "risk_tier": state.get("risk_tier"),
                # What the agents assumed, changed, traded off and left open in this phase (open first);
                # G4 under express also shows the spec's, since it approves the spec too.
                "agent_notes": gate_notes(state, gate, phase),
                # G5 and G6 see the evidence: every suite run so far, and any accepted risks.
                "quality": quality_summary(state) if gate in ("g5", "g6") else None,
                # G6 sees whether the events that measure the feature's goal fired in staging.
                "instrumentation": (state.get("instrumentation_check") or None) if gate == "g6" else None,
                # Each data contract's events, reconciled in staging (reconcile.py).
                "datasets": (state.get("datasets_check") or None) if gate == "g6" else None,
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
                # G6 sees each screen built in staging beside the mockup G3 approved (valxel_sdlc.screens).
                "screens": (__import__("valxel_sdlc.screens", fromlist=["g6"]).g6(state) if gate == "g6" else None),
                # G3 looks at a mockup of each screen, with what their checks found.
                "mockups": (state.get("mockups") or None) if gate == "g3" else None,
                # G4: what the design's added load does to the product's approved capacity (valxel_sdlc.capacity).
                "capacity_change": (_capacity_change(services, state, snap)
                                    if gate == "g4" and lane_id == state["product_id"] else None),
                # G4: whether the architect read the code, and which commit of each repo.
                "code_read": ({"checked": bool(state.get("code_read")), **(state.get("code_read") or {})}
                              if gate == "g4" else None),
                # G6 sees the CloudWatch alarms firing before production on what the feature touches.
                "alarms_firing": _alarms_firing(services, state) if gate == "g6" else [],
                # G6 sees how the change's own agents (repos with the agent role) did in their evals, by
                # stage, against the last release of the same agent.
                "agent_evals": agent_evals(services, state) if gate == "g6" and lane_id == state["product_id"] else None,
                # G5 and G6 can open each build session's trace: what the coder and reviewer did, step by step.
                "agent_sessions": (build_sessions(services, state) if gate in ("g5", "g6") else None),
                # G6: the traceability matrix must be complete, or a named person overrides it with a reason.
                "traceability": (release_traceability(services, state, lane)
                                 if gate == "g6" and lane_id == state["product_id"] else None),
                # G6 for a shared lane: each release candidate, and whether its workflow published it.
                "packages": (package_check(services, state, lane)
                             if gate == "g6" and lane.kind == "shared" else None),
                # G1 decides whether this intent is new: what the product already has that looks like it.
                "related_features": list(state.get("related_features") or []) if gate == "g1" else [],
                # G2 decides whether UX design runs: it does unless nothing people see changes.
                "ux_design": ({"runs": state.get("ui", True) is not False, "why": state.get("ui_why", "")}
                              if gate == "g2" and "design" not in profiles.get(state.get("profile")).skips else None),
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
            trace = packet.get("traceability") or {}
            while (d["decision"] == "approve" and trace.get("blocks")
                   and not str(d.get("override_reason") or "").strip()):
                # Production needs a complete matrix: ask again until it's an override with a reason
                # (the API checks the person may override) or another decision.
                packet = {**packet, "refused": "The traceability matrix isn't complete: release only with an "
                                              "override and its reason, or send it back."}
                d = normalize(interrupt(packet), tuple(allowed))
            while d["decision"] == "approve" and packet.get("packages") is not None:
                # Stable means the same build consumers tested: a candidate that was never published can't be it.
                packet = {**packet, "packages": package_check(services, state, lane)}  # look again each time
                missing = ", ".join(p["rc"] for p in packet["packages"] if p["status"] == "missing")
                if not missing:
                    break
                packet = {**packet, "refused": f"Not in GitHub Packages yet: {missing}. Approve once the release "
                                              "workflow has published it, or send it back."}
                d = normalize(interrupt(packet), tuple(allowed))
            records.append(_record(gate, phase, d, lane_id))
            if trace and d["decision"] == "approve":
                trace_decision = (trace, d)
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
        if preset is None and records:
            label_eval_case(services, state, phase, records[0], human=human,
                            reference=(human if human is not None else (services.artifacts.load(ref) if ref else ""))
                            if d["decision"] == "approve" else "")

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
        if gate == "g2":
            # G2 settles whether UX design runs: the creation-time judgement, or what the approver said.
            said = next((r.get("ui") for r in [d] if r.get("ui") is not None), None)
            ui = bool(said) if said is not None else state.get("ui", True) is not False
            update["ui"] = ui
            if said is not None and said != state.get("ui", True):
                update["ui_why"] = f"{'Turned on' if ui else 'Turned off'} at G2 by {d['approver']}"
                update["events"].append(event("g2", "ux_design_" + ("on" if ui else "off"), update["ui_why"],
                                              approver=d["approver"]))
            if not ui and "design" not in profiles.get(state.get("profile")).skips:
                update["approvals"] = {"g2": True, "g3": True}
                update["phase_status"] = {"design": "skipped"}
                update["events"].append(event("design", "skipped", "Nothing people see changes"))
            state = {**state, "ui": ui}
        if trace_decision is not None:
            trace, td = trace_decision
            update["traceability"] = {**trace["snapshot"], "drift": list(trace.get("drift") or []),
                                      "approved_by": td["approver"], "at": now(),
                                      **({"override": {"by": td["approver"], "reason": td["override_reason"]}}
                                         if trace.get("blocks") else {})}
            if trace.get("blocks"):
                update["events"].append(event(gate, "traceability_overridden", str(td["override_reason"])[:500],
                                              approver=td["approver"], gaps=len(trace["gaps"]),
                                              drift=len(trace.get("drift") or [])))
            if trace["snapshot"]["deferred"]:  # approving is accepting what the plan leaves out
                update["events"].append(event(gate, "deferrals_confirmed", "; ".join(
                    f"{x['id']}: {x['reason']}" for x in trace["snapshot"]["deferred"])[:1000], approver=td["approver"]))
        assumed = [n["text"] for n in gate_notes(state, gate, phase) if n["kind"] == "assumed"]
        if assumed:  # approving is confirming them, on the record (AI-DLC's assumption confirmation)
            update["events"].append(event(gate, "assumptions_confirmed", "; ".join(assumed)[:1000],
                                          approver=approvers, count=len(assumed)))
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
            update["capacity_change"] = _capacity_change(services, state, snap) or []
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
                sized = _apply_capacity(services, snap, t, env)
                update.setdefault("deployments", {})[repo] = {"repo": repo, **t, "env": env,
                                                              "release": rec.get("release", ""),
                                                              "pipeline": list(t["pipeline"]),
                                                              **({"capacity": sized} if sized else {})}
            # Shared lanes own no runtime: their packages are marked stable for consumers to adopt.
            # The same commit as the release candidate, tagged as the stable version.
            candidates = (state.get("change_set") or {}).get("packages") or {}
            versions = {}
            for repo in release.get("packages", []):
                rc = candidates.get(repo) or {}
                rec = services.packages.publish(repo=repo, version=rc.get("version") or release.get("version", state["feature_id"]),
                                                channel="stable", actor=d["approver"], sha=rc.get("sha", ""))
                versions[repo] = rec["version"]
            release |= {"production": bool(release.get("components")), "stable": release.get("packages", []),
                        "versions": versions, "promoted_by": d["approver"]}
            update["release"] = release
            if release["production"] or release.get("flag_on"):
                record_change(services, release_record_for(state, update, records, approvers), update, gate)
        return Command(goto="production_wait" if gate == "g6" else approve_target(gate, state), update=update)

    dest = ("production_wait",) if gate == "g6" else (tuple(target) if isinstance(target, list) else (target,))
    return gate_node, (*dest, phase, END)


# The suites that run an agent's evals (replayed on every PR, against the real model after dev),
# and a knowledge repo's retrieval evals (the golden set on every PR, the real models after dev).
AGENT_EVAL_SUITES = {"pr": "evals", "dev": "integration"}


EVAL_SUITES = {"agent": AGENT_EVAL_SUITES, "knowledge": {"pr": "retrieval", "dev": "integration"}}


def _eval_rows(state: dict, repo: str, role: str = "agent") -> dict[str, dict]:
    out = {}
    for stage, suite in EVAL_SUITES.get(role, AGENT_EVAL_SUITES).items():
        for r in ((state.get("quality") or {}).get(stage) or {}).get("suites") or []:
            if r.get("suite") == suite and r.get("repo") == repo and r.get("passed") is not None:
                ran = int(r.get("passed") or 0) + int(r.get("failed") or 0)
                out[stage] = {"suite": suite, "passed": int(r.get("passed") or 0), "failed": int(r.get("failed") or 0),
                              "rate": round(int(r.get("passed") or 0) / ran, 4) if ran else None, "state": r.get("state")}
    return out


def agent_evals(services, state: FeatureState) -> list[dict]:
    """For each agent or knowledge repo the change set changes: its evals by stage (on the PR, live
    in dev) and the same numbers from the last release that changed it, so G6 sees the trend."""
    roles = {t["repo"]: t.get("role") for t in state.get("tasks") or []}
    out = []
    for repo in sorted(r for r, role in roles.items() if role in EVAL_SUITES):
        role = roles[repo] or "agent"
        previous, prev_id = {}, ""
        suspects = getattr(services, "suspects", None)
        if callable(suspects):
            try:
                for f in suspects(state["product_id"], repo):
                    if f.get("feature_id") and f["feature_id"] != state["feature_id"] and not f.get("via"):
                        prev_state = services.feature_state(f["feature_id"]) if callable(getattr(services, "feature_state", None)) else {}
                        previous, prev_id = _eval_rows(prev_state or {}, repo, role), f["feature_id"]
                        break
            except Exception:
                previous = {}
        now_rows = _eval_rows(state, repo, role)
        trend = {st: round(now_rows[st]["rate"] - previous[st]["rate"], 4)
                 for st in now_rows if st in previous and now_rows[st]["rate"] is not None and previous[st]["rate"] is not None}
        out.append({"repo": repo, "role": role, "stages": now_rows, "previous": previous, "previous_feature": prev_id,
                    "change": trend})
    return out


def build_sessions(services, state: FeatureState) -> list[dict]:
    """The coder and reviewer sessions of this change set, with links to their traces."""
    from ..telemetry import sessions

    tel = getattr(services, "telemetry", None)
    try:
        records = services.ledger.records(state["feature_id"])
    except Exception:
        return []
    return sessions(records, tel.cfg.link if tel is not None else None, phases=("build",))


def package_check(services, state: FeatureState, lane) -> list[dict]:
    """Each of the lane's release candidates in this change set and whether it is published."""
    spec = {r.name: r.package for r in lane.repos}
    out = []
    for repo, rc in sorted(((state.get("change_set") or {}).get("packages") or {}).items()):
        if repo not in spec:
            continue
        try:
            status = services.packages.status(repo=repo, version=rc["package_version"], package=spec[repo])
        except Exception:
            status = "unknown"
        out.append({"repo": repo, "version": rc["version"], "rc": rc["tag"], "status": status,
                    "package": spec[repo]})
    return out


def release_traceability(services, state: FeatureState, lane) -> dict:
    """What G6 shows and checks: the matrix's gaps and deferrals, the tracker's drift from it, and
    who may release despite gaps. Hotfixes (decided by on-call) are shown it but never blocked."""
    from .. import traceability

    arts = state.get("artifacts") or {}

    def load(kind: str) -> str:
        try:
            return services.artifacts.load(arts[kind]) if kind in arts else ""
        except Exception:
            return ""

    trace = traceability.build(state, load)
    drift, drift_error, drift_blocks = [], "", False
    sync = getattr(services, "work_sync", None)
    if sync is not None:
        try:
            got = sync.reconcile(state)
            drift, drift_blocks = got.get("drift") or [], bool(got.get("block"))
        except Exception as exc:  # a tracker outage never decides a release
            drift_error = f"{type(exc).__name__}: {exc}"
    hotfix = state.get("track") == "hotfix"
    blocks = not hotfix and (not trace["complete"] or (drift_blocks and bool(drift)))
    return {"complete": trace["complete"], "gaps": trace["gaps"], "deferred": trace["deferred"],
            "drift": drift, "drift_error": drift_error, "drift_blocks": drift_blocks, "blocks": blocks,
            "override_approvers": list(lane.approvers.get("traceability") or lane.approvers.get("g2") or []),
            "snapshot": traceability.snapshot(trace)}


def release_record_for(state: FeatureState, update: dict, records: list[dict], approvers: str) -> dict:
    """The change record for a G6 release, from the feature and what this approval just did."""
    from ..ops.changes import release_record

    n = sum(1 for g in state.get("gate_decisions") or [] if g.get("gate") == "g6")
    now_state = {**state, "deployments": {**(state.get("deployments") or {}), **(update.get("deployments") or {})},
                 "release": update.get("release") or state.get("release") or {},
                 "traceability": update.get("traceability") or state.get("traceability"),
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
                              description=f"Valxel feature {state['feature_id']}: {state['title']}", actor=actor)
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
            "agent_notes": list((state.get("diaries") or {}).get(phase) or []),
            "allowed": list(ESCALATION_DECISIONS),
        }
        d = normalize(interrupt(packet), ESCALATION_DECISIONS)
        rec = _record(f"escalation:{phase}", phase, d)
        update: dict = {"gate_decisions": [rec]}
        mine = d.get("artifact_content") or ""
        changed = d["decision"] == "resolved" and bool(mine.strip()) and mine.strip() != draft.strip()
        if phase in DOC_PHASES and d["decision"] != "abort":
            label_eval_case(services, state, phase, {**rec, "decision": f"escalation_{d['decision']}"},
                            human=mine if changed else None,
                            reference=(mine if changed else draft) if d["decision"] == "resolved" else "")
        if d["decision"] == "resolved":
            rec["changed"] = changed  # the pilot report: did people fix the document, or accept it as it was?
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
            update["cases"] = parse_cases(services.artifacts.load(ref)) if ref else []
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
            if phase == "spec" and "g2" in profiles.get(state.get("profile")).skips_gates:
                return Command(goto="architecture", update=update)  # express: G4 reviews the spec too
            return Command(goto=approve_target(spec.gate, state), update=update)
        nxt = after_ci(state) if phase == "build" else after_phase(phase, state)  # a person vouched for CI
        return Command(goto=nxt, update=update)

    targets = {phase, END}
    if spec.gate in CROSS_LANE_GATES:
        targets.add(spec.gate)
    elif spec.gate:
        t = GATE_APPROVE_TARGET[spec.gate]
        targets |= set(t) if isinstance(t, list) else {t}
        if phase == "spec":
            targets.add("architecture")
    elif phase == "build":
        targets |= {"g5", "merge_train"}
    else:
        targets.add(NEXT_AFTER_PHASE[phase])
    return escalate, tuple(sorted(targets))


def join(state: FeatureState) -> Command:
    """Plan starts only when both G3 (design) and G4 (architecture) are approved."""
    a = state.get("approvals", {})
    if a.get("g3") and a.get("g4"):  # then check they fit together (valxel_sdlc.fit)
        return Command(goto="fit", update={"events": [event("join", "design_and_architecture_approved")]})
    return Command(goto=END, update={"events": [event("join", "waiting_for_other_gate")]})

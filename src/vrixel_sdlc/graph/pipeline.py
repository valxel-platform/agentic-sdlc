"""The feature pipeline: supervisor routing, eight phase subgraphs, six human gates.

discovery -> G1 -> spec -> G2 -> (design -> G3 | architecture -> G4) -> join -> plan -> build
  -> ci_wait -> [G5 if high-risk] -> merge_train -> build_wait -> deploy_dev -> dev_wait
  -> deploy_staging -> staging_wait -> release -> G6 (promotes production) -> production_wait
  -> operate -> finalize

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
from ..waits import LABELS, TIMEOUT_S
from .phase import capability_id, design_knowledge, event, make_phase_node, now, touched_lanes

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
    return GateDecision(gate=gate, phase=phase, lane=lane, decision=d["decision"], approver=d["approver"],
                        feedback=d["feedback"], active_minutes=d["active_minutes"], at=now())


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

        records, d = [], None
        for lane_id in approving:
            lane = lanes[lane_id]
            packet = {
                "type": "gate", "gate": gate, "phase": phase, "lane": lane_id, "lanes": approving,
                "feature_id": state["feature_id"], "product_id": state["product_id"], "title": state["title"],
                "constitution_version": state.get("constitution_version"),
                "artifact": ref, "preview": services.artifacts.load(ref)[:2000] if ref else "",
                "score": state.get("scores", {}).get(phase), "loops": state.get("loop_count", {}).get(phase),
                "cost_usd": round(float(state.get("cost_usd", 0.0)), 4),
                "budget_usd": state.get("token_budget_usd"),
                "approvers": lane.approvers.get(gate, []),
                "risk_tier": state.get("risk_tier"),
                # G5 and G6 see the evidence: every suite run so far, and any accepted risks.
                "quality": quality_summary(state) if gate in ("g5", "g6") else None,
                # Approving G4 approves creating these repos, with these settings.
                "new_repos": repo_plans(services, state) if gate == "g4" and lane_id == state["product_id"] else [],
                "design_review": review,
                # Each lane reviews the PRs in its own org.
                "pull_requests": sorted(k for k, pr in state.get("pull_requests", {}).items()
                                        if pr["repo"].split("/", 1)[0] == lane.org) if gate == "g5" else [],
                "allowed": list(GATE_DECISIONS),
            }
            d = normalize(interrupt(packet), GATE_DECISIONS)
            records.append(_record(gate, phase, d, lane_id))
            if d["decision"] == "takeover":
                hb = normalize(interrupt({"type": "handback", "gate": gate, "phase": phase, "lane": lane_id,
                                          "feature_id": state["feature_id"], "allowed": list(HANDBACK_DECISIONS)}),
                               HANDBACK_DECISIONS)
                records.append(_record(gate, phase, hb, lane_id))
                d = {**hb, "decision": "abort" if hb["decision"] == "abort" else "approve"}
            if d["decision"] != "approve":
                break  # one lane's revise or abort decides the gate

        if d["decision"] == "revise":
            return Command(goto=phase, update={
                "gate_decisions": records, "feedback": {phase: d["feedback"]}, "approvals": {gate: False},
                "events": [event(gate, "revise", d["feedback"], approver=d["approver"])],
            })
        if d["decision"] == "abort":
            if state.get("design"):  # an approved design is no longer in flight
                services.catalog.register({**_design_entry(state, state["design"]), "status": "abandoned"})
            return Command(goto=END, update={
                "gate_decisions": records, "status": "aborted",
                "events": [event(gate, "aborted", d["feedback"], approver=d["approver"])],
            })

        approvers = ", ".join(r["approver"] for r in records if r["decision"] in ("approve", "handback"))
        update: dict = {"gate_decisions": records, "approvals": {gate: True},
                        "events": [event(gate, "approved", d["feedback"], approver=approvers, lanes=approving)]}
        if gate == "g4" and review is not None:
            # Approved designs are visible to every other feature of the product while in flight.
            design = {k: review[k] for k in ("builds_on", "supersedes", "contracts", "record")}
            design["adrs"] = _hld_adrs(services.artifacts.load(ref))
            services.catalog.register({**_design_entry(state, design), "status": "in_design"})
            update["design"] = design
        if gate == "g6":
            release = dict(state.get("release", {}))
            # VX-3: production promotion is a human action, in each product's own runtime. In
            # production the approver promotes in the OpenChoreo portal and a watcher resumes this
            # thread; the stub records it here.
            lanes_by_repo = snap.lane_of_repo()
            for repo in release.get("components", []):
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
        return Command(goto="production_wait" if gate == "g6" else target, update=update)

    dest = ("production_wait",) if gate == "g6" else (tuple(target) if isinstance(target, list) else (target,))
    return gate_node, (*dest, phase, END)


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
        failed_runs = {r for r, v in runs.items() if v.get("state") == "failure"}
        for repo in cs.get("merge_order", []):  # dependency order: libraries, providers, consumers
            sha = ""
            for key, pr in sorted(state.get("pull_requests", {}).items()):
                if pr["repo"] != repo:
                    continue
                if pr.get("state") != "merged":  # idempotent: a retry skips what is merged
                    res = services.github.merge(repo=repo, number=pr["number"], actor=actor)
                    pr = {**pr, "state": res["state"], "merged_by": actor, "merge_sha": res.get("merge_sha")}
                    merged[key] = pr
                sha = pr.get("merge_sha") or sha
            lane = lanes.get(repo)
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
        targets = [{"repo": p["repo"], "number": p["number"], "sha": p["head_sha"], "base": p.get("base") or "main",
                    "url": p["url"]} for p in prs]
        res = interrupt(external(state, "ci", targets, quality_packet(services, state, "ci", targets)))
        if res["status"] == "success":
            return Command(goto=nxt, update={"delivery": {"ci": res}, **_quality_update(res),
                                             "events": [event("ci_wait", "passed", f"CI green on {len(prs)} PRs")]})
        if res["status"] == "security":
            fix = "Fix these security findings:\n" + "\n".join(
                f"- {f['severity']} {f['title']} in {f['repo']} ({f['url']})" for f in res["quality"]["blocking"])
            return security_review(services, state, "ci", res, ok=nxt, retry="build",
                                   retry_update={"feedback": {"build": fix}})
        # Red CI sends the build back to people, who can let agents fix it or vouch for it.
        findings = (list((res.get("quality") or {}).get("test_failures") or []) or
                    [f"{r['repo']}#{r['number']}: {r.get('message') or 'checks failed'}"
                     for r in res["results"] if r["state"] == "failure"])
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
        lanes = services.feature_snapshot(state).lane_of_repo()
        deps, failures = {}, []
        for repo, run in ((state.get("change_set") or {}).get("runs") or {}).items():
            t = oc_target(lanes.get(repo), repo)
            env = env_for(stage, t["pipeline"])
            if env is None:
                continue
            try:
                rec = services.openchoreo.promote(component=t["component"], env=env, actor=f"agent:{stage}",
                                                  actor_type="agent", project=t["project"],
                                                  namespace=t["namespace"], pipeline=t["pipeline"])
                deps[repo] = {"repo": repo, **t, "pipeline": list(t["pipeline"]), "env": env,
                              "release": rec.get("release", "")}
            except Exception as exc:
                failures.append({"repo": repo, "component": t["component"], "state": "failure",
                                 "message": f"{type(exc).__name__}: {exc}"})
        if failures:
            res = {"status": "failure", "wait": stage, "results": failures, "at": now(),
                   "reason": f"Couldn't deploy to {stage}: " + "; ".join(f["message"] for f in failures)}
            return _people_said(state, stage, res, ok=nxt, retry=f"deploy_{stage}", extra={"deployments": deps})
        events = [event(f"deploy_{stage}", "deployed", ", ".join(sorted(deps)))] if deps else []
        return Command(goto=f"{stage}_wait", update={"deployments": deps, "events": events})

    return deploy, (f"{stage}_wait", nxt, f"deploy_{stage}", END)


def make_env_wait(services, stage: str, ok: str, retry: str):
    def env_wait(state: FeatureState) -> Command:
        targets = [{"repo": d["repo"], "component": d["component"], "project": d.get("project"),
                    "namespace": d.get("namespace"), "env": d["env"]}
                   for d in (state.get("deployments") or {}).values()
                   if d.get("env") == env_for(stage, tuple(d.get("pipeline") or PIPELINE))]
        if not targets:
            return Command(goto=ok)
        runs = (state.get("change_set") or {}).get("runs") or {}
        qtargets = [{**t, "sha": runs.get(t["repo"], {}).get("sha", "")} for t in targets]
        res = interrupt(external(state, stage, targets, quality_packet(services, state, stage, qtargets)))
        report_to_github(services, state, stage, res)
        if res["status"] == "security":
            return security_review(services, state, stage, res, ok=ok, retry=f"{stage}_wait")
        if res["status"] == "success":
            return Command(goto=ok, update={"delivery": {stage: res}, **_quality_update(res),
                                            "events": [event(f"{stage}_wait", "passed",
                                                             f"{len(targets)} components ready in {stage}")]})
        return _people_said(state, stage, res, ok=ok, retry=retry, extra=_quality_update(res))

    return env_wait, (ok, retry, f"{stage}_wait", END)


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
    node, dest = make_env_wait(services, "production", ok="operate", retry="g6")
    g.add_node("production_wait", node, destinations=dest)
    g.add_node("finalize", finalize)

    g.add_edge(START, "intake")
    g.add_edge("intake", "discovery")
    for phase in PHASES:
        g.add_conditional_edges(phase, _route(phase), _route_targets(phase))
    g.add_edge("merge_train", "build_wait")
    g.add_edge("finalize", END)
    return g.compile(checkpointer=checkpointer, name="vrixel_feature")

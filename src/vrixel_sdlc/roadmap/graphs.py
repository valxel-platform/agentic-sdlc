"""The initiative and release workflows, each a LangGraph thread like a feature.

Initiative:  research -> draft -> check (loop) -> G0 (product owners, then each other lane involved)
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

from ..agents import untrusted
from ..research import DOC_SECTIONS
from ..research import summary as research_summary
from ..agents.context import build_layers, skills_dir_of
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
                          parent_texts={}, tail=tail, product_context=snap.product_context,
                          skills_dir=skills_dir_of(services))
    tags = CallTags(product_id=product_id, feature_id=thread_id, phase=phase, role=role,
                    data_class=snap.product.data_class, workspace=snap.product.workspace, prompt_version=f"{phase}@1")
    return services.runtime_for(role).run(AgentTask(role=role, phase=phase, tags=tags, layers=layers, skills=tuple(skills),
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
    research_depth: str  # quick | thorough | own (vrixel_sdlc.research)
    created_by: str
    research: Annotated[str, last]
    research_report: Annotated[dict, last]
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
        "research": state.get("research", ""),
        "goals": record["goals"], "initiatives": record["initiatives"] + [i for i in in_tool if i["id"] not in known],
        "features": services.features(pid) if callable(getattr(services, "features", None)) else [],
        "dependency_lanes": list(snap.product.depends_on), "feature_budget": snap.product.budget_usd,
        "max_budget_usd": services.settings.limits.max_initiative_budget_usd,
    }


def initiative_research(services, state: dict):
    """The research note the initiative cites (vrixel_sdlc.research)."""
    from .. import research as rs

    pid = state["product_id"]
    snap = services.snapshot(pid)
    role = "strategist"

    def layers_for(skills, tail):
        return build_layers(role=role, skills=tuple(skills), constitution=snap.constitution, product=snap.product,
                            parent_texts={}, tail=tail, product_context=snap.product_context,
                            skills_dir=skills_dir_of(services))

    tags = CallTags(product_id=pid, feature_id=state["initiative_id"], phase="research", role=role,
                    data_class=snap.product.data_class, workspace=snap.product.workspace, prompt_version="research@1")
    request = state.get("request", "") + (f"\nHow it will be measured: {state['measure_hint']}"
                                          if state.get("measure_hint") else "")
    return rs.run(services, role=role, subject=f"{state['title']} ({snap.product.name})", request=request,
                  depth=state.get("research_depth") or "quick", data_class=snap.product.data_class, tags=tags,
                  layers_for=layers_for)


def build_initiative_graph(services, checkpointer=None):
    def research(state: InitiativeState) -> dict:
        r = initiative_research(services, state)
        return {"research": r.text, "research_report": {k: v for k, v in r.as_dict().items() if k != "text"},
                "cost_usd": r.cost_usd, "status": "drafting",
                "events": [event("research", r.status, f"{r.depth}, {'searched the web' if r.web else 'no web search'}",
                                 findings=r.findings)]}

    def draft(state: InitiativeState) -> dict:
        ctx = initiative_context(services, state)
        findings = state.get("findings") or []
        tail = "\n".join([
            f"Draft initiative {state['initiative_id']} for product {state['product_id']}: {state['title']}.",
            "Request:\n" + untrusted.wrap("request", state.get("request", "")),
            "Write Markdown with these sections: ## Goal (an existing goal '- GOAL-...: title', or "
            "'- New goal: <title> (measure: <how measured>)'), ## Outcome, ## Success measure, ## Horizon (now, "
            "next or later), ## Budget (USD envelope for all its features), ## Lanes (the product and any lane it "
            "depends on that must change), ## Release model (launch: features ship dark and launch together; train: "
            "regulated or contractual shipments approved together), ## Candidate features ('- Title: what it "
            "delivers'; don't repeat features the product already has), ## Risks"
            + (f", {DOC_SECTIONS}." if state.get("research") else "."),
            *(["The research note to cite:\n" + untrusted.wrap("research", state["research"])]
              if state.get("research") else []),
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
                "title": state["title"], "findings": state.get("findings") or [],
                "preview": state["content"][:6000] + (
                    f"\n\n# The research behind it\n\n_{research_summary(state.get('research_report'))}_\n\n"
                    + state["research"].split("\n", 1)[-1][:14000] if state.get("research") else ""),
                "research": research_summary(state.get("research_report")),
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
        if state.get("research"):
            files[f"roadmap/research/{state['initiative_id']}.md"] = state["research"]
        if d.get("new_goal"):
            goals_path = "roadmap/goals.yaml"
            try:
                gov = services.settings.entry(state["product_id"]).governance
                src = services.governance.source
                current = src.read(gov.org, gov.repo, goals_path, src.resolve(gov.org, gov.repo, gov.ref), optional=True) or ""
            except Exception:
                current = ""
            gid = f"GOAL-{state['product_id']}-{state['initiative_id'].rsplit('-', 1)[-1]}"
            import yaml

            from ..outcomes import stub_from_measure

            # Measurable from the start, as inception's goals are: entered by hand until someone
            # wires a query (vrixel_sdlc.outcomes).
            goal = {"id": gid, "title": d["new_goal"]["title"], "measure": d["new_goal"]["measure"], "period": "",
                    "metric": stub_from_measure(d["new_goal"]["measure"])}
            entry = "".join("  " + ln + "\n" for ln in
                            yaml.safe_dump([goal], sort_keys=False, allow_unicode=True).splitlines())
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
    g.add_node("research", research)
    g.add_node("draft", draft)
    g.add_node("check", check, destinations=("draft", "gate"))
    g.add_node("gate", gate, destinations=("draft", "record", END))
    g.add_node("record", record)
    g.add_edge(START, "research")
    g.add_edge("research", "draft")
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
    rollout_plan: Annotated[dict, last]  # a gradual launch: the plan the release gate approved (ops/rollout.py)
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
        plan = None
        if state["model"] == "launch":
            from ..ops.rollout import TIERS_BY_RISK, percent_label, plan_for

            failed = []
            # A gradual launch when the platform asks for one: the release gate approves the plan for
            # the whole group, held to its riskiest feature's bake time.
            info = getattr(services, "feature_info", None)
            tiers = [(info(f) if callable(info) else {}).get("risk_tier") for f in (state.get("flags") or {})]
            tier = max((t for t in tiers if t in TIERS_BY_RISK), key=TIERS_BY_RISK.index, default=None)
            plan = plan_for(getattr(snap, "remediation", None), tier) if state.get("flags") else None
            first = plan["steps"][0] if plan else 100.0
            for fid, key in (state.get("flags") or {}).items():
                try:
                    if plan:
                        services.flags.set_percent(state["product_id"], key, first, actor=approvers)
                    else:
                        services.flags.set(state["product_id"], key, True, actor=approvers)
                    events.append(event("release", "flag_on", f"{key}{' for ' + percent_label(first) if plan else ''}",
                                        feature=fid))
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
                        targets=[{"kind": "flag", "name": k, "on": True, "feature_id": f,
                                  **({"percent": first} if plan else {})} for f, k in on],
                        release_id=state["release_id"], approved_by=[a for a in approvers.split(", ") if a],
                        approvals=[{"gate": "release", "lane": r.get("lane", ""), "approver": r.get("approver", ""),
                                    "at": r.get("at", "")} for r in records],
                        evidence={"gradual_release": {k: plan[k] for k in ("steps", "bake_minutes", "risk_tier")}} if plan else {}))
                    events.append(event("release", "change_recorded", f"{stored['id']}: launch"))
                except Exception as exc:
                    events.append(event("release", "change_not_recorded", f"{type(exc).__name__}: {exc}"))
            status = "shipped"
            note = " Flags are on: the features are live."
            if plan and on:
                status = "approved"  # shipped once every flag is on for everyone
                note = (f" Flags are on for {percent_label(first)} of users, then "
                        f"{', '.join(percent_label(s) for s in plan['steps'][1:])}, each after {plan['bake_minutes']} quiet minutes.")
            else:
                plan = None
            if held:
                note += f" {', '.join(held)} had no flag and now promote{'s' if len(held) == 1 else ''} to production."
            if failed:
                note += f" Turn on {', '.join(failed)} in Flipt: the pipeline couldn't."
        done = {**state, "approved_by": approvers}
        events += record(done, status, f"{state['release_id']} approved by {approvers}." + note)
        update = {"gate_decisions": records, "status": status, "approved_by": approvers, "events": events}
        if plan:
            from ..ops.rollout import _clock as rollout_clock

            update["rollout_plan"] = {**plan, "at": rollout_clock().isoformat(), "approved_by": approvers}
            return Command(goto="launch_rollout", update=update)
        return Command(goto=END, update=update)

    def launch_rollout(state: ReleaseState) -> Command:
        """Widen every flag of the launch together, per the plan its gate approved."""
        from ..graph.pipeline import ESCALATION_DECISIONS, normalize
        from ..ops import rollout as ro
        from ..ops.changes import new as new_change
        from ..ops.remediation import ROLLOUT_AGENT

        plan = dict(state.get("rollout_plan") or {})
        flags = dict(state.get("flags") or {})
        steps, step = list(plan.get("steps") or [100.0]), int(plan.get("step", 0))
        if not flags or step >= len(steps) - 1:
            return Command(goto=END, update={"status": "shipped"})
        info = getattr(services, "feature_info", None)
        components = sorted({c for f in flags for c in ((info(f) if callable(info) else {}).get("components") or [])})
        res = interrupt({"type": "external", "wait": "flag_step", "feature_id": state["release_id"],
                         "product_id": state["product_id"], "title": state["title"], "flags": sorted(flags.values()),
                         "flag": sorted(flags.values())[0], "percent": steps[step], "components": components,
                         "since": plan.get("at") or ro._clock().isoformat(), "bake_minutes": plan["bake_minutes"],
                         "compare": plan.get("compare") or {},
                         "label": f"Gradual launch: on for {ro.percent_label(steps[step])}, watching"})
        approved = [a for a in str(plan.get("approved_by") or state.get("approved_by") or "").split(", ") if a]

        def change(pct: float, by: str, who: list[str], n: int, why: str) -> list[dict]:
            if getattr(services, "changes", None) is None:
                return []
            try:
                stored = services.changes.record(new_change(
                    key=f"{state['release_id']}:launch:{n}:{pct:g}", product_id=state["product_id"], kind="flag",
                    type="standard", action="rollout_step",
                    summary=f"{state['release_id']} launch: on for {ro.percent_label(pct)}",
                    targets=[{"kind": "flag", "name": k, "on": True, "percent": pct, "feature_id": f} for f, k in flags.items()],
                    release_id=state["release_id"], requested_by=by, approved_by=who,
                    evidence={"plan": steps, "why": why}))
                return [event("launch_rollout", "change_recorded", f"{stored['id']}: {stored['summary']}")]
            except Exception as exc:
                return [event("launch_rollout", "change_not_recorded", f"{type(exc).__name__}: {exc}")]

        if res.get("status") == "success":
            nxt = steps[step + 1]
            try:
                for key in flags.values():
                    services.flags.set_percent(state["product_id"], key, nxt, actor=ROLLOUT_AGENT)
            except Exception as exc:
                res = {"status": "failure", "reason": f"couldn't widen the flags: {type(exc).__name__}: {exc}"}
            else:
                from ..ops.cohorts import describe_groups

                seen = res.get("cohorts") or {}
                plan |= {"step": step + 1, "at": ro._clock().isoformat(),
                         "history": list(plan.get("history") or []) + [{
                             "percent": steps[step], "until": res.get("at", ""),
                             "cohorts": "; ".join(describe_groups(k, seen[k]) for k in sorted(seen))
                             or "no comparison (no summary lines): time alone"}]}
                events = [event("launch_rollout", "widened", f"{', '.join(flags.values())} on for {ro.percent_label(nxt)}")]
                events += change(nxt, ROLLOUT_AGENT, approved, int(plan.get("attempt", 0)),
                                 f"quiet for {plan['bake_minutes']} minutes at {ro.percent_label(steps[step])}")
                done = step + 1 >= len(steps) - 1
                if done:
                    events += record({**state, "approved_by": state.get("approved_by", "")}, "shipped",
                                     f"{state['release_id']}: every flag is on for everyone after a gradual launch.")
                return Command(goto=END if done else "launch_rollout",
                               update={"rollout_plan": plan, "events": events, **({"status": "shipped"} if done else {})})
        pre: list[dict] = []
        if res.get("regression"):
            pre, said = ro.on_regression(services, product_id=state["product_id"], thread_id=state["release_id"],
                                         title=f"{state['release_id']} {state['title']}", flags=flags, res=res,
                                         policy=getattr(services.snapshot(state["product_id"]), "remediation", None),
                                         release_id=state["release_id"])
            res = {**res, "reason": f"{res.get('reason', '')}. {said}"}
        d = normalize(interrupt({
            "type": "escalation", "phase": "delivery", "wait": "launch_rollout", "feature_id": state["release_id"],
            "product_id": state["product_id"], "title": state["title"],
            "reason": f"The gradual launch stopped at {ro.percent_label(steps[step])}: {res.get('reason', '')}. "
                      "Retry starts every flag again from the first step; resolved leaves them as they are; "
                      "abort cancels the launch and turns its flags off.",
            "findings": [], "results": [], "allowed": list(ESCALATION_DECISIONS)}), ESCALATION_DECISIONS)
        rec = {"gate": "escalation:delivery:launch_rollout", "lane": state["product_id"], "at": now(), **d}
        if d["decision"] == "retry":
            for key in flags.values():
                services.flags.set_percent(state["product_id"], key, steps[0], actor=d["approver"])
            attempt = int(plan.get("attempt", 0)) + 1
            plan |= {"step": 0, "at": ro._clock().isoformat(), "attempt": attempt}
            return Command(goto="launch_rollout", update={
                "rollout_plan": plan, "gate_decisions": [rec],
                "events": pre + [event("launch_rollout", "restarted", res.get("reason", ""), approver=d["approver"])]
                + change(steps[0], d["approver"], [d["approver"]], attempt, f"restarted after: {res.get('reason', '')}")})
        final = "cancelled" if d["decision"] == "abort" else "shipped"
        extra = []
        if final == "cancelled":  # a cancelled launch is off for everyone
            for key in flags.values():
                try:
                    services.flags.set(state["product_id"], key, False, actor=d["approver"])
                except Exception as exc:
                    extra.append(event("launch_rollout", "flag_failed", f"Couldn't turn off {key}: {exc}"))
            if getattr(services, "changes", None) is not None:
                try:
                    stored = services.changes.record(new_change(
                        key=f"{state['release_id']}:launch:cancelled", product_id=state["product_id"], kind="flag",
                        type="standard", action="flag_off", summary=f"{state['release_id']} launch cancelled: flags off",
                        targets=[{"kind": "flag", "name": k, "on": False, "feature_id": f} for f, k in flags.items()],
                        release_id=state["release_id"], requested_by=d["approver"], approved_by=[d["approver"]],
                        evidence={"why": res.get("reason", "")}))
                    extra.append(event("launch_rollout", "change_recorded", f"{stored['id']}: {stored['summary']}"))
                except Exception as exc:
                    extra.append(event("launch_rollout", "change_not_recorded", f"{type(exc).__name__}: {exc}"))
        return Command(goto=END, update={
            "rollout_plan": {**plan, "stopped": True}, "gate_decisions": [rec], "status": final,
            "events": pre + extra + [event("launch_rollout", "stopped" if d["decision"] == "abort" else "left_as_is",
                             d.get("feedback", "") or res.get("reason", ""), approver=d["approver"])]
            + record(state, final, f"{state['release_id']}: the gradual launch stopped at {ro.percent_label(steps[step])} "
                                   f"({res.get('reason', '')}); {d['approver']} {'cancelled it' if final == 'cancelled' else 'left it as it is'}.")})

    g = StateGraph(ReleaseState)
    g.add_node("plan", plan)
    g.add_node("wait_ready", wait_ready)
    g.add_node("notes", notes)
    g.add_node("gate", gate, destinations=("launch_rollout", END))
    g.add_node("launch_rollout", launch_rollout, destinations=("launch_rollout", END))
    g.add_edge(START, "plan")
    g.add_edge("plan", "wait_ready")
    g.add_conditional_edges("wait_ready", route_after_wait, ["notes", END])
    g.add_edge("notes", "gate")
    return g.compile(checkpointer=checkpointer, name="vrixel_release")

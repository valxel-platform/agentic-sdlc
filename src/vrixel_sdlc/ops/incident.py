"""Incidents: a production problem from first signal to postmortem.

    triage (agent: what's happening, likely cause, proposed actions)
      -> decide (on-call: approve some or all actions, ask for another look, or close)
      -> act (flag off, roll back, start a hotfix, start an intent change)
      -> monitor (until its alerts and issues resolve; still firing after the window -> decide again)
      -> postmortem (agent, blameless) -> close (on-call approves; a governance PR records it)

Every production action is a person's decision (VX-1, VX-3): agents investigate and propose.
On-call is paged through PagerDuty when the incident opens (ops/paging.py); their first decision
acknowledges the page, and reaching the postmortem (or a merge) resolves it.
"""

from __future__ import annotations

import operator
import re
from datetime import datetime, timedelta, timezone
from typing import Annotated, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from ..roadmap import governance_pr
from ..roadmap.graphs import _decide, event, lane_approvers, now, run_agent
from ..state import last, merge_dict
from .signals import resolves, still_firing

ACTIONS = ("flag_off", "rollback", "hotfix", "intent_change", "monitor")
DECISIONS = ("approve", "revise", "resolve", "merge")
CLOSE_DECISIONS = ("approve", "revise")
RECOVERY_MINUTES = {"sev1": 30, "sev2": 60, "sev3": 240, "sev4": 1440}
_ACTION = re.compile(r"^-\s*(flag_off|rollback|hotfix|intent_change|monitor)\s*:\s*(.*)$", re.M)


def oncall_approvers(lane) -> list[str]:
    return list(lane.approvers.get("oncall") or lane_approvers(lane, "release"))


class IncidentState(TypedDict, total=False):
    incident_id: str
    product_id: str
    title: str
    severity: Annotated[str, last]
    component: str  # repo of the affected component, when known
    opened_at: str
    status: Annotated[str, last]  # triaging | awaiting_decision | mitigating | monitoring | postmortem | closed
    triage: Annotated[str, last]
    actions: Annotated[list[dict], last]  # proposed
    feedback: Annotated[str, last]
    taken: Annotated[list[dict], operator.add]  # actions carried out, with results
    links: Annotated[dict, merge_dict]  # feature or initiative ids started from this incident
    postmortem: Annotated[str, last]
    record_pr: Annotated[dict, last]
    gate_decisions: Annotated[list[dict], operator.add]
    events: Annotated[list[dict], operator.add]
    cost_usd: Annotated[float, operator.add]
    tokens_used: Annotated[int, operator.add]


def section(text: str, heading: str) -> str:
    m = re.search(rf"^## {re.escape(heading)}\s*$(.*?)(?=^## |\Z)", text, re.M | re.S)
    return m.group(1).strip() if m else ""


def parse_actions(text: str) -> list[dict]:
    """'## Proposed actions' bullets: '- flag_off: EMR-FEAT-3', '- rollback: vrixel-emr/emr-api',
    '- hotfix: vrixel-emr/emr-api: what to change', '- intent_change: feature|initiative: the new intent',
    '- monitor: why no action is needed'."""
    out = []
    for n, m in enumerate(_ACTION.finditer(section(text, "Proposed actions"))):
        kind, rest = m.group(1), m.group(2).strip()
        a: dict = {"id": f"A{n + 1}", "type": kind, "text": rest}
        if kind == "flag_off":
            a["feature_id"] = rest.split()[0] if rest else ""
        elif kind == "rollback":
            a["component"] = rest.split()[0] if rest else ""
        elif kind == "hotfix":
            repo, _, what = rest.partition(":")
            a |= {"repo": repo.strip(), "change": what.strip()}
        elif kind == "intent_change":
            scope, _, what = rest.partition(":")
            scope = scope.strip().lower()
            a |= {"scope": scope if scope in ("feature", "initiative") else "feature",
                  "intent": what.strip() if scope in ("feature", "initiative") else rest}
        out.append(a)
    return out


def stub_triage(ctx: dict) -> str:
    """Deterministic triage for stub mode, from the signals and the suspects alone."""
    sigs, suspects = ctx["signals"], ctx["suspects"]
    first = sigs[0] if sigs else {"title": ctx["title"], "detail": "", "source": "ticket"}
    tickets_only = all(s["source"] == "ticket" for s in sigs)
    text = " ".join(f"{s['title']} {s.get('detail', '')}" for s in sigs).lower()
    wants = re.search(r"\b(should|would like|want|expected|can't|cannot|need to|confusing|missing)\b", text)
    lines = [f"# Incident triage: {ctx['title']}", "", "## Summary",
             f"{len(sigs)} signal(s), first: {first['title']}.", "", "## Impact",
             f"Severity {ctx['severity']}" + (f" on {ctx['component']}" if ctx.get("component") else "") + ".",
             "", "## Likely cause"]
    actions = []
    cost = [s for s in sigs if s.get("category") == "cost"]
    if cost and len(cost) == len(sigs):
        lines.append(f"Spend is above what was planned: {cost[-1].get('detail') or cost[-1]['title']}")
        feature = next((s["labels"].get("feature_id") for s in cost if (s.get("labels") or {}).get("feature_id")), "")
        actions.append(f"- intent_change: feature: bring {feature or 'the spend'} back within its approved cost "
                       f"(right-size or cut usage): {cost[-1].get('detail', '')}")
        actions.append("- monitor: if the spend is expected, accept it and record the new figure")
    elif tickets_only and wants:
        lines.append("The product works as built, but not as users need: the intent should change.")
        actions.append(f"- intent_change: feature: {first['title']}")
    elif suspects:
        s = suspects[0]
        lines.append(f"{s['feature_id']} ({s['title']}) changed {ctx.get('component') or 'this component'} "
                     f"most recently and is the likely cause.")
        if s.get("flag") and s.get("flag_on"):
            actions.append(f"- flag_off: {s['feature_id']}")
        elif ctx.get("component"):
            actions.append(f"- rollback: {ctx['component']}")
        if ctx.get("component"):
            actions.append(f"- hotfix: {ctx['component']}: fix the failure behind \"{first['title']}\"")
    elif ctx.get("component"):
        lines.append("No recent change explains it; the last release of the component is the first suspect.")
        actions.append(f"- rollback: {ctx['component']}")
    else:
        lines.append("Not enough to go on yet.")
        actions.append("- monitor: collect more signals before acting")
    if ctx.get("feedback"):
        lines += ["", "## Changes from review", f"- {ctx['feedback']}"]
    lines += ["", "## Proposed actions", *actions]
    return "\n".join(lines) + "\n"


def stub_postmortem(ctx: dict) -> str:
    timeline = [f"- {e['at'][:16]} {e['node']}: {e['kind']} {e.get('detail', '')}".rstrip() for e in ctx["events"]]
    taken = [f"- {t['type']}: {t.get('result', '')}" for t in ctx["taken"]] or ["- None"]
    follow = [f"- {k}: {v}" for k, v in ctx["links"].items()] or ["- None"]
    return "\n".join([f"# Postmortem: {ctx['title']}", "", f"Severity {ctx['severity']}. Blameless.", "",
                      "## Timeline", *timeline, "", "## Cause", section(ctx["triage"], "Likely cause") or "Unknown", "",
                      "## What we did", *taken, "", "## Follow-ups", *follow, ""])


def build_incident_graph(services, checkpointer=None):
    def context(state: IncidentState) -> dict:
        signals = services.incident_signals(state["incident_id"])
        # The furthest-downstream failing component is the root cause candidate (ops/correlate.py).
        from .correlate import root

        try:
            component, why = root(services.snapshot(state["product_id"]), signals, state.get("component", ""))
        except Exception:
            component, why = state.get("component", ""), ""
        state = {**state, "component": component or state.get("component", ""), "root_why": why}
        suspects = services.suspects(state["product_id"], state.get("component", ""))
        observed = {}
        if state.get("component"):
            try:
                snap = services.snapshot(state["product_id"])
                rt = snap.product.runtime or {}
                observed = services.openchoreo.observe(
                    component=(rt.get("components") or {}).get(state["component"], state["component"]),
                    env="production", project=rt.get("openchoreo_project"), namespace=rt.get("namespace"))
            except Exception as exc:
                observed = {"available": False, "error": f"{type(exc).__name__}: {exc}"}
        return {**state, "signals": signals, "suspects": suspects, "observed": observed,
                "feedback": state.get("feedback", "")}

    def triage(state: IncidentState) -> dict:
        ctx = context(state)
        tail = "\n".join([
            f"Triage incident {state['incident_id']} in {state['product_id']} (severity {state['severity']}).",
            "Signals:", *[f"- [{s['source']} {s['status']} {s['severity']}] {s['title']}: {s.get('detail', '')[:300]}"
                          for s in ctx["signals"][-20:]],
            *([f"Root cause candidate: {ctx['component']} ({ctx['root_why']})."] if ctx.get("root_why") else []),
            *[f"Joined because {s['labels']['joined_by']}: {s['title']}"
              for s in ctx["signals"] if (s.get("labels") or {}).get("joined_by")][-5:],
            "Recent changes to the component (most recent first):",
            *[f"- {x['feature_id']} {x['title']}: released {x.get('released_at', '')}, flag "
              f"{'on' if x.get('flag_on') else 'off' if x.get('flag') else 'none'}" for x in ctx["suspects"][:10]],
            "Recent logs (may be partial):", *[f"  {line}" for line in (ctx["observed"].get("logs") or [])[-40:]],
            f"Metrics: {str(ctx['observed'].get('metrics') or {})[:1500]}",
            *([f"On-call asked for another look: {state['feedback']}"] if state.get("feedback") else []),
            "Write Markdown: ## Summary, ## Impact, ## Likely cause, ## Proposed actions with one bullet per action:"
            " '- flag_off: <feature id>', '- rollback: <repo>', '- hotfix: <repo>: <what to change>',"
            " '- intent_change: feature|initiative: <the new intent>' (when it works as built but not as users need),"
            " '- monitor: <why no action>'. Prefer turning a recent feature's flag off over rolling back."
            " For cost signals (spend above a budget or above what G4 approved): propose an intent_change to"
            " right-size or cut usage, or monitor when the spend is expected; turn a flag off only when the"
            " spend must stop now, and never roll back for cost alone."
            " Never include patient data or identifiers.",
        ])
        res = run_agent(services, role="ops", product_id=state["product_id"], thread_id=state["incident_id"],
                        phase="incident", skills=("engineering:incident-response", "engineering:debug"),
                        tail=tail, stub_text=stub_triage(ctx))
        return {"triage": res.text, "actions": parse_actions(res.text), "status": "awaiting_decision",
                "cost_usd": res.cost_usd, "tokens_used": res.tokens,
                "events": [event("triage", "triaged", f"{len(parse_actions(res.text))} actions proposed")]}

    def decide(state: IncidentState) -> Command:
        snap = services.snapshot(state["product_id"])
        signals = services.incident_signals(state["incident_id"])
        from .correlate import root

        try:
            root_component, root_why = root(snap, signals, state.get("component", ""))
        except Exception:
            root_component, root_why = state.get("component", ""), ""
        packet = {
            "type": "gate", "gate": "incident", "phase": "incident", "kind": "incident", "lane": state["product_id"],
            "feature_id": state["incident_id"], "product_id": state["product_id"], "title": state["title"],
            "severity": state["severity"], "preview": state["triage"][:4000], "actions": state.get("actions") or [],
            "signals": signals[-20:], "firing": len(still_firing(signals)),
            # Where the failure starts, among the components that showed it (ops/correlate.py).
            "root": {"component": root_component, "why": root_why},
            "suspects": services.suspects(state["product_id"], state.get("component", ""))[:5],
            "approvers": oncall_approvers(snap.product), "allowed": list(DECISIONS),
        }
        d = _decide(interrupt(packet), DECISIONS)
        rec = {"gate": "incident", "lane": state["product_id"], "at": now(), **d}
        pager = getattr(services, "pager", None)
        if pager is not None and not str(d.get("approver") or "").startswith("agent:"):
            pager.acknowledge(state["incident_id"], state["product_id"], d["approver"])  # someone has it
        if d["decision"] == "revise":
            return Command(goto="triage", update={"gate_decisions": [rec], "feedback": d.get("feedback", ""),
                                                  "status": "triaging", "events": [event("incident", "revise", d.get("feedback", ""),
                                                                                         approver=d["approver"])]})
        if d["decision"] == "merge":
            into = str(d.get("into") or "")
            if not into or into == state["incident_id"]:
                from ..graph import InvalidDecision

                raise InvalidDecision("Merging needs the incident to merge into")
            services.close_incident(state["incident_id"])
            if pager is not None:
                pager.resolve(state["incident_id"], state["product_id"], f"merged into {into}")
            return Command(goto=END, update={"gate_decisions": [rec], "status": "merged",
                                             "links": {into: "merged into"},
                                             "events": [event("incident", "merged", f"into {into}: {d.get('feedback', '')}",
                                                              approver=d["approver"])]})
        if d["decision"] == "resolve":
            return Command(goto="postmortem", update={"gate_decisions": [rec], "status": "postmortem",
                                                      "events": [event("incident", "resolved", d.get("feedback", ""),
                                                                       approver=d["approver"])]})
        chosen = d.get("action_ids")
        approved = [a for a in state.get("actions") or [] if chosen is None or a["id"] in chosen]
        return Command(goto="act", update={"gate_decisions": [rec], "actions": approved, "status": "mitigating",
                                           "events": [event("incident", "approved", ", ".join(a["type"] for a in approved),
                                                            approver=d["approver"])]})

    def act(state: IncidentState) -> dict:
        approver = (state.get("gate_decisions") or [{}])[-1].get("approver", "")
        snap = services.snapshot(state["product_id"])
        rt = snap.product.runtime or {}
        taken, links, events = [], {}, []
        for a in state.get("actions") or []:
            try:
                if a["type"] == "flag_off":
                    key = services.flag_of(a["feature_id"])
                    if not key:
                        raise ValueError(f"{a['feature_id']} has no feature flag")
                    services.flags.set(state["product_id"], key, False, actor=approver)
                    result = f"turned off {key}"
                elif a["type"] == "rollback":
                    comp = a["component"] or state.get("component", "")
                    rec = services.openchoreo.rollback(
                        component=(rt.get("components") or {}).get(comp, comp), env="production", actor=approver,
                        actor_type="human", project=rt.get("openchoreo_project"), namespace=rt.get("namespace"))
                    result = f"rolled {comp} back from {rec.get('from')} to {rec.get('to')}"
                elif a["type"] == "hotfix":
                    fid = services.start_hotfix(state, repo=a["repo"] or state.get("component", ""),
                                                change=a["change"] or state["title"], approver=approver)
                    links[fid] = "hotfix"
                    result = f"started hotfix {fid}"
                elif a["type"] == "intent_change":
                    tid = services.start_intent_change(state, scope=a["scope"], intent=a["intent"], approver=approver)
                    links[tid] = f"intent change ({a['scope']})"
                    result = f"started {tid} for G{'0' if a['scope'] == 'initiative' else '1'}"
                else:
                    result = a.get("text") or "monitoring"
                taken.append({**a, "ok": True, "result": result, "by": approver, "at": now()})
                events.append(event("act", a["type"], result, approver=approver))
            except Exception as exc:
                msg = f"{type(exc).__name__}: {exc}"
                taken.append({**a, "ok": False, "result": msg, "by": approver, "at": now()})
                events.append(event("act", f"{a['type']}_failed", msg, approver=approver))
        return {"taken": taken, "links": links, "events": events, "status": "monitoring"}

    def monitor(state: IncidentState) -> Command:
        machine = [s for s in services.incident_signals(state["incident_id"]) if resolves(s)]
        if not machine:  # a reported problem or a cost finding: nothing to watch recover
            return Command(goto="postmortem", update={"status": "postmortem"})
        res = interrupt({"type": "external", "wait": "incident_recovery", "feature_id": state["incident_id"],
                         "product_id": state["product_id"], "label": "Waiting for its alerts and issues to resolve",
                         "since": now(), "minutes": RECOVERY_MINUTES.get(state["severity"], 240)})
        if res.get("status") == "success":
            return Command(goto="postmortem", update={"status": "postmortem",
                                                      "events": [event("monitor", "recovered")]})
        return Command(goto="decide", update={"status": "awaiting_decision", "feedback": res.get("reason", ""),
                                              "events": [event("monitor", "still_firing", res.get("reason", ""))]})

    def postmortem(state: IncidentState) -> dict:
        # Mitigated, recovered or resolved by on-call: the page is over.
        pager = getattr(services, "pager", None)
        if pager is not None:
            pager.resolve(state["incident_id"], state["product_id"], "mitigated")
        ctx = {**state, "events": state.get("events") or [], "taken": state.get("taken") or [],
               "links": state.get("links") or {}}
        res = run_agent(services, role="ops", product_id=state["product_id"], thread_id=state["incident_id"],
                        phase="postmortem", skills=("engineering:incident-response",),
                        tail=(f"Write a blameless postmortem for {state['incident_id']}: timeline, cause, what was "
                              "done, follow-ups. No patient data or identifiers.\n\nTriage:\n" + state.get("triage", "")
                              + (f"\n\nOn-call notes: {state['feedback']}" if state.get("feedback") else "")),
                        stub_text=stub_postmortem(ctx))
        return {"postmortem": res.text, "cost_usd": res.cost_usd, "tokens_used": res.tokens}

    def close(state: IncidentState) -> Command:
        snap = services.snapshot(state["product_id"])
        d = _decide(interrupt({
            "type": "gate", "gate": "postmortem", "phase": "incident", "kind": "incident",
            "lane": state["product_id"], "feature_id": state["incident_id"], "product_id": state["product_id"],
            "title": state["title"], "severity": state["severity"], "preview": state.get("postmortem", "")[:4000],
            "approvers": oncall_approvers(snap.product), "allowed": list(CLOSE_DECISIONS)}), CLOSE_DECISIONS)
        rec = {"gate": "postmortem", "lane": state["product_id"], "at": now(), **d}
        if d["decision"] == "revise":
            return Command(goto="postmortem", update={"gate_decisions": [rec], "feedback": d.get("feedback", "")})
        events = [event("close", "closed", approver=d["approver"])]
        try:
            pr = governance_pr(services, state["product_id"], branch=f"vrixel/{state['incident_id']}",
                               files={f"incidents/{state['incident_id']}.md": state.get("postmortem", "")},
                               title=f"{state['incident_id']}: postmortem", labels=["vrixel", "incident"],
                               body=f"Postmortem for {state['incident_id']} ({state['severity']}), approved by {d['approver']}.")
            events.append(event("close", "postmortem_pr", pr["url"]))
        except Exception as exc:
            pr = {"error": f"{type(exc).__name__}: {exc}"}
            events.append(event("close", "postmortem_pr_failed", pr["error"]))
        services.close_incident(state["incident_id"])
        return Command(goto=END, update={"gate_decisions": [rec], "status": "closed", "record_pr": pr, "events": events})

    g = StateGraph(IncidentState)
    g.add_node("triage", triage)
    g.add_node("decide", decide, destinations=("triage", "act", "postmortem"))
    g.add_node("act", act)
    g.add_node("monitor", monitor, destinations=("postmortem", "decide"))
    g.add_node("postmortem", postmortem)
    g.add_node("close", close, destinations=("postmortem", END))
    g.add_edge(START, "triage")
    g.add_edge("triage", "decide")
    g.add_edge("act", "monitor")
    g.add_edge("postmortem", "close")
    return g.compile(checkpointer=checkpointer, name="vrixel_incident")


def recovery(signals: list[dict], since: str, minutes: int) -> dict | None:
    """The monitor wait's result: success once nothing fires; failure if still firing after the window."""
    firing = still_firing(signals)
    if not firing:
        return {"status": "success"}
    started = datetime.fromisoformat(since)
    if datetime.now(timezone.utc) - started > timedelta(minutes=minutes):
        return {"status": "failure", "reason": f"still firing after {minutes} minutes: "
                + "; ".join(s["title"] for s in firing[:5])}
    return None

"""Incidents: a production problem from first signal to postmortem, worked by the tool.

    triage   (agent: what's happening, likely cause, proposed actions)
      -> auto      (the tool mitigates by itself when the platform's remediation policy allows it:
                    turn off a recent feature's flag, roll back a recent release; then starts the fix)
      -> escalate  (it can't: page on-call urgently through PagerDuty, unless it's a cost finding or
                    a request to change what was built, which waits for working hours)
      -> decide    (on-call: approve some or all actions, ask for another look, resolve, merge)
      -> act       (flag off, roll back, start a hotfix, start an intent change)
      -> monitor   (until it's quiet: alerts resolved, no new alerts or error reports, errors in the
                    logs back below the alert rule; still failing -> escalate, or back to on-call)
      -> postmortem (agent, blameless) -> close (on-call approves; a governance PR records it)

The tool's own production actions are the platform's exception to VX-3, limited to undoing a
recent change (ops/remediation.py). Everything else, and anything after a person has decided, is a
person's decision. SEV1s also send on-call a low-urgency heads-up as they open (engine._page).
"""

from __future__ import annotations

import operator
import re
from datetime import datetime, timedelta, timezone
from typing import Annotated, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from ..agents import untrusted
from ..roadmap import governance_pr
from ..roadmap.graphs import _decide, event, lane_approvers, now, run_agent
from ..state import last, merge_dict
from . import remediation
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
    auto_attempts: Annotated[int, operator.add]  # mitigations the tool took by itself
    analysis: Annotated[dict, last]  # OpenChoreo's root-cause (or FinOps) analysis: {kind, report_id, status, report}
    escalated: Annotated[bool, last]  # on-call was paged urgently (or told it waits for them)


def section(text: str, heading: str) -> str:
    m = re.search(rf"^## {re.escape(heading)}\s*$(.*?)(?=^## |\Z)", text, re.M | re.S)
    return m.group(1).strip() if m else ""


def parse_actions(text: str) -> list[dict]:
    """'## Proposed actions' bullets: '- flag_off: CLN-FEAT-3', '- rollback: acme-clinic/cln-api',
    '- hotfix: acme-clinic/cln-api: what to change', '- intent_change: feature|initiative: the new intent',
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
        rec = ((remediation.report_body(ctx.get("analysis")).get("overprovisioning") or {}).get("recommendation") or {}) \
            if (ctx.get("analysis") or {}).get("kind") == "cost" else {}
        sizing = (f" OpenChoreo's FinOps agent recommends cpu {rec.get('cpu_request')}/{rec.get('cpu_limit')}, memory "
                  f"{rec.get('memory_request')}/{rec.get('memory_limit')}, saving about {rec.get('estimated_savings')} "
                  f"{rec.get('currency', 'USD')}.") if rec else ""
        actions.append(f"- intent_change: feature: bring {feature or 'the spend'} back within its approved cost "
                       f"(right-size or cut usage): {cost[-1].get('detail', '')}{sizing}")
        actions.append("- monitor: if the spend is expected, accept it and record the new figure")
    elif tickets_only and wants:
        lines.append("The product works as built, but not as users need: the intent should change.")
        actions.append(f"- intent_change: feature: {first['title']}")
    elif suspects:
        s = suspects[0]
        lines.append(f"{s['feature_id']} ({s['title']}) changed {ctx.get('component') or 'this component'} "
                     f"most recently and is the likely cause.")
        may = ctx.get("may") or []  # what the tool may do by itself: prefer it, so it can act now
        if s.get("flag") and s.get("flag_on") and not (may and "flag_off" not in may and ctx.get("component")):
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
        try:
            may = sorted((services.snapshot(state["product_id"]).remediation or {}).get("autonomous") or {})
        except Exception:
            may = []
        return {**state, "signals": signals, "suspects": suspects, "observed": observed,
                "feedback": state.get("feedback", ""), "may": may, "analysis": state.get("analysis") or {}}

    def target(state: IncidentState) -> dict:
        """Where the failure is, in OpenChoreo's terms."""
        snap = services.snapshot(state["product_id"])
        rt = snap.product.runtime or {}
        comp = state.get("component", "")
        name = (rt.get("components") or {}).get(comp, comp).rsplit("/", 1)[-1]
        pipeline = list(rt.get("pipeline") or ["production"])
        return {"namespace": rt.get("namespace") or "default", "project": rt.get("openchoreo_project") or "",
                "component": name, "environment": pipeline[-1]}

    def analyze(state: IncidentState) -> dict:
        """Ask OpenChoreo's agent to investigate: root cause for a failure, right-sizing for a budget
        alert. Only where the product's data may go to the agent's AI provider."""
        if state.get("analysis"):
            return {}
        signals = services.incident_signals(state["incident_id"])
        cost = [s for s in signals if s.get("category") == "cost"]
        kind = "cost" if cost and len(cost) == len(signals) else "rca"
        agents = getattr(services, "agents", None)
        a = (policy(state).get("analysis") or {}).get(kind)
        base = {"kind": kind, "requested_at": now()}
        if kind == "rca" and not state.get("component"):
            return {"analysis": {**base, "status": "skipped", "why": "no component to investigate"}}
        if a is None or agents is None or not (agents.rca_available() if kind == "rca" else agents.cost_available()):
            return {"analysis": {**base, "status": "skipped", "why": "OpenChoreo's agent isn't set up for the tool"}}
        if not a.get("allowed"):
            return {"analysis": {**base, "status": "skipped", "why": a.get("why_not", "")},
                    "events": [event("analyze", "not_analyzed", a.get("why_not", ""))]}
        try:
            where = target(state)
            if kind == "rca":
                first = signals[0] if signals else {}
                rid = agents.request_rca(alert_id=state["incident_id"], alert_name=state["title"],
                                         description=str(first.get("detail") or "")[:500],
                                         value=(first.get("labels") or {}).get("alertValue") or 1,
                                         at=str(first.get("at") or now()), **where)
            else:
                s = next((x for x in cost if (x.get("labels") or {}).get("alertThreshold")), None)
                if s is None or not s.get("component"):
                    return {"analysis": {**base, "status": "skipped",
                                         "why": "a budget finding without a component's figures"}}
                rid = agents.request_cost(budget=float(s["labels"]["alertThreshold"]),
                                          actual=float(s["labels"].get("alertValue") or 0),
                                          period=str(s["labels"].get("window") or "30d"), at=str(s.get("at") or now()),
                                          **{**where, "component": s["component"].rsplit("/", 1)[-1]})
        except Exception as exc:
            return {"analysis": {**base, "status": "skipped", "why": f"{type(exc).__name__}: {exc}"},
                    "events": [event("analyze", "not_analyzed", f"{type(exc).__name__}: {exc}")]}
        wait = (a.get("wait_minutes") or remediation.ANALYSIS_WAIT).get(state["severity"], 10)
        return {"analysis": {**base, "status": "pending", "report_id": rid, "wait_minutes": wait},
                "events": [event("analyze", "requested", f"OpenChoreo's {'SRE' if kind == 'rca' else 'FinOps'} agent: {rid}")]}

    def analysis_wait(state: IncidentState) -> Command:
        a = state.get("analysis") or {}
        if a.get("status") != "pending":
            return Command(goto="triage")
        res = interrupt({"type": "external", "wait": "analysis_report", "feature_id": state["incident_id"],
                         "product_id": state["product_id"], "kind": a["kind"], "report_id": a["report_id"],
                         "label": "Waiting for OpenChoreo's analysis", "since": a.get("requested_at") or now(),
                         "minutes": a.get("wait_minutes", 10)})
        if (done := merged(state)) is not None:
            return done
        found = {**a, "status": res.get("report_status") or ("completed" if res.get("status") == "success" else "failed"),
                 "report": res.get("report"), "why": res.get("reason", "")}
        summary = str(remediation.report_body(found).get("summary") or found["why"] or found["status"])
        return Command(goto="triage", update={"analysis": found,
                                              "events": [event("analyze", found["status"], summary[:300])]})

    def analysis_lines(a: dict) -> list[str]:
        body = remediation.report_body(a)
        if not body:
            return [f"OpenChoreo's analysis: {a.get('status', 'none')}{' (' + a['why'] + ')' if a.get('why') else ''}."]
        if a.get("kind") == "cost":
            o = body.get("overprovisioning") or {}
            r = o.get("recommendation") or {}
            out = [f"OpenChoreo's FinOps agent: {body.get('summary', '')} {o.get('analysis', '')}".strip()]
            if r:
                out.append(f"It recommends cpu {r.get('cpu_request')}/{r.get('cpu_limit')}, memory "
                           f"{r.get('memory_request')}/{r.get('memory_limit')} (saves about "
                           f"{r.get('estimated_savings')} {r.get('currency', 'USD')}): {r.get('rationale', '')}")
            return out
        res = body.get("result") or {}
        out = [f"OpenChoreo's root-cause analysis: {body.get('summary', '')}"]
        for rc in (res.get("root_causes") or [])[:3]:
            out.append(f"- ({rc.get('confidence')}) {rc.get('summary')}: {rc.get('analysis', '')}")
        for x in (res.get("excluded_causes") or [])[:3]:
            out.append(f"- ruled out: {x.get('description')}")
        for x in ((res.get("recommendations") or {}).get("recommended_actions") or [])[:3]:
            out.append(f"- it recommends: {x.get('description')}")
        return out

    def triage(state: IncidentState) -> dict:
        ctx = context(state)
        tail = "\n".join([
            f"Triage incident {state['incident_id']} in {state['product_id']} (severity {state['severity']}).",
            "Signals (from monitoring, error trackers and reports):",
            untrusted.lines("signals", [f"- [{s['source']} {s['status']} {s['severity']}] {s['title']}: "
                                        f"{s.get('detail', '')[:300]}" for s in ctx["signals"][-20:]]),
            *([f"Root cause candidate: {ctx['component']} ({ctx['root_why']})."] if ctx.get("root_why") else []),
            *([untrusted.lines("openchoreo-analysis", analysis_lines(state["analysis"]))] if state.get("analysis") else []),
            untrusted.lines("joined-signals", [f"Joined because {s['labels']['joined_by']}: {s['title']}"
                                               for s in ctx["signals"] if (s.get("labels") or {}).get("joined_by")][-5:]),
            "Recent changes to the component (most recent first):",
            *[f"- {x['feature_id']} {x['title']}: released {x.get('released_at', '')}, flag "
              f"{'on' if x.get('flag_on') else 'off' if x.get('flag') else 'none'}" for x in ctx["suspects"][:10]],
            "Recent logs (may be partial):", untrusted.lines("logs", (ctx["observed"].get("logs") or [])[-40:]),
            "Metrics:", untrusted.wrap("metrics", str(ctx["observed"].get("metrics") or {})[:1500]),
            *([f"Look again, because: {state['feedback']}"] if state.get("feedback") else []),
            "Write Markdown: ## Summary, ## Impact, ## Likely cause, ## Proposed actions with one bullet per action:"
            " '- flag_off: <feature id>', '- rollback: <repo>', '- hotfix: <repo>: <what to change>',"
            " '- intent_change: feature|initiative: <the new intent>' (when it works as built but not as users need),"
            " '- monitor: <why no action>'. Prefer turning a recent feature's flag off over rolling back."
            + (f" The tool may itself {' and '.join(a.replace('_', ' ') for a in ctx['may'])} for a change released"
               " recently, at once, without waiting for on-call: when one of those fits, propose it first."
               if ctx.get("may") else "")
            + " For cost signals (spend above a budget or above what G4 approved): propose an intent_change to"
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

    def policy(state: IncidentState) -> dict:
        try:
            return services.snapshot(state["product_id"]).remediation or remediation.for_product({}, None)
        except Exception:
            return remediation.for_product({}, None)

    def people_decided(state: IncidentState) -> bool:
        return any(not str(g.get("approver") or "").startswith("agent:") for g in state.get("gate_decisions") or [])

    def merged(state: IncidentState, into: str = "") -> Command | None:
        """Merged into another incident while this one was busy: it ends here."""
        into = into or (getattr(services, "merged_into", None) or (lambda _: ""))(state["incident_id"]) or ""
        if not into:
            return None
        return Command(goto=END, update={"status": "merged", "links": {into: "merged into"},
                                         "events": [event("incident", "merged", f"into {into}")]})

    def auto(state: IncidentState) -> Command:
        """Mitigate by itself when the policy allows it; otherwise a person is needed."""
        if (done := merged(state)) is not None:
            return done
        if state.get("escalated") or people_decided(state):
            return Command(goto="decide")  # a person has it now
        pol = policy(state)
        if (state.get("auto_attempts") or 0) >= pol.get("attempts", 0):
            why = ("the tool's mitigation didn't fix it" if state.get("auto_attempts")
                   else "this product's remediation policy lets the tool take no action by itself")
            return Command(goto="escalate", update={"feedback": why})
        outside = remediation.applied_outside(state.get("analysis"))
        if outside:
            log_outside(state, outside)
            return Command(goto="escalate", update={"feedback": "OpenChoreo's fix agent changed production outside "
                                                                "the remediation policy: " + "; ".join(outside)[:300]})
        record = services.remediation_record(state["product_id"], pol["calibration"]["window_days"]) \
            if callable(getattr(services, "remediation_record", None)) else {}
        off = remediation.suspended(record, pol["calibration"]) if record else {}
        events = []
        if off:
            pol = {**pol, "autonomous": {k: v for k, v in pol["autonomous"].items() if k not in off}}
            events += log_suspension(state, off)
        chosen, why = remediation.choose(state.get("actions") or [],
                                         lambda repo: services.suspects(state["product_id"], repo),
                                         state.get("component", ""), pol)
        if not chosen:
            if off:
                why = "; ".join(off.values()) + f" ({why})"
            return Command(goto="escalate", update={"feedback": why, "events": events})
        ok, why = confident(state, pol, chosen[0])
        events.append(event("auto", "agreed" if ok else "not_confident", why))
        if not ok:
            return Command(goto="escalate", update={"feedback": why, "events": events})
        rec = {"gate": "remediation", "lane": state["product_id"], "at": now(), "decision": "approve",
               "approver": remediation.AGENT, "action_ids": [a["id"] for a in chosen]}
        return Command(goto="act", update={
            "actions": chosen, "auto_attempts": 1, "status": "mitigating", "gate_decisions": [rec],
            "events": events + [event("auto", "mitigating", ", ".join(
                f"{a['type']} {a.get('feature_id') or a.get('component') or a.get('repo', '')}".strip() for a in chosen),
                approver=remediation.AGENT)]})

    def confident(state: IncidentState, pol: dict, action: dict) -> tuple[bool, str]:
        """Two independent sources before the tool acts alone (policy `require_agreement`)."""
        if not pol.get("require_agreement", True):
            return True, "the policy lets the tool act on the release suspect alone"
        a = state.get("analysis") or {}
        if a.get("kind") == "rca" and a.get("status") not in (None, "skipped"):
            comp = action.get("component") if action["type"] == "rollback" else state.get("component", "")
            return remediation.agreement(a if a.get("status") == "completed" else {"status": a.get("status")},
                                         comp or state.get("component", ""))
        if pol.get("without_analysis") == "suspects":
            return True, f"no root-cause analysis ({a.get('why') or 'not set up'}); the policy lets the tool act on the release suspect alone"
        return False, f"no second opinion: OpenChoreo's root-cause analysis didn't run ({a.get('why') or 'not set up'})"

    def log_suspension(state: IncidentState, off: dict[str, str]) -> list[dict]:
        out = []
        log = getattr(services, "changes", None)
        for action, why in off.items():
            out.append(event("auto", "suspended", why))
            if log is None:
                continue
            from . import changes

            try:
                log.record(changes.new(
                    key=f"{state['product_id']}:suspend:{action}:{why}", product_id=state["product_id"], kind="policy",
                    type="automatic", action="suspend_autonomy", summary=f"Suspended the tool's own {action.replace('_', ' ')}",
                    targets=[{"kind": "policy", "name": f"autonomous.{action}"}], incident_id=state["incident_id"],
                    requested_by=remediation.AGENT, approved_by=["platform/remediation.yaml (calibration)"],
                    evidence={"track_record": why}, detail=why))
            except Exception:
                pass
        return out

    def log_outside(state: IncidentState, applied: list[str]) -> None:
        log = getattr(services, "changes", None)
        if log is None:
            return
        from . import changes

        try:
            log.record(changes.new(
                key=f"{state['incident_id']}:outside:{(state.get('analysis') or {}).get('report_id', '')}",
                product_id=state["product_id"], kind="external", type="automatic", action="applied_outside",
                summary=f"{state['incident_id']}: OpenChoreo's fix agent changed production",
                targets=[{"kind": "component", "name": state.get("component", "")}], incident_id=state["incident_id"],
                requested_by="openchoreo:sre-agent", approved_by=[], evidence={"actions": applied},
                detail="Applied outside the remediation policy; turn REMED_AGENT off"))
        except Exception:
            pass

    def escalate(state: IncidentState) -> Command:
        """The tool can't fix it: page on-call urgently, unless nothing is down."""
        signals = services.incident_signals(state["incident_id"])
        why = state.get("feedback") or "the tool could not fix it"
        quiet = remediation.no_page_reason(signals, state.get("actions") or [])
        pager = getattr(services, "pager", None)
        if quiet or pager is None:
            ev = event("escalate", "not_paged", quiet or "no pager configured")
        else:
            try:
                data_class = services.snapshot(state["product_id"]).product.data_class
            except Exception:
                data_class = "phi"
            said = pager.trigger({"incident_id": state["incident_id"], "product_id": state["product_id"],
                                  "title": state["title"], "severity": state["severity"],
                                  "component": state.get("component", ""), "data_class": data_class,
                                  "source": (signals[0] if signals else {}).get("source", ""), "why": why},
                                 urgent=True)
            ev = event("escalate", "not_paged" if said.startswith("not paged") else "paged", f"{why}: {said}")
        return Command(goto="decide", update={"escalated": True, "status": "awaiting_decision", "events": [ev]})

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

    def log_change(state: IncidentState, a: dict, result: str, approver: str, by_tool: bool, outcome: str,
                   n: int) -> list[dict]:
        """An incident's flag off or rollback in the change log (ops/changes.py), linked to the
        release it undid: that release caused the incident."""
        log = getattr(services, "changes", None)
        if log is None:
            return []
        from . import changes

        comp = a.get("component") or state.get("component", "")
        target = ({"kind": "flag", "name": services.flag_of(a["feature_id"]) or "", "on": False,
                   "feature_id": a["feature_id"]} if a["type"] == "flag_off" else {"kind": "component", "name": comp})
        try:
            stored = log.record(changes.new(
                key=f"{state['incident_id']}:{a['id']}:{n}", product_id=state["product_id"],
                kind="flag" if a["type"] == "flag_off" else "rollback",
                type="automatic" if by_tool else "emergency", action=a["type"],
                summary=f"{state['incident_id']}: {result}", targets=[target], incident_id=state["incident_id"],
                feature_id=a.get("feature_id", ""), requested_by=remediation.AGENT if by_tool else approver,
                approved_by=["platform/remediation.yaml"] if by_tool else [approver],
                evidence={"triage": section(state.get("triage", ""), "Likely cause")[:500]},
                outcome=outcome, detail="" if outcome == "done" else result))
            out = [event("act", "change_recorded", f"{stored['id']}: {stored['summary']}")]
            if outcome == "done":
                cause = changes.cause_of(log, state["product_id"], feature_id=a.get("feature_id", "") if a["type"] == "flag_off" else "",
                                         component="" if a["type"] == "flag_off" else comp)
                if cause:
                    log.link(cause["id"], state["incident_id"], f"undone by {stored['id']} ({a['type'].replace('_', ' ')})")
                    out.append(event("act", "cause", f"{cause['id']} ({cause['summary']}) caused it"))
            return out
        except Exception as exc:
            return [event("act", "change_not_recorded", f"{type(exc).__name__}: {exc}")]

    def act(state: IncidentState) -> Command:
        approver = (state.get("gate_decisions") or [{}])[-1].get("approver", "")
        by_tool = approver == remediation.AGENT
        snap = services.snapshot(state["product_id"])
        rt = snap.product.runtime or {}
        taken, links, events = [], {}, []
        for a in state.get("actions") or []:
            try:
                if a["type"] == "flag_off":
                    key = services.flag_of(a["feature_id"])
                    if not key:
                        raise ValueError(f"{a['feature_id']} has no feature flag")
                    # The flag lives in the feature's product: a product built on this one may own it.
                    services.flags.set(a["feature_id"].split("-FEAT-", 1)[0] if "-FEAT-" in a["feature_id"] else state["product_id"], key, False, actor=approver)
                    result = f"turned off {key}"
                elif a["type"] == "rollback":
                    comp = a["component"] or state.get("component", "")
                    rec = services.openchoreo.rollback(
                        component=(rt.get("components") or {}).get(comp, comp), env="production", actor=approver,
                        actor_type="policy" if by_tool else "human", project=rt.get("openchoreo_project"),
                        namespace=rt.get("namespace"))
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
                if a["type"] in ("flag_off", "rollback"):
                    events += log_change(state, a, result, approver, by_tool, "done", len(state.get("taken") or []))
            except Exception as exc:
                msg = f"{type(exc).__name__}: {exc}"
                taken.append({**a, "ok": False, "result": msg, "by": approver, "at": now()})
                events.append(event("act", f"{a['type']}_failed", msg, approver=approver))
                if a["type"] in ("flag_off", "rollback"):
                    events += log_change(state, a, msg, approver, by_tool, "failed", len(state.get("taken") or []))
        update = {"taken": taken, "links": links, "events": events, "status": "monitoring"}
        mitigations = [x for x in taken if x["type"] in ("flag_off", "rollback")]
        if by_tool and not all(x["ok"] for x in mitigations):
            failed = next(x for x in mitigations if not x["ok"])
            return Command(goto="escalate", update={**update, "status": "awaiting_decision",
                                                    "feedback": f"the tool's {failed['type'].replace('_', ' ')} failed"})
        return Command(goto="monitor", update=update)

    def monitor(state: IncidentState) -> Command:
        signals = services.incident_signals(state["incident_id"])
        mitigated = any(x.get("ok") and x["type"] in ("flag_off", "rollback") for x in state.get("taken") or [])
        machine = [s for s in signals if s.get("category") != "cost" and s["source"] != "ticket"]
        if not machine and not mitigated:  # a reported problem or a cost finding: nothing to watch recover
            return Command(goto="postmortem", update={"status": "postmortem"})
        sev = state["severity"]
        res = interrupt({"type": "external", "wait": "incident_recovery", "feature_id": state["incident_id"],
                         "product_id": state["product_id"], "component": state.get("component", ""),
                         "label": "Checking that it has recovered", "since": now(),
                         "minutes": RECOVERY_MINUTES.get(sev, 240),
                         "quiet": (policy(state).get("quiet_minutes") or remediation.QUIET_MINUTES).get(sev, 30)
                         if any(not resolves(s) for s in machine) or mitigated else 0})
        if res.get("status") == "merged":
            return merged(state, res.get("into", "")) or Command(goto=END)
        if res.get("status") == "success":
            return Command(goto="postmortem", update={"status": "postmortem",
                                                      "events": [event("monitor", "recovered", res.get("reason", ""))]})
        reason = res.get("reason", "")
        update = {"status": "awaiting_decision", "feedback": reason,
                  "events": [event("monitor", "still_firing", reason)]}
        if state.get("escalated"):
            return Command(goto="decide", update=update)
        if not people_decided(state) and (state.get("auto_attempts") or 0) < policy(state).get("attempts", 0):
            return Command(goto="triage", update=update)  # another look, and another try if allowed
        return Command(goto="escalate", update={**update, "feedback": f"still failing after the tool's mitigation: {reason}"[:300]
                                                if state.get("auto_attempts") else reason})

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
                              "done, follow-ups. No patient data or identifiers.\n\nTriage:\n"
                              + untrusted.wrap("triage", state.get("triage", ""))
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
    g.add_node("analyze", analyze)
    g.add_node("analysis_wait", analysis_wait, destinations=("triage", END))
    g.add_node("triage", triage)
    g.add_node("auto", auto, destinations=("act", "escalate", "decide", END))
    g.add_node("escalate", escalate, destinations=("decide",))
    g.add_node("decide", decide, destinations=("triage", "act", "postmortem"))
    g.add_node("act", act, destinations=("monitor", "escalate"))
    g.add_node("monitor", monitor, destinations=("postmortem", "decide", "escalate", "triage", END))
    g.add_node("postmortem", postmortem)
    g.add_node("close", close, destinations=("postmortem", END))
    g.add_edge(START, "analyze")
    g.add_edge("analyze", "analysis_wait")
    g.add_edge("triage", "auto")
    g.add_edge("postmortem", "close")
    return g.compile(checkpointer=checkpointer, name="vrixel_incident")


GRACE = timedelta(minutes=5)  # alert rules look back 5 minutes: what fires just after an action is old news
ERROR_LINES = 10  # errors in 5 minutes of logs at or above the default exceptions rule: still failing


def _clock() -> datetime:
    return datetime.now(timezone.utc)


def _at(value) -> datetime | None:
    try:
        t = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def recovery(signals: list[dict], since: str, minutes: int, quiet_minutes: int = 0, errors=None) -> dict | None:
    """The monitor wait's result: {"status": "success"} once it has recovered, {"status": "failure",
    "reason"} once it clearly hasn't, None while it's too early to say.

    Recovered: every alert that reports its own resolution has resolved, and, when the incident has
    signals that never do (OpenChoreo rules, GlitchTip, error-report alerts) or the tool mitigated
    it, it has been quiet for `quiet_minutes`: no new alert or error report after the first few
    minutes, and the component's logs below the exceptions rule (`errors()`, when they can be read).
    """
    started = _at(since) or _clock()
    elapsed = _clock() - started
    firing = still_firing(signals)
    fresh = [s for s in signals if s["status"] == "firing" and not resolves(s) and s.get("category") != "cost"
             and s["source"] != "ticket" and (_at(s.get("at")) or started) > started + GRACE]
    quiet_due = quiet_minutes and elapsed >= timedelta(minutes=quiet_minutes)
    if quiet_minutes and not quiet_due:
        if elapsed > timedelta(minutes=minutes):
            pass  # a window shorter than the quiet period: judge now
        else:
            return None
    problems = []
    if fresh:
        problems.append("new alerts since the action: " + "; ".join(s["title"][:80] for s in fresh[:3]))
    if quiet_minutes and not fresh:
        count = errors() if errors else None
        if count is not None and count >= ERROR_LINES:
            problems.append(f"{count} errors in the component's logs in the last 5 minutes")
    if not firing and not problems:
        return {"status": "success",
                "reason": f"quiet for {quiet_minutes} minutes" if quiet_minutes else "its alerts resolved"}
    if problems and quiet_minutes:
        return {"status": "failure", "reason": "; ".join(problems)}
    if elapsed > timedelta(minutes=minutes):
        return {"status": "failure", "reason": f"still firing after {minutes} minutes: "
                + "; ".join(s["title"] for s in (firing or fresh)[:5])}
    return None

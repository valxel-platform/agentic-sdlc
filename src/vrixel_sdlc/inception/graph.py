"""The inception thread: the brief, the brief gate, the architecture and roadmap, the product gate,
then setup. Both gates are the product council's (registry.yaml `inception.council`).

    research -> brief -> brief_gate --approve--> plan -> product_gate --approve--> setup -> end
                           |  revise: back to brief            |  revise: back to plan (architecture, then roadmap)
                           +-- abort: dropped                  +-- abort: dropped

Research (vrixel_sdlc.research) runs once; the brief cites it, and the brief gate shows it.

Thread ids are INCEPTION-PRD-<n>; the product doesn't exist in the registry until setup's pull
requests merge.
"""

from __future__ import annotations

import operator
from datetime import datetime, timezone
from typing import Annotated, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from ..state import last, merge_dict
from . import Vision
from . import setup as setup_mod
from .run import draft
from .run import research as research_note

PRODUCT_ID = "INCEPTION"
DECISIONS = ("approve", "revise", "abort")


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def event(node: str, kind: str, detail: str = "", **extra) -> dict:
    return {"at": now(), "node": node, "kind": kind, "detail": detail, **extra}


class InceptionState(TypedDict, total=False):
    inception_id: str
    product_id: str  # INCEPTION: the thread's index key, not a product
    title: str
    vision: dict
    created_by: str
    research: Annotated[str, last]
    brief: Annotated[str, last]
    architecture: Annotated[str, last]
    roadmap: Annotated[str, last]
    reports: Annotated[dict, merge_dict]  # doc -> the draft's report (status, attempts, findings, checklist, cost)
    feedback: Annotated[dict, merge_dict]  # doc -> what the gate asked to change
    status: Annotated[str, last]  # researching | drafting_brief | brief_gate | drafting_plan | product_gate | set_up | dropped
    gate_decisions: Annotated[list[dict], operator.add]
    events: Annotated[list[dict], operator.add]
    setup: Annotated[dict, last]
    cost_usd: Annotated[float, operator.add]


def council(services) -> list[str]:
    return list((services.settings.inception or {}).get("council") or [])


def _report(d) -> dict:
    return {k: v for k, v in d.as_dict().items() if k != "text"}


def _decide(raw) -> dict:
    from ..graph.pipeline import normalize

    return normalize(raw, DECISIONS)


def build_inception_graph(services, checkpointer=None):
    def vision(state: dict) -> Vision:
        return Vision(**state["vision"])

    def research(state: InceptionState) -> dict:
        r = research_note(services, vision(state))
        return {"research": r.text, "reports": {"research": {"doc": "research", "data_class": "phi",
                                                                 **{k: v for k, v in r.as_dict().items() if k != "text"}}},
                "cost_usd": r.cost_usd, "status": "drafting_brief",
                "events": [event("research", r.status, f"{r.depth}, {'searched the web' if r.web else 'no web search'}",
                                 findings=r.findings)]}

    def brief(state: InceptionState) -> dict:
        fb = (state.get("feedback") or {}).get("brief", "")
        d = draft(services, vision(state), "brief", feedback=fb, previous=state.get("brief", "") if fb else "",
                  research=state.get("research", ""))
        return {"brief": d.text, "reports": {"brief": _report(d)}, "status": "brief_gate", "cost_usd": d.cost_usd,
                "feedback": {"brief": ""},
                "events": [event("brief", d.status, f"{d.attempts} attempt(s)", findings=d.findings)]}

    def gate(state: InceptionState, name: str, preview: str, extra: dict) -> dict:
        report = (state.get("reports") or {})
        findings = [f for doc in extra.pop("docs") for f in (report.get(doc) or {}).get("findings", [])]
        packet = {"type": "gate", "gate": name, "kind": "inception", "phase": name, "feature_id": state["inception_id"],
                  "product_id": PRODUCT_ID, "title": state["title"], "preview": preview[:20000], "findings": findings,
                  "approvers": council(services), "cost_usd": round(float(state.get("cost_usd", 0.0)), 4),
                  "budget_usd": float((services.settings.inception or {}).get("budget_usd") or 0), "allowed": list(DECISIONS),
                  **extra}
        return _decide(interrupt(packet))

    def brief_gate(state: InceptionState) -> Command:
        from ..research import summary

        rep = (state.get("reports") or {}).get("research")
        preview = state["brief"] + (f"\n\n# The research behind it\n\n_{summary(rep)}_\n\n"
                                    + state["research"].split("\n", 1)[-1] if state.get("research") else "")
        d = gate(state, "brief", preview, {"docs": ["brief"], "research": summary(rep)})
        rec = {"gate": "brief", "at": now(), **d}
        if d["decision"] == "revise":
            return Command(goto="brief", update={"gate_decisions": [rec], "feedback": {"brief": d.get("feedback", "")},
                                                 "status": "drafting_brief",
                                                 "events": [event("brief_gate", "revise", d.get("feedback", ""),
                                                                  approver=d.get("approver", ""))]})
        if d["decision"] == "abort":
            return Command(goto=END, update={"gate_decisions": [rec], "status": "dropped",
                                             "events": [event("brief_gate", "dropped", d.get("feedback", ""),
                                                              approver=d.get("approver", ""))]})
        return Command(goto="plan", update={"gate_decisions": [rec], "status": "drafting_plan",
                                            "events": [event("brief_gate", "approved", approver=d.get("approver", ""))]})

    def plan(state: InceptionState) -> dict:
        v = vision(state)
        fb = (state.get("feedback") or {}).get("plan", "")
        a = draft(services, v, "architecture", brief=state["brief"], feedback=fb,
                  previous=state.get("architecture", "") if fb else "")
        update = {"architecture": a.text, "reports": {"architecture": _report(a)}, "cost_usd": a.cost_usd,
                  "status": "product_gate", "feedback": {"plan": ""},
                  "events": [event("architecture", a.status, f"{a.attempts} attempt(s)", findings=a.findings)]}
        if a.status == "passed":
            r = draft(services, v, "roadmap", brief=state["brief"], architecture=a.text, feedback=fb,
                      previous=state.get("roadmap", "") if fb else "")
            update |= {"roadmap": r.text, "reports": {"architecture": _report(a), "roadmap": _report(r)},
                       "cost_usd": a.cost_usd + r.cost_usd}
            update["events"].append(event("roadmap", r.status, f"{r.attempts} attempt(s)", findings=r.findings))
        return update

    def product_gate(state: InceptionState) -> Command:
        arch = (state.get("reports") or {}).get("architecture") or {}
        preview = state.get("architecture", "") + "\n\n" + (state.get("roadmap") or "")
        d = gate(state, "product", preview, {"docs": ["architecture", "roadmap"],
                                             "monthly_usd": arch.get("monthly_usd") or {}})
        rec = {"gate": "product", "at": now(), **d}
        if d["decision"] == "revise":
            return Command(goto="plan", update={"gate_decisions": [rec], "feedback": {"plan": d.get("feedback", "")},
                                                "status": "drafting_plan",
                                                "events": [event("product_gate", "revise", d.get("feedback", ""),
                                                                 approver=d.get("approver", ""))]})
        if d["decision"] == "abort":
            return Command(goto=END, update={"gate_decisions": [rec], "status": "dropped",
                                             "events": [event("product_gate", "dropped", d.get("feedback", ""),
                                                              approver=d.get("approver", ""))]})
        return Command(goto="setup", update={"gate_decisions": [rec],
                                             "events": [event("product_gate", "approved", approver=d.get("approver", ""))]})

    def setup(state: InceptionState) -> dict:
        approvers = ", ".join(sorted({g.get("approver", "") for g in state.get("gate_decisions") or []
                                      if g.get("decision") == "approve"}))
        out = setup_mod.run(services, vision(state), brief=state["brief"], architecture=state["architecture"],
                            roadmap=state["roadmap"], approved_by=approvers, thread_id=state["inception_id"],
                            research=state.get("research", ""))
        return {"setup": out, "status": "set_up",
                "events": [event("setup", s["step"], s.get("url") or s.get("error") or s.get("why", ""), ok=s["ok"])
                           for s in out["steps"]]}

    g = StateGraph(InceptionState)
    g.add_node("research", research)
    g.add_node("brief", brief)
    g.add_node("brief_gate", brief_gate, destinations=("brief", "plan", END))
    g.add_node("plan", plan)
    g.add_node("product_gate", product_gate, destinations=("plan", "setup", END))
    g.add_node("setup", setup)
    g.add_edge(START, "research")
    g.add_edge("research", "brief")
    g.add_edge("brief", "brief_gate")
    g.add_edge("plan", "product_gate")
    g.add_edge("setup", END)
    return g.compile(checkpointer=checkpointer, name="vrixel_inception")

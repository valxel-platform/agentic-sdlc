"""Design and architecture agree: checked once both G3 and G4 are approved, before planning.

UX design and architecture run side by side after G2, and neither sees the other's draft. Before
the plan is written:

    machine   the architecture changes a web or mobile app but UX design was skipped (design it);
              the UX spec has screens but the architecture changes no web or mobile app the product has
    critic    each screen gets the data and actions it needs from the architecture's endpoints;
              the error and empty states match how the architecture handles failure;
              nothing either one adds is missing from the other

No findings: planning starts. Findings: the people who approved decide, once, whether the design
or the architecture goes back (with the findings as feedback), or to carry on and say why.

    join -> fit -> provision                      (they agree)
                -> fit_gate -> design | architecture | provision
"""

from __future__ import annotations

from datetime import datetime, timezone

from pydantic import BaseModel, Field

from .agents.parse import SCREEN_ROLES, parse_affected_repos, parse_screens

DECISIONS = ("design", "architecture", "resolved", "abort")


class Mismatch(BaseModel):
    to: str = Field(pattern="^(design|architecture)$", description="Which document should change")
    text: str = Field(min_length=3, max_length=600, description="What doesn't fit, naming the screen and endpoint")


class FitReply(BaseModel):
    findings: list[Mismatch] = Field(default_factory=list)


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def machine(ux: str, hld: str, product_repos: list[dict], ui: bool) -> list[dict]:
    affected = parse_affected_repos(hld)
    apps = [r for r in affected if r.get("role") in SCREEN_ROLES]
    has_apps = [r for r in product_repos if r.get("role") in SCREEN_ROLES]
    screens = parse_screens(ux) if ux else {}
    out = []
    if apps and not ux:
        names = ", ".join(r["name"] for r in apps)
        out.append({"to": "design", "text": f"The architecture changes {names}, which people see, but UX design was "
                                            "skipped: design the screens it changes"})
    if screens and has_apps and not apps:
        out.append({"to": "architecture", "text": f"The UX spec has {len(screens)} screen(s) "
                    f"({', '.join(list(screens.values())[:3])}), but the architecture changes none of the product's web "
                    f"or mobile apps ({', '.join(r['name'] for r in has_apps)})"})
    return out


def critic(services, state: dict, ctx: dict, ux: str, hld: str) -> tuple[list[dict], float]:
    """A reader with both documents: what one needs that the other doesn't give."""
    from .agents import structured, untrusted
    from .graph.phase import call_agent

    if not ux or not hld:
        return [], 0.0
    tail = ("The UX spec and the architecture (HLD) for this feature were written side by side and approved "
            "separately. Find where they don't fit: a screen that needs data or an action no endpoint in the HLD "
            "gives; an error or empty state the HLD's failure handling makes impossible, or a failure the UX has no "
            "state for; a repo the HLD changes for screens the UX doesn't have. Name the screen (S1...) and the "
            "endpoint or repo. Only real mismatches, not style. For each, say which document should change "
            "('design' or 'architecture').\n\n" + structured.instruction(FitReply)
            + "\n\n" + untrusted.wrap("ux-spec", ux) + "\n\n" + untrusted.wrap("hld", hld))
    res = call_agent(services, role="critic", phase="architecture", ctx=ctx, skills=(), tail=tail,
                     stub_text=FitReply().model_dump_json(), max_tokens=1500, schema=structured.schema(FitReply))
    reply, _ = structured.parse(res.text, FitReply)
    return ([f.model_dump() for f in reply.findings] if reply else []), float(res.cost_usd or 0.0)


def make_fit(services):
    from langgraph.types import Command

    from .graph.phase import feature_ctx

    def fit(state: dict) -> Command:
        ctx = feature_ctx(services, state, "plan")
        texts = ctx.get("parent_texts") or {}
        ux, hld = texts.get("UX", ""), texts.get("HLD", "")
        found = machine(ux, hld, ctx.get("repos") or [], state.get("ui", True) is not False)
        cost = 0.0
        if not found:  # the critic only when the machine found nothing: one reason at a time
            found, cost = critic(services, state, ctx, ux, hld)
        ev = {"at": now(), "node": "fit", "kind": "agree" if not found else "mismatch",
              "detail": "; ".join(f["text"] for f in found)[:1000]}
        if not found:
            return Command(goto="provision", update={"fit": {"findings": [], "at": now()}, "events": [ev],
                                                     "cost_usd": cost})
        return Command(goto="fit_gate", update={"fit": {"findings": found, "at": now()}, "events": [ev],
                                                "cost_usd": cost})

    return fit, ("provision", "fit_gate")


def make_fit_gate(services):
    from langgraph.graph import END
    from langgraph.types import Command, interrupt

    from .graph.pipeline import normalize

    def fit_gate(state: dict) -> Command:
        snap = services.feature_snapshot(state)
        found = (state.get("fit") or {}).get("findings") or []
        approvers = list(dict.fromkeys(list(snap.product.approvers.get("g4", [])) + list(snap.product.approvers.get("g3", []))))
        d = normalize(interrupt({
            "type": "escalation", "phase": "fit", "feature_id": state["feature_id"], "product_id": state["product_id"],
            "title": state["title"], "approvers": approvers, "allowed": list(DECISIONS),
            "reason": "The design and the architecture don't fit together yet.", "findings": [f["text"] for f in found],
            "fit": found, "cost_usd": round(float(state.get("cost_usd", 0.0)), 4)}), DECISIONS)
        rec = {"gate": "fit", "phase": "fit", "lane": state["product_id"], "decision": d["decision"],
               "approver": d["approver"], "feedback": d["feedback"], "active_minutes": d["active_minutes"], "at": now()}
        if d["decision"] == "abort":
            return Command(goto=END, update={"gate_decisions": [rec], "status": "aborted",
                                             "events": [{"at": now(), "node": "fit", "kind": "aborted",
                                                         "detail": d["feedback"]}]})
        if d["decision"] == "resolved":
            if not d["feedback"].strip():
                from .graph import InvalidDecision

                raise InvalidDecision("Carrying on with a mismatch needs a reason")
            return Command(goto="provision", update={"gate_decisions": [rec], "events": [
                {"at": now(), "node": "fit", "kind": "accepted", "detail": d["feedback"], "approver": d["approver"]}]})
        to = d["decision"]
        mine = [f["text"] for f in found if f["to"] == to] or [f["text"] for f in found]
        said = "The design and the architecture don't fit:\n" + "\n".join(f"- {t}" for t in mine) + (
            f"\n\n{d['feedback']}" if d["feedback"].strip() else "")
        gate = "g3" if to == "design" else "g4"
        update = {"gate_decisions": [rec], "feedback": {to: said}, "approvals": {gate: False},
                  "events": [{"at": now(), "node": "fit", "kind": f"back_to_{to}", "detail": said[:1000],
                              "approver": d["approver"]}]}
        if to == "design":
            update |= {"ui": True, "phase_status": {"design": "pending"}}
        return Command(goto=to, update=update)

    return fit_gate, ("design", "architecture", "provision", END)

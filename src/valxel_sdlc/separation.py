"""Separation of duties at gates (engineering review, finding 6).

Whoever wrote the work doesn't approve it. Two kinds of author:

- the person who started the feature (its intent): they asked for it, so their approval at a gate
  that applies is not an independent one;
- a person who took a phase over and handed back their own version of its document: their hand-back
  no longer counts as the gate's approval where this applies; another approver must approve it
  (graph/gates.py asks), and a later round of the same phase remembers who rewrote it.

Where it applies (registry.yaml limits): every gate of a product whose data class is in
`separation_of_duties_data_classes` (PHI and payments by default), and the gates in
`separation_of_duties_gates` for every product (G5 and G6 by default: code review and production).

With one approver (a solo founder), `separation_of_duties_waiver` lets the author approve anyway after
writing why no one else can (`waiver_reason`, at least WAIVER_MIN characters). That is a recorded
exception, not the control: the decision says `separation: waived` with the reason, the audit event
carries both, and GET /api/waivers lists them for someone to review afterwards. Emptying the lists
above turns the rule off with nothing recorded; prefer the waiver.

The engine checks it for every decision, however it arrives (web, bulk approval, MCP, A2A), and the
decision records `separation: checked` (or `waived`) so an audit can see the rule ran.
"""

from __future__ import annotations

from collections.abc import Iterable

WAIVER_MIN = 20  # characters: a reason, not "ok"


class SeparationOfDuties(PermissionError):
    pass


def applies(limits, data_class: str, gate: str) -> bool:
    gate = (gate or "").lower()
    if not gate.startswith("g"):
        return False
    return data_class in tuple(getattr(limits, "separation_of_duties_data_classes", ())) or gate in tuple(
        getattr(limits, "separation_of_duties_gates", ()))


def _norm(who: str) -> str:
    return (who or "").strip().lower()


def authors(state: dict, item: dict) -> set[str]:
    """Who wrote what this question asks about: the feature's starter, whoever rewrote this phase's
    document in an earlier round (its takeover hand-backs), and whoever rewrote it in this one."""
    phase = item.get("phase")
    out = {_norm(state.get("created_by", ""))}
    out |= {_norm(r.get("approver", "")) for r in state.get("gate_decisions") or []
            if r.get("phase") == phase and r.get("decision") in ("takeover", "handback")}
    out.add(_norm(item.get("rewritten_by", "")))
    return out - {""}


def check(state: dict, item: dict, decision: dict, data_class: str, limits) -> str:
    """Raise SeparationOfDuties when the decider may not approve this; return 'checked' when the rule
    applied and they may, '' when it doesn't apply."""
    gate = str(item.get("gate") or "")
    if item.get("type") not in ("gate", "handback") or not applies(limits, data_class, gate):
        return ""
    who = _norm(decision.get("approver", ""))
    approving = decision.get("decision") == "approve" or (
        item.get("type") == "handback" and decision.get("decision") == "handback"
        and not _changed(item.get("content", ""), decision.get("artifact_content")))
    if approving and who and who in authors(state, item):
        rewrote = who == _norm(item.get("rewritten_by", "")) or who in _rewriters(state, item)
        why = "you rewrote this document" if rewrote else "you started this feature"
        if getattr(limits, "separation_of_duties_waiver", False):
            if len((decision.get("waiver_reason") or "").strip()) >= WAIVER_MIN:
                return "waived"
            raise SeparationOfDuties(
                f"{gate.upper()} needs an approver who didn't write the work: {why}. To approve it yourself, "
                "say why no one else can (a waiver: recorded with the decision and listed for review).")
        raise SeparationOfDuties(
            f"{gate.upper()} needs an approver who didn't write the work: {why}. Another approver decides "
            "this one; you can still send it back or stop it.")
    return "checked"


def _rewriters(state: dict, item: dict) -> Iterable[str]:
    return {_norm(r.get("approver", "")) for r in state.get("gate_decisions") or []
            if r.get("phase") == item.get("phase") and r.get("decision") in ("takeover", "handback")}


def _changed(draft: str, mine) -> bool:
    return bool(mine) and str(mine).strip() != (draft or "").strip()

"""What a person may decide at a gate or an escalation, and the check that a decision is one of them.

The delivery pipeline, the roadmap's graphs, inception, outcome reviews and incidents all read
these, so they're the core's (ADR 0005); routing between phases stays in graph/routing.py.
"""

from __future__ import annotations

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


def lane_approvers(lane, gate: str) -> list[str]:
    """G0 falls back to G1's approvers (product owners); a release gate uses G6's."""
    return list(lane.approvers.get(gate) or lane.approvers.get({"g0": "g1", "release": "g6"}.get(gate, gate)) or [])


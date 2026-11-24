"""The cost plan G4 approves: the AWS services a feature's design needs and their monthly cost per
environment. The core's: the pipeline's G4 writes it and ops' cost review reads it back (ADR 0005);
collecting actual costs stays in ops/cost.py.
"""

from __future__ import annotations

from datetime import UTC, datetime


def now() -> datetime:
    return datetime.now(UTC)


def plan_from_g4(summary: dict | None, state: dict, approvers: str) -> dict:
    """The cost plan G4 approved: AWS services and their monthly cost per environment, and what
    building the feature had cost when the design was approved."""
    s = summary or {}
    return {"approved_at": now().isoformat(), "approved_by": approvers,
            "aws_monthly_usd": {k: float(v) for k, v in (s.get("monthly_usd") or {}).items()},
            "resources": [{"name": r["name"], "type": r["type"], "monthly_usd": dict(r["monthly_usd"])}
                          for r in s.get("resources") or []],
            "accounts": dict(s.get("accounts") or {}),
            "build_usd_at_g4": round(float(state.get("cost_usd", 0.0)), 4),
            "token_budget_usd": state.get("token_budget_usd")}


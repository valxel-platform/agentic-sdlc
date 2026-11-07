"""Gradual releases: a feature reaches production behind its flag, for a growing share of users.

G6 approves the plan (platform/remediation.yaml `gradual_release`, narrowed per product):

    steps          [5, 25, 50, 100]: the share of users with the feature on at each step
    bake_minutes   how long each step must stay quiet, by the feature's risk tier

At G6 the flag goes on for the first step. The tool then steps up when the step has been quiet
for its bake time: no incident open on the feature's production components, and none recovered
within the bake time. An incident holds it. The flag going off (the tool's or on-call's flag off
in an incident) stops it, and the release managers decide: start again from the first step, leave
it off, or stop the feature. Each step is a change record, approved by G6.

Users are bucketed by the OpenFeature targeting key, so each person keeps the same answer as the
share grows: services pass the signed-in user, the web and mobile apps a stable per-device id.

Stub delivery compresses the bake times, like it does deployments.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

COMPRESS_IN_STUB = True
DEFAULT_TIER = "high"


def _clock() -> datetime:
    return datetime.now(timezone.utc)


def _at(value) -> datetime | None:
    try:
        t = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def plan_for(policy: dict | None, risk_tier: str | None) -> dict | None:
    """The plan G6 approves for a feature, or None to release at once."""
    g = (policy or {}).get("gradual_release")
    if not g:
        return None
    tier = risk_tier if risk_tier in g["bake_minutes"] else DEFAULT_TIER
    return {"steps": list(g["steps"]), "bake_minutes": int(g["bake_minutes"].get(tier, 60)), "risk_tier": tier,
            "step": 0, "history": []}


def percent_label(p: float) -> str:
    return f"{int(p)}%" if float(p).is_integer() else f"{p:g}%"


def check(*, on: bool | None, percent: float | None, since: str, bake_minutes: int, incidents: list[dict],
          stub: bool = False) -> dict | None:
    """The step's result: success to step up, failure when the flag went off, None to keep waiting.
    `incidents`: the product's incidents on the feature's components ({status, events, opened_at})."""
    if on is False or percent == 0:
        return {"status": "failure", "reason": "the flag was turned off"}
    now = _clock()
    start = _at(since) or now
    quiet_from = start
    for inc in incidents:
        if inc.get("status") not in ("postmortem", "closed", "merged"):
            return None  # an incident on its components holds the release
        ended = next((_at(e.get("at")) for e in reversed(inc.get("events") or [])
                      if e.get("kind") in ("recovered", "resolved", "closed")), None)
        if ended and ended > quiet_from:
            quiet_from = ended
    if (stub and COMPRESS_IN_STUB) or now - quiet_from >= timedelta(minutes=bake_minutes):
        return {"status": "success", "at": now.isoformat(), "quiet_since": quiet_from.isoformat()}
    return None

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
TIERS_BY_RISK = ("low", "high", "regulated")


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
            "compare": dict(g.get("compare") or {}), "step": 0, "history": []}


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


def on_regression(services, *, product_id: str, thread_id: str, title: str, flags: dict[str, str], res: dict,
                  policy: dict | None, release_id: str = "") -> tuple[list[dict], str]:
    """Users with a flag on fail more (ops/cohorts.py): turn the regressing flags off when the
    remediation policy lets the tool turn flags off, else page on-call urgently. Returns
    (events, what happened) for the release managers' decision."""
    from datetime import datetime as _dt

    from ..roadmap.graphs import event
    from .changes import new as new_change
    from .cohorts import describe
    from .remediation import ROLLOUT_AGENT

    worse = res.get("regression") or {}
    keys = {f: k for f, k in flags.items() if k in worse}
    events: list[dict] = []
    if "flag_off" in ((policy or {}).get("autonomous") or {}):
        for fid, key in keys.items():
            try:
                services.flags.set(product_id, key, False, actor=ROLLOUT_AGENT)
            except Exception as exc:
                events.append(event("rollout", "flag_failed", f"Couldn't turn off {key}: {exc}"))
                continue
            events.append(event("rollout", "flag_off", describe(key, worse[key])))
            log = getattr(services, "changes", None)
            if log is not None:
                try:
                    stored = log.record(new_change(
                        key=f"{thread_id}:regression:{key}:{_dt.now().strftime('%Y%m%d%H%M')}", product_id=product_id,
                        kind="flag", type="automatic", action="flag_off",
                        summary=f"{title}: turned {key} off, its users fail more",
                        targets=[{"kind": "flag", "name": key, "on": False, "feature_id": fid}],
                        feature_id=fid if fid.startswith(product_id) and "-FEAT-" in fid else "", release_id=release_id,
                        requested_by=ROLLOUT_AGENT, approved_by=["platform/remediation.yaml"],
                        evidence={"cohorts": worse[key]}, detail=describe(key, worse[key])))
                    events.append(event("rollout", "change_recorded", f"{stored['id']}: {stored['summary']}"))
                except Exception as exc:
                    events.append(event("rollout", "change_not_recorded", f"{type(exc).__name__}: {exc}"))
        return events, "The tool turned the flag off: " + "; ".join(describe(k, worse[k]) for k in keys.values())
    pager = getattr(services, "pager", None)
    if pager is not None:
        try:
            said = pager.trigger({"incident_id": thread_id, "product_id": product_id, "title": title, "severity": "sev2",
                                  "data_class": "phi", "source": "gradual-release",
                                  "why": "its users fail more and the tool may not turn flags off"}, urgent=True)
            events.append(event("rollout", "paged", said))
        except Exception as exc:
            events.append(event("rollout", "not_paged", f"{type(exc).__name__}: {exc}"))
    return events, "The flag is still on (this product doesn't let the tool turn flags off): " + "; ".join(
        describe(k, worse[k]) for k in keys.values())

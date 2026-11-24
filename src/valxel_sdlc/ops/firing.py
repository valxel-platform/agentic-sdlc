"""What is still firing: the alerts and issues an incident waits on, and the alarms G6 approvers see
before production. The core's: delivery's G6, the roadmap's views and ops all read them (ADR 0005);
the stores that keep signals and alarm states stay in ops (signals.py, alarms.py).
"""

from __future__ import annotations

from datetime import UTC, datetime


def still_firing(signals: list[dict]) -> list[dict]:
    """The latest state of each alert or issue that is still firing. Tickets and notifications that
    never send a resolution (resolves: false) aren't waited on."""
    latest: dict[str, dict] = {}
    for s in signals:
        latest[s["fingerprint"]] = s
    return [s for s in latest.values() if s["status"] == "firing" and resolves(s)]


def resolves(signal: dict) -> bool:
    return signal["source"] != "ticket" and signal.get("resolves", True) is not False



def _age_minutes(at: str, now: datetime) -> float:
    try:
        t = datetime.fromisoformat(str(at).replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    return (now - (t if t.tzinfo else t.replace(tzinfo=UTC))).total_seconds() / 60


def for_feature(board, state: dict, *, before: str = "production", now: datetime | None = None,
                fresh_minutes: int = 60) -> list[dict]:
    """Alarms firing outside `before` on the feature's own resources, on resources its components
    use, or on its components themselves: what G6 approvers should see before production. Alerts
    that never send a resolution (OpenChoreo's) count only while recent."""
    if board is None:
        return []
    now = now or datetime.now(UTC)
    fid = state.get("feature_id", "")
    mine = {r["name"] for r in state.get("resources") or [] if r.get("type") != "workload-identity"}
    comps = {d.get("component") for d in (state.get("deployments") or {}).values() if d.get("component")}
    comps |= {r.get("owner") for r in state.get("resources") or []}
    out = []
    for s in board.firing(state.get("product_id", "")):
        lab = s.get("labels") or {}
        users = set(filter(None, (lab.get("consumers") or "").split(",")))
        if s.get("env") == before:
            continue
        if s.get("resolves") is False and _age_minutes(s.get("at", ""), now) > fresh_minutes:
            continue
        if (lab.get("feature_id") == fid or lab.get("resource") in mine or users & comps
                or (s.get("source") == "openchoreo" and s.get("component") in comps)):
            out.append({"env": s["env"], "resource": lab.get("resource", ""), "alarm": lab.get("alarm", ""),
                        "severity": s["severity"], "says": s.get("detail", ""), "since": s.get("since", ""),
                        "url": s.get("url", ""), "title": s["title"]})
    return out


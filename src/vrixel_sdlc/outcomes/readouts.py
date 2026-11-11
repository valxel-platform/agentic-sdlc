"""Readouts: whether a released feature moved its goal, and whether each initiative's goal is moving
(step 3 of the outcome loop).

    feature released (finalize)  ->  after each window (tools.metrics.windows, default 7, 30, 90 days):
        before  the goal's metric over the same length of time just before release
        after   the goal's metric over the window after release
        change  after - before, and whether it went toward the target

    every goal, once a week       ->  a reading of the last 30 days: the goal's trend line

    initiative                    ->  its goal's latest reading against the baseline and target,
                                      prorated to the time since the initiative was approved:
        on track    moved toward the target by at least a quarter of the share of the time gone
                    (and at least 5% of the gap)
        flat        moved less than that either way
        off track   moved away from the target by more than 5% of the gap
        no data     no reading yet, or a suppressed one, or no baseline to compare with

A before-and-after readout is not proof the feature caused the change; it says so. Flag cohorts
(step 4) are the comparison. Goals entered by hand ask for their number when a readout is due.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from . import Metric, parse_metric
from . import read as rd

TREND_DAYS = 30
TREND_EVERY = timedelta(days=7)
RETRIES = 7  # daily tries for a readout whose number isn't there yet
FLAT_SHARE, MIN_BAND = 0.25, 0.05
CAVEAT = "Before and after release: other changes in the same period count too."


def _t(iso) -> datetime | None:
    if not iso:
        return None
    try:
        t = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def _now(now=None) -> datetime:
    return now or datetime.now(timezone.utc)


def released_at(state: dict) -> datetime | None:
    """When the feature reached all users: the pipeline's finalize step."""
    for e in reversed(state.get("events") or []):
        if e.get("node") == "finalize" and e.get("kind") == "done":
            return _t(e.get("at"))
    return None


def feature_goals(services, state: dict) -> list[str]:
    from ..agents.parse import parse_product_goal

    arts = state.get("artifacts") or {}
    if "OPP" not in arts:
        return []
    try:
        return parse_product_goal(services.artifacts.load(arts["OPP"]))[0]
    except Exception:
        return []


def metrics(services, product_id: str) -> dict[str, Metric]:
    out = {}
    for g in rd.goals(services, product_id):
        m, problems = parse_metric(g["id"], g.get("metric"))
        if m is not None and not problems:
            out[g["id"]] = m
    return out


def toward(metric: Metric, before: float | None, after: float | None) -> bool | None:
    if before is None or after is None or after == before:
        return None
    return (after < before) if metric.direction == "down" else (after > before)


def _reading(services, product_id: str, metric: Metric, start: datetime, end: datetime) -> dict:
    r = rd.read(services, product_id, metric.goal_id, start=start.isoformat(), end=end.isoformat(), record=False)
    return r.as_dict()


def take(services, product_id: str, feature_id: str, metric: Metric, window: int, released: datetime) -> dict:
    """One readout: the goal before and after release, over `window` days each."""
    span = timedelta(days=window)
    before = _reading(services, product_id, metric, released - span, released)
    if before["status"] != "ok" and metric.baseline is not None:
        before = {**before, "status": "ok", "value": metric.baseline, "note": "the goal's baseline (no reading before)"}
    after = _reading(services, product_id, metric, released, released + span)
    b = before["value"] if before["status"] == "ok" else None
    a = after["value"] if after["status"] == "ok" else None
    return {"product_id": product_id, "feature_id": feature_id, "goal_id": metric.goal_id, "window_days": window,
            "released_at": released.isoformat(), "source": metric.source, "unit": metric.unit,
            "direction": metric.direction, "before": before, "after": after,
            "change": round(a - b, 4) if a is not None and b is not None else None,
            "toward": toward(metric, b, a), "kind": "before_after", "caveat": CAVEAT,
            "at": datetime.now(timezone.utc).isoformat()}


def run(engine, product_id: str | None = None, now: datetime | None = None) -> list[dict]:
    """Take every readout that is due (and retry ones still waiting for their number), then the
    weekly trend reading of each goal. Returns the readouts taken."""
    services = engine.services
    now = _now(now)
    products = [product_id] if product_id else sorted({r["product_id"] for r in engine.index.all()})
    taken = []
    for pid in products:
        try:
            ms = metrics(services, pid)
        except Exception:
            continue
        if not ms:
            continue
        cfg, _ = rd.product_metrics(services, pid)
        have = {(r["feature_id"], r["goal_id"], r["window_days"]): r for r in services.readings.readouts(pid)}
        for f in engine.feature_summaries(pid):
            if f["status"] != "released":
                continue
            state = engine._state(f["feature_id"])
            released = released_at(state)
            if released is None:
                continue
            for gid in feature_goals(services, state):
                if gid not in ms:
                    continue
                for w in cfg["windows"]:
                    if released + timedelta(days=w) > now:
                        continue
                    old = have.get((f["feature_id"], gid, w))
                    if old and (old["after"]["status"] in ("ok", "suppressed") or old.get("tries", 1) >= RETRIES
                                or _t(old.get("at")) and now - _t(old["at"]) < timedelta(hours=20)):
                        continue
                    r = take(services, pid, f["feature_id"], ms[gid], w, released)
                    r["tries"] = (old.get("tries", 1) + 1) if old else 1
                    services.readings.put_readout(r)
                    taken.append(r)
        for gid, m in ms.items():
            if m.source == "manual":
                continue  # the trend of a hand-entered goal is the numbers people enter
            last = [h for h in services.readings.history(pid, gid) if (h.get("extra") or {}).get("kind") == "trend"]
            if last and now - (_t(last[-1]["end"]) or now) < TREND_EVERY:
                continue
            r = rd.read(services, pid, gid, start=(now - timedelta(days=TREND_DAYS)).isoformat(), end=now.isoformat(),
                        record=False)
            r.extra = {**r.extra, "kind": "trend"}
            services.readings.add(r)
    return taken


# ------------------------------------------------------------------------------- status

def progress(metric: Metric, value: float) -> float | None:
    """How far from the baseline toward the target, as a share of the gap (negative: away)."""
    if metric.baseline is None:
        return None
    if metric.target is not None and metric.target != metric.baseline:
        return (value - metric.baseline) / (metric.target - metric.baseline)
    if metric.baseline == 0:
        return None
    sign = -1 if metric.direction == "down" else 1
    return sign * (value - metric.baseline) / abs(metric.baseline)


def status(metric: Metric, value: float | None, start: datetime | None, now: datetime | None = None) -> dict:
    now = _now(now)
    if value is None:
        return {"status": "no_data", "why": "no reading yet"}
    p = progress(metric, value)
    if p is None:
        return {"status": "no_data", "why": "no baseline to compare with: set metric.baseline"}
    expected = None
    by = _t(metric.by + "T00:00:00+00:00") if metric.by else None
    if by and start and by > start:
        expected = max(0.0, min(1.0, (now - start) / (by - start)))
    band = max(MIN_BAND, FLAT_SHARE * expected) if expected is not None else MIN_BAND
    s = "on_track" if p >= band else "off_track" if p <= -MIN_BAND else "flat"
    return {"status": s, "progress": round(p, 3), "expected": round(expected, 3) if expected is not None else None}


def trend(services, product_id: str, goal_id: str, metric: Metric) -> list[dict]:
    """The goal's readings over time: weekly trend readings, or the numbers people entered."""
    kind = "manual" if metric.source == "manual" else "trend"
    rows = [h for h in services.readings.history(product_id, goal_id)
            if (h.get("source") == "manual" if kind == "manual" else (h.get("extra") or {}).get("kind") == "trend")]
    out = []
    for h in rows[-26:]:
        out.append({"end": h["end"], "value": h.get("value") if h.get("status") == "ok" else None,
                    "status": h.get("status"), "note": h.get("note", "")})
    return out


def latest(points: list[dict]) -> float | None:
    for p in reversed(points):
        if p["value"] is not None:
            return p["value"]
    return None


def product_outcomes(engine, product_id: str, roadmap: dict | None = None, now: datetime | None = None) -> dict:
    """What the Roadmap shows: each goal's trend and status, each initiative's status, each
    feature's readouts, and the numbers people are asked to enter."""
    services = engine.services
    now = _now(now)
    try:
        ms = metrics(services, product_id)
        cfg, _ = rd.product_metrics(services, product_id)
    except Exception:
        return {"goals": {}, "initiatives": {}, "features": {}, "wanted": [], "windows": []}
    readouts = services.readings.readouts(product_id) if getattr(services, "readings", None) else []
    goals = {}
    for gid, m in ms.items():
        points = trend(services, product_id, gid, m)
        if not any(p["value"] is not None for p in points):  # fall back to the newest readout
            after = [r["after"] for r in readouts if r["goal_id"] == gid and r["after"]["status"] == "ok"]
            points = points + [{"end": a["end"], "value": a["value"], "status": "ok", "note": "a readout"} for a in after[-1:]]
        value = latest(points)
        goals[gid] = {"metric": m.as_dict(), "trend": points, "latest": value,
                      **status(m, value, None, now)}
    inits = {}
    for i in (roadmap or {}).get("initiatives") or []:
        gid = i.get("goal") or ""
        if gid in ms:
            start = _t(i.get("approved_at") and str(i["approved_at"]) + ("" if "T" in str(i["approved_at"]) else "T00:00:00+00:00"))
            inits[i["id"]] = {"goal_id": gid, **status(ms[gid], goals[gid]["latest"], start, now)}
    features: dict[str, list] = {}
    for r in readouts:
        features.setdefault(r["feature_id"], []).append(r)
    wanted = [{"goal_id": r["goal_id"], "feature_id": r["feature_id"], "window_days": r["window_days"],
               "start": r["after"]["start"], "end": r["after"]["end"], "unit": r.get("unit", "")}
              for r in readouts if r["source"] == "manual" and r["after"]["status"] == "no_data"]
    return {"goals": goals, "initiatives": inits, "features": features, "wanted": wanted,
            "windows": list(cfg["windows"]), "min_cell": cfg["min_cell"]}


def retake_waiting(engine, product_id: str, goal_id: str) -> list[dict]:
    """After someone enters a number: retake the readouts of that goal that were waiting for one."""
    services = engine.services
    ms = metrics(services, product_id)
    if goal_id not in ms:
        return []
    out = []
    for r in services.readings.readouts(product_id):
        if r["goal_id"] == goal_id and r["after"]["status"] == "no_data":
            new = take(services, product_id, r["feature_id"], ms[goal_id], int(r["window_days"]),
                       _t(r["released_at"]))
            new["tries"] = r.get("tries", 1)
            services.readings.put_readout(new)
            out.append(new)
    return out


def feature_outcome(engine, feature_id: str) -> dict:
    """What the feature page shows: the goals the feature moves, its events in staging, and its
    readouts so far (and which windows are still to come)."""
    services = engine.services
    state = engine._state(feature_id)
    pid = state.get("product_id", "")
    goals = feature_goals(services, state)
    released = released_at(state)
    try:
        cfg, _ = rd.product_metrics(services, pid)
        ms = metrics(services, pid)
    except Exception:
        cfg, ms = {"windows": ()}, {}
    rows = [r for r in services.readings.readouts(pid) if r["feature_id"] == feature_id]
    have = {(r["goal_id"], int(r["window_days"])) for r in rows}
    upcoming = [{"goal_id": g, "window_days": w, "due": (released + timedelta(days=w)).date().isoformat()}
                for g in goals if g in ms for w in cfg["windows"] if released and (g, w) not in have]
    return {"feature_id": feature_id, "product_id": pid, "goals": goals,
            "metrics": {g: ms[g].as_dict() for g in goals if g in ms},
            "released_at": released.isoformat() if released else "",
            "instrumentation": state.get("instrumentation_check") or None,
            "readouts": rows, "upcoming": upcoming}

"""A visible path: how long each step of a feature usually takes, and when it should be done.

Each station of the rail (engine.RAIL) gets an estimate from the product's own history:

    agent steps   the median time between one station finishing and the next, over the product's
                  features that went through both (events in the feature's state)
    gates         the median time people took to decide them (waited_minutes on gate decisions)

Without enough history (fewer than MIN_SAMPLES), a default stands in. The expected finish is now
plus what's left: the current station counts half. Estimates are planning figures: the page says so.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from statistics import median

MIN_SAMPLES = 3
DEFAULT_MINUTES = {
    "discovery": 20, "spec": 20, "design": 30, "architecture": 45, "plan": 15, "build": 180, "release": 60,
    "operate": 3 * 24 * 60,  # a gradual release over days
    "g1": 240, "g2": 240, "g3": 240, "g4": 240, "g5": 120, "g6": 240,
}
# Pipeline nodes that belong to a station other than their own name.
NODE_STATION = {
    "cs_wait": "build", "merge_train": "build", "build_wait": "build", "deploy_dev": "build", "dev_wait": "build",
    "deploy_staging": "release", "staging_wait": "release", "production_wait": "operate", "flag_rollout": "operate",
    "finalize": "operate",
}


def _t(iso: str) -> datetime | None:
    try:
        t = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def station_of(node: str, keys: set[str]) -> str:
    if node in keys:
        return node
    if node in NODE_STATION:
        return NODE_STATION[node]
    return next((k for k in keys if node.startswith(k)), "")


def finished_at(state: dict, keys: list[str]) -> dict[str, datetime]:
    """When each station last finished, from the feature's events."""
    ks = set(keys)
    out: dict[str, datetime] = {}
    for e in state.get("events") or []:
        st = station_of(str(e.get("node") or ""), ks)
        t = _t(e.get("at", ""))
        if st and t and e.get("kind") not in ("revise", "aborted", "failed"):
            out[st] = max(out.get(st, t), t)
    return out


def history(states: list[dict], rail: list[tuple[str, str]]) -> dict[str, float]:
    """Median minutes per station over the product's features, where there are enough samples."""
    keys = [k for k, _ in rail]
    samples: dict[str, list[float]] = {k: [] for k in keys}
    for s in states:
        for g in s.get("gate_decisions") or []:
            w = g.get("waited_minutes")
            if w is not None and g.get("gate") in samples and g.get("decision") == "approve":
                samples[g["gate"]].append(float(w))
        done = finished_at(s, keys)
        prev = None
        for k, kind in rail:
            if k in done and prev is not None and kind == "phase":
                samples[k].append(max(0.0, (done[k] - prev).total_seconds() / 60))
            if k in done:
                prev = done[k]
    return {k: median(v) for k, v in samples.items() if len(v) >= MIN_SAMPLES}


def estimate(stations: list[dict], learned: dict[str, float], now: datetime | None = None) -> dict:
    """Each station's estimate in minutes, and the expected finish."""
    now = now or datetime.now(timezone.utc)
    left = 0.0
    out = []
    for st in stations:
        minutes = learned.get(st["key"], DEFAULT_MINUTES.get(st["key"], 60))
        sure = st["key"] in learned
        if st["state"] in ("todo", "current", "waiting"):
            left += minutes / 2 if st["state"] in ("current", "waiting") else minutes
        out.append({**st, "eta_minutes": round(minutes), "from_history": sure})
    stopped = any(st["state"] == "stopped" for st in stations)
    finished = all(st["state"] in ("done", "skipped") for st in stations)
    return {"stations": out, "expected_done": None if stopped or finished else (now + timedelta(minutes=left)).isoformat(),
            "minutes_left": None if stopped or finished else round(left)}


def describe(minutes: float) -> str:
    if minutes < 90:
        return f"about {max(5, round(minutes / 5) * 5)} minutes"
    if minutes < 36 * 60:
        return f"about {round(minutes / 60)} hours"
    return f"about {round(minutes / 1440)} days"

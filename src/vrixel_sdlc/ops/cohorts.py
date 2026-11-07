"""Comparing users with a feature on against users with it off, during a gradual release.

Time alone (ops/rollout.py) can miss a feature that hurts only the users who have it: at 5%, its
errors are a small share of the component's. So the backend templates count, per flag, the
requests that evaluated it and how many of them failed (a 5xx or an unhandled exception), apart
for users who got it on and users who got it off, and log one line per flag a minute:

    vrixel.flag_cohorts {"flag": "emr.emr-feat-3.kiosk", "on": {"requests": 40, "errors": 0},
                         "off": {"requests": 760, "errors": 2}}

The tool sums those lines (OpenChoreo's log query, from every pod of the feature's components)
since the step began and asks whether the "on" users fail more often, with a one-sided
two-proportion z-test. It checks every half minute, so the bar is high: enough requests on both
sides, z at least 3, and an increase that matters both absolutely and relatively
(platform/remediation.yaml `gradual_release.compare`). A regression stops the release: the tool
turns the flag off (when the policy lets it) and the release managers decide.

A feature with no such lines (UI only, or a component on an older template) falls back to time
alone, and the step's history says so.
"""

from __future__ import annotations

import json
import math
from datetime import datetime, timedelta, timezone
from typing import Callable

MARKER = "vrixel.flag_cohorts"
DEFAULTS = {"min_requests": 200, "z": 3.0, "min_increase": 0.005, "relative": 1.25}


def parse(lines, flags: list[str]) -> dict[str, dict]:
    """Sum the summary lines for these flags: {flag: {"on": {requests, errors}, "off": {...}, "lines": n}}."""
    out = {f: {"on": {"requests": 0, "errors": 0}, "off": {"requests": 0, "errors": 0}, "lines": 0} for f in flags}
    for line in lines or []:
        text = str(line)
        i = text.find(MARKER)
        if i < 0:
            continue
        try:
            rec = json.loads(text[i + len(MARKER):].strip())
        except ValueError:
            # A JSON log line whose message carries the summary, escaped.
            try:
                rec = json.loads(json.loads(text).get("message", "").split(MARKER, 1)[1].strip())
            except (ValueError, AttributeError, IndexError, TypeError):
                continue
        flag = rec.get("flag")
        if flag not in out:
            continue
        for side in ("on", "off"):
            s = rec.get(side) or {}
            try:
                out[flag][side]["requests"] += max(0, int(s.get("requests", 0)))
                out[flag][side]["errors"] += max(0, int(s.get("errors", 0)))
            except (TypeError, ValueError):
                continue
        out[flag]["lines"] += 1
    return out


def compare(counts: dict, cfg: dict | None = None) -> dict:
    """Whether users with the flag on fail more than users with it off.
    {"verdict": regression | ok | too_few, "on_rate", "off_rate", "z", ...}."""
    c = {**DEFAULTS, **(cfg or {})}
    n1, e1 = counts["on"]["requests"], min(counts["on"]["errors"], counts["on"]["requests"])
    n0, e0 = counts["off"]["requests"], min(counts["off"]["errors"], counts["off"]["requests"])
    base = {"on_requests": n1, "on_errors": e1, "off_requests": n0, "off_errors": e0}
    if n1 < c["min_requests"] or n0 < c["min_requests"]:
        return {**base, "verdict": "too_few", "on_rate": None, "off_rate": None, "z": None}
    p1, p0 = e1 / n1, e0 / n0
    p = (e1 + e0) / (n1 + n0)
    se = math.sqrt(p * (1 - p) * (1 / n1 + 1 / n0))
    z = (p1 - p0) / se if se > 0 else (math.inf if p1 > p0 else 0.0)
    worse = (z >= c["z"] and p1 - p0 >= c["min_increase"] and (p0 == 0 or p1 / p0 >= c["relative"]))
    return {**base, "verdict": "regression" if worse else "ok", "on_rate": round(p1, 5), "off_rate": round(p0, 5),
            "z": round(z, 2) if math.isfinite(z) else None}


def describe(flag: str, r: dict) -> str:
    if r["verdict"] == "too_few":
        return f"{flag}: too few requests to compare yet ({r['on_requests']} on, {r['off_requests']} off)"
    return (f"{flag}: {r['on_rate'] * 100:.2f}% of requests failed with it on ({r['on_errors']}/{r['on_requests']}) "
            f"against {r['off_rate'] * 100:.2f}% with it off ({r['off_errors']}/{r['off_requests']})"
            + (f", z {r['z']}" if r.get("z") is not None else ""))


class ObserverLogs:
    """Component logs from OpenChoreo's observer (POST /api/v1/logs/query), searched for the
    summary lines. Answers are capped at 1,000 entries, so a full answer splits the window."""

    def __init__(self, url: str, auth: Callable[[], dict] | None = None, opener=None) -> None:
        from urllib.request import build_opener

        self.url = url.rstrip("/")
        self.auth = auth or (lambda: {})
        self.opener = opener or build_opener()

    def _query(self, scope: dict, start: datetime, end: datetime) -> list[str]:
        from urllib.request import Request

        body = {"startTime": start.isoformat(), "endTime": end.isoformat(), "limit": 1000, "sortOrder": "asc",
                "searchScope": scope, "searchPhrase": MARKER}
        req = Request(f"{self.url}/api/v1/logs/query", data=json.dumps(body).encode(), method="POST",
                      headers={"Content-Type": "application/json", "Accept": "application/json", **self.auth()})
        with self.opener.open(req, timeout=20) as resp:
            got = json.loads(resp.read().decode("utf-8") or "{}")
        return [str(x.get("log", "")) for x in got.get("logs") or [] if isinstance(x, dict)]

    def lines(self, *, namespace: str, project: str, component: str, environment: str, since: datetime,
              until: datetime, depth: int = 0) -> list[str]:
        scope = {"namespace": namespace, "project": project, "component": component, "environment": environment}
        got = self._query(scope, since, until)
        if len(got) < 1000 or depth >= 5 or until - since < timedelta(minutes=2):
            return got
        mid = since + (until - since) / 2
        return (self.lines(namespace=namespace, project=project, component=component, environment=environment,
                           since=since, until=mid, depth=depth + 1)
                + self.lines(namespace=namespace, project=project, component=component, environment=environment,
                             since=mid, until=until, depth=depth + 1))


class StubLogs:
    """Tests: lines per component."""

    def __init__(self) -> None:
        self.by_component: dict[str, list[str]] = {}

    def lines(self, *, component: str, **_) -> list[str]:
        return list(self.by_component.get(component, []))


def line(flag: str, on: tuple[int, int], off: tuple[int, int]) -> str:
    return f'{MARKER} {json.dumps({"flag": flag, "on": {"requests": on[0], "errors": on[1]}, "off": {"requests": off[0], "errors": off[1]}})}'


def from_env(auth=None):
    import os

    url = os.environ.get("VRIXEL_OPENCHOREO_OBSERVER_URL", "")
    return ObserverLogs(url, auth) if url else None


def now() -> datetime:
    return datetime.now(timezone.utc)

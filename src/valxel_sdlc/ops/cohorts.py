"""Comparing users with a feature on against users with it off, during a gradual release.

Time alone (ops/rollout.py) can miss a feature that hurts only the users who have it: at 5%, its
errors are a small share of the component's. So the backend templates count, per flag, the
requests that evaluated it and how many of them failed (a 5xx or an unhandled exception), apart
for users who got it on and users who got it off, and log one line per flag a minute:

    valxel.flag_cohorts {"flag": "cln.cln-feat-3.kiosk", "on": {"requests": 40, "errors": 0},
                         "off": {"requests": 760, "errors": 2}}

The tool sums those lines (OpenChoreo's log query, across the product's whole project, so a
component that checks the flag counts even if the feature didn't change it) since the step began,
per component (and per app: the BFF relays the web and mobile apps' counts as source web and
mobile), and asks whether the "on" users fail more often in any of them, with a one-sided
two-proportion z-test. Comparing per component keeps a busy component that's fine from diluting
one that isn't. It checks every half minute, so the bar is high: enough requests on both
sides, z at least 3, and an increase that matters both absolutely and relatively
(platform/remediation.yaml `gradual_release.compare`). A regression stops the release: the tool
turns the flag off (when the policy lets it) and the release managers decide.

Latency counts the same way. Percentiles can't be summed across pods and minutes, but counts
can: each line also says how many requests were slow, slower than the component's latency budget
(500 ms by default, the API budget at the 95th percentile; `FLAG_COHORTS_SLOW_MS`). The "on" users
being slow more often, by the same test and bar, stops the release too. A share of slow requests
over 5% means the 95th percentile is over the budget; the step's history says so.

    valxel.flag_cohorts {"flag": "...", "on": {"requests": 40, "errors": 0, "slow": 3}, ...}

A feature with no such lines (UI only, or a component on an older template) falls back to time
alone, and the step's history says so.

The feature's goal events (outcome loop, step 4) ride on the same lines, per side, so the step can
also say whether users with it on do the things its goal counts more or less often. That is
reported on each step and never stops the release: a flat goal is a person's call.

    valxel.flag_cohorts {"flag": "...", "on": {"requests": 40, "errors": 0, "events": {"checkin_completed": 9}},
                         "off": {"requests": 760, "errors": 2, "events": {"checkin_completed": 120}}}
"""

from __future__ import annotations

import json
import math
from datetime import datetime, timedelta, timezone
from typing import Callable

MARKER = "valxel.flag_cohorts"
DEFAULTS = {"min_requests": 200, "z": 3.0, "min_increase": 0.005, "relative": 1.25}


def _record(text: str) -> dict | None:
    i = text.find(MARKER)
    if i < 0:
        return None
    try:
        return json.loads(text[i + len(MARKER):].strip())
    except ValueError:
        # A JSON log line whose message carries the summary, escaped.
        try:
            return json.loads(json.loads(text).get("message", "").split(MARKER, 1)[1].strip())
        except (ValueError, AttributeError, IndexError, TypeError):
            return None


def _empty() -> dict:
    return {"on": {"requests": 0, "errors": 0, "slow": 0, "events": {}},
            "off": {"requests": 0, "errors": 0, "slow": 0, "events": {}}, "lines": 0, "timed": 0}


def _add(into: dict, rec: dict) -> None:
    timed = all("slow" in (rec.get(side) or {}) for side in ("on", "off"))
    for side in ("on", "off"):
        s = rec.get(side) or {}
        try:
            into[side]["requests"] += max(0, int(s.get("requests", 0)))
            into[side]["errors"] += max(0, int(s.get("errors", 0)))
            into[side]["slow"] += max(0, int(s.get("slow", 0)))
        except (TypeError, ValueError):
            continue
        for name, n in (s.get("events") or {}).items() if isinstance(s.get("events"), dict) else ():
            if isinstance(n, (int, float)) and n >= 0:
                into[side]["events"][str(name)] = into[side]["events"].get(str(name), 0) + int(n)
    into["lines"] += 1
    into["timed"] += 1 if timed else 0


def parse(lines, flags: list[str]) -> dict[str, dict]:
    """Sum the summary lines for these flags: {flag: {"on": {requests, errors}, "off": {...}, "lines": n}}."""
    out = {f: _empty() for f in flags}
    for line in lines or []:
        rec = _record(str(line))
        if rec and rec.get("flag") in out:
            _add(out[rec["flag"]], rec)
    return out


def parse_entries(entries, flags: list[str]) -> dict[str, dict[str, dict]]:
    """Sum the summary lines per flag and per group: the component that logged them, or the
    line's `source` (the BFF relays the web and mobile apps' counts as source web or mobile).
    {flag: {group: counts}}. Comparing per group keeps a busy component that's fine from hiding
    one that isn't."""
    out: dict[str, dict[str, dict]] = {f: {} for f in flags}
    for component, line in entries or []:
        rec = _record(str(line))
        if not rec or rec.get("flag") not in out:
            continue
        group = str(rec.get("source") or component or "unknown")
        _add(out[rec["flag"]].setdefault(group, _empty()), rec)
    return out


def describe_groups(flag: str, groups: dict[str, dict]) -> str:
    """Every component's comparison for a flag, for a step's history."""
    return "; ".join(describe(flag, {**r, "group": r.get("group") or g}) for g, r in sorted(groups.items()))


def _ztest(e1: int, n1: int, e0: int, n0: int, c: dict) -> tuple[float, float, float | None, bool]:
    """A one-sided two-proportion z-test: the "on" share against the "off" share, and whether it is
    worse by the policy's bar (z, an absolute and a relative increase)."""
    p1, p0 = e1 / n1, e0 / n0
    p = (e1 + e0) / (n1 + n0)
    se = math.sqrt(p * (1 - p) * (1 / n1 + 1 / n0))
    z = (p1 - p0) / se if se > 0 else (math.inf if p1 > p0 else 0.0)
    worse = z >= c["z"] and p1 - p0 >= c["min_increase"] and (p0 == 0 or p1 / p0 >= c["relative"])
    return p1, p0, (round(z, 2) if math.isfinite(z) else None), worse


SLOW_SHARE_P95 = 0.05  # more than 5% of requests over the latency budget: the 95th percentile is over it


def compare(counts: dict, cfg: dict | None = None) -> dict:
    """Whether users with the flag on fail more, or are slow more often, than users with it off.
    {"verdict": regression | ok | too_few, "on_rate", "off_rate", "z", "worse": [errors, latency],
    "slow": {...} when the lines count slow requests}."""
    c = {**DEFAULTS, **(cfg or {})}
    n1, e1 = counts["on"]["requests"], min(counts["on"]["errors"], counts["on"]["requests"])
    n0, e0 = counts["off"]["requests"], min(counts["off"]["errors"], counts["off"]["requests"])
    base = {"on_requests": n1, "on_errors": e1, "off_requests": n0, "off_errors": e0}
    if n1 < c["min_requests"] or n0 < c["min_requests"]:
        return {**base, "verdict": "too_few", "on_rate": None, "off_rate": None, "z": None, "worse": []}
    p1, p0, z, errors_worse = _ztest(e1, n1, e0, n0, c)
    out = {**base, "on_rate": round(p1, 5), "off_rate": round(p0, 5), "z": z,
           "worse": ["errors"] if errors_worse else []}
    # Latency, only when every line counted slow requests (a mix of old and new pods would undercount).
    if counts.get("timed") and counts.get("timed") == counts.get("lines"):
        s1, s0 = min(counts["on"].get("slow", 0), n1), min(counts["off"].get("slow", 0), n0)
        q1, q0, zs, slow_worse = _ztest(s1, n1, s0, n0, c)
        out["slow"] = {"on_slow": s1, "off_slow": s0, "on_rate": round(q1, 5), "off_rate": round(q0, 5), "z": zs,
                       "on_p95_over_budget": q1 > SLOW_SHARE_P95, "off_p95_over_budget": q0 > SLOW_SHARE_P95}
        if slow_worse:
            out["worse"].append("latency")
    out["verdict"] = "regression" if out["worse"] else "ok"
    return out


GOAL_DEFAULTS = {"min_requests": 200, "z": 2.0}


def goal_events(groups: dict[str, dict], events: list[str], cfg: dict | None = None) -> dict[str, dict]:
    """Per goal event, across every component: how often it happens per request for users with the
    flag on against users with it off. {event: {verdict: higher | lower | no_difference | too_few,
    on_rate, off_rate, z, on_events, off_events, on_requests, off_requests}}. Events are counts, so
    the test compares two Poisson rates (z of the difference over its standard error)."""
    c = {**GOAL_DEFAULTS, **(cfg or {})}
    n1 = sum(g["on"]["requests"] for g in groups.values())
    n0 = sum(g["off"]["requests"] for g in groups.values())
    out = {}
    for e in events:
        k1 = sum(g["on"].get("events", {}).get(e, 0) for g in groups.values())
        k0 = sum(g["off"].get("events", {}).get(e, 0) for g in groups.values())
        base = {"on_events": k1, "off_events": k0, "on_requests": n1, "off_requests": n0}
        if n1 < c["min_requests"] or n0 < c["min_requests"] or k1 + k0 == 0:
            out[e] = {**base, "verdict": "too_few", "on_rate": None, "off_rate": None, "z": None}
            continue
        r1, r0 = k1 / n1, k0 / n0
        se = math.sqrt((k1 + k0) / (n1 + n0) * (1 / n1 + 1 / n0))
        z = (r1 - r0) / se if se > 0 else 0.0
        verdict = "higher" if z >= c["z"] else "lower" if z <= -c["z"] else "no_difference"
        out[e] = {**base, "verdict": verdict, "on_rate": round(r1, 5), "off_rate": round(r0, 5), "z": round(z, 2)}
    return out


def describe_goal(r: dict[str, dict]) -> str:
    """The goal events' comparison, for a step's history."""
    parts = []
    for e, x in sorted(r.items()):
        if x["verdict"] == "too_few":
            parts.append(f"{e}: too few to compare yet ({x['on_events']} with it on, {x['off_events']} off)")
            continue
        word = {"higher": "more often", "lower": "less often", "no_difference": "about as often"}[x["verdict"]]
        parts.append(f"{e}: {x['on_rate'] * 100:.2f} per 100 requests with it on against {x['off_rate'] * 100:.2f} "
                     f"with it off ({word}, z {x['z']})")
    return "; ".join(parts)


def describe(flag: str, r: dict) -> str:
    where = f" in {r['group']}" if r.get("group") else ""
    if r["verdict"] == "too_few":
        return f"{flag}{where}: too few requests to compare yet ({r['on_requests']} on, {r['off_requests']} off)"
    text = (f"{flag}{where}: {r['on_rate'] * 100:.2f}% of requests failed with it on ({r['on_errors']}/{r['on_requests']}) "
            f"against {r['off_rate'] * 100:.2f}% with it off ({r['off_errors']}/{r['off_requests']})"
            + (f", z {r['z']}" if r.get("z") is not None else ""))
    s = r.get("slow")
    if s:
        text += (f"; {s['on_rate'] * 100:.1f}% were slow with it on against {s['off_rate'] * 100:.1f}% with it off"
                 + (f", z {s['z']}" if s.get("z") is not None else "")
                 + (" (its 95th percentile is over the latency budget)" if s["on_p95_over_budget"] else ""))
    if r.get("worse") == ["latency"]:
        text += ": slower with it on"
    return text


class ObserverLogs:
    """Component logs from OpenChoreo's observer (POST /api/v1/logs/query), searched for the
    summary lines. Answers are capped at 1,000 entries, so a full answer splits the window."""

    def __init__(self, url: str, auth: Callable[[], dict] | None = None, opener=None) -> None:
        from urllib.request import build_opener

        self.url = url.rstrip("/")
        self.auth = auth or (lambda: {})
        self.opener = opener or build_opener()

    def _query(self, scope: dict, start: datetime, end: datetime, phrase: str = MARKER) -> list[tuple[str, str]]:
        from urllib.request import Request

        body = {"startTime": start.isoformat(), "endTime": end.isoformat(), "limit": 1000, "sortOrder": "asc",
                "searchScope": scope, "searchPhrase": phrase}
        req = Request(f"{self.url}/api/v1/logs/query", data=json.dumps(body).encode(), method="POST",
                      headers={"Content-Type": "application/json", "Accept": "application/json", **self.auth()})
        with self.opener.open(req, timeout=20) as resp:
            got = json.loads(resp.read().decode("utf-8") or "{}")
        return [(str((x.get("metadata") or {}).get("componentName") or ""), str(x.get("log", "")))
                for x in got.get("logs") or [] if isinstance(x, dict)]

    def entries(self, *, namespace: str, project: str, environment: str, since: datetime, until: datetime,
                depth: int = 0, phrase: str = MARKER) -> list[tuple[str, str]]:
        """Every summary line in the project's environment, from any component: [(component, line)].
        `phrase` picks the kind: flag cohorts (the default) or product events (outcomes.instrumentation).
        Watching the whole project catches a component that checks the flag though the feature
        didn't change it."""
        scope = {"namespace": namespace, "project": project, "environment": environment}
        got = self._query(scope, since, until, phrase)
        if len(got) < 1000 or depth >= 5 or until - since < timedelta(minutes=2):
            return got
        mid = since + (until - since) / 2
        return (self.entries(namespace=namespace, project=project, environment=environment, since=since, until=mid,
                             depth=depth + 1, phrase=phrase)
                + self.entries(namespace=namespace, project=project, environment=environment, since=mid, until=until,
                               depth=depth + 1, phrase=phrase))


class StubLogs:
    """Tests: lines per component."""

    def __init__(self) -> None:
        self.by_component: dict[str, list[str]] = {}

    def entries(self, **_) -> list[tuple[str, str]]:
        return [(c, line) for c, lines in self.by_component.items() for line in lines]


def line(flag: str, on: tuple, off: tuple, source: str = "") -> str:
    """A summary line for tests: on and off as (requests, errors) or (requests, errors, slow)."""
    def side(v: tuple) -> dict:
        return {"requests": v[0], "errors": v[1], **({"slow": v[2]} if len(v) > 2 else {})}

    rec = {"flag": flag, "on": side(on), "off": side(off)}
    return f"{MARKER} {json.dumps({**rec, **({'source': source} if source else {})})}"


def from_env(auth=None):
    import os

    url = os.environ.get("VALXEL_OPENCHOREO_OBSERVER_URL", "")
    return ObserverLogs(url, auth) if url else None


def now() -> datetime:
    return datetime.now(timezone.utc)

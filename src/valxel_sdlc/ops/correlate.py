"""Which signals are one incident.

A failure people see in the app and the backend error behind it arrive as different signals (a
Sentry issue from the web app, an OpenChoreo alert or a Sentry issue from a service), from different
components. Before opening an incident for a new signal, the tool looks for an open incident in the
same product that it belongs to, strongest evidence first:

    trace     the signal names a trace (`trace_id` from the apps' and services' error reports, or
              trace ids read from the error logs behind an OpenChoreo alert) that a signal of the
              incident names too: the same request failed on both sides
    calls     within the window, one component calls the other, directly or through others (the
              product's call graph: its consumer repos call its provider repos, plus runtime.calls)
    release   within the window, the same feature was released to both components shortly before

A match joins the incident, saying why. The root cause candidate is the furthest-downstream
component among the incident's signals: the others are where the failure showed. On-call can split
a signal that was joined wrongly, and merge two incidents that are one.
"""

from __future__ import annotations

import json
import re
from collections import deque
from datetime import datetime, timedelta, timezone

WINDOW = timedelta(minutes=30)
RELEASE_WINDOW = timedelta(hours=2)
STRENGTH = {"trace": 0, "calls": 1, "release": 2}
_TRACE = re.compile(r"\b[0-9a-f]{32}\b")


def short(component: str) -> str:
    return (component or "").rsplit("/", 1)[-1]


def _at(value) -> datetime | None:
    try:
        t = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def trace_ids(signal: dict) -> set[str]:
    """The traces a signal names: a `trace_id` tag, or `trace_ids` read from logs."""
    labels = signal.get("labels") or {}
    out = set()
    for key in ("trace_id", "trace_ids"):
        out |= set(_TRACE.findall(str(labels.get(key) or "").lower()))
    return out


def traces_in_logs(lines: list[str], limit: int = 20) -> list[str]:
    """Trace ids of the error lines in a component's recent logs (JSON lines with trace.id)."""
    out: list[str] = []
    for line in lines or []:
        if "stack_trace" not in line and '"ERROR"' not in line:
            continue
        try:
            rec = json.loads(line)
            tid = rec.get("trace.id") or (rec.get("trace") or {}).get("id")
        except (ValueError, AttributeError):
            m = re.search(r'"trace\.id"\s*:\s*"([0-9a-f]{32})"', line)
            tid = m.group(1) if m else None
        if tid and tid not in out:
            out.append(tid)
        if len(out) >= limit:
            break
    return out


class CallGraph:
    """Which components call which, by component name (the repo's name)."""

    def __init__(self, edges=()) -> None:
        self.edges: set[tuple[str, str]] = {(short(a), short(b)) for a, b in edges if short(a) != short(b)}

    def downstream(self, component: str) -> set[str]:
        """Everything the component calls, directly or through others."""
        seen, todo = set(), deque([short(component)])
        while todo:
            c = todo.popleft()
            for a, b in self.edges:
                if a == c and b not in seen:
                    seen.add(b)
                    todo.append(b)
        return seen

    def related(self, a: str, b: str) -> bool:
        a, b = short(a), short(b)
        return bool(a and b and a != b and (b in self.downstream(a) or a in self.downstream(b)))

    def root(self, components) -> str:
        """The furthest-downstream component: nothing else among them is downstream of it."""
        comps = [short(c) for c in components if short(c)]
        for c in dict.fromkeys(comps):
            if not (self.downstream(c) & set(comps) - {c}):
                calls_it = sum(1 for o in comps if o != c and c in self.downstream(o))
                if calls_it or len(set(comps)) == 1:
                    return c
        return comps[0] if comps else ""


def graph_for(snap) -> CallGraph:
    """The product's call graph: each consumer repo calls each provider repo (the contracts say who
    calls whom), across the product and the lanes it builds on, plus `runtime.calls`
    ({caller: [callee, ...]}) in any lane's product.yaml."""
    lanes = list(snap.lanes().values()) if hasattr(snap, "lanes") else [snap.product]
    repos = [(r.name, getattr(r, "role", "")) for lane in lanes for r in lane.repos]
    consumers = [n for n, role in repos if role == "consumer"]
    providers = [n for n, role in repos if role == "provider"]
    edges = [(c, p) for c in consumers for p in providers]
    for lane in lanes:
        for caller, callees in ((lane.runtime or {}).get("calls") or {}).items():
            edges += [(caller, callee) for callee in (callees or [])]
    return CallGraph(edges)


def related(engine, product_id: str, signal: dict) -> list[tuple[str, str]]:
    """Open incidents of the product this signal belongs to, strongest evidence first:
    [(incident id, why)]."""
    store = engine.services.signals
    lister = getattr(store, "open_incidents", None)
    if not callable(lister):
        return []
    at = _at(signal.get("at")) or datetime.now(timezone.utc)
    mine = trace_ids(signal)
    graph = None
    found: list[tuple[int, str, str]] = []
    for iid in lister(product_id):
        sigs = [s for s in store.for_incident(iid) if s.get("category") != "cost"]
        if not sigs:
            continue
        theirs = set().union(*(trace_ids(s) for s in sigs))
        shared = sorted(mine & theirs)
        if shared:
            found.append((STRENGTH["trace"], iid, f"trace: the same request ({shared[0]}) failed in both"))
            continue
        times = [t for t in (_at(s.get("at")) for s in sigs) if t]
        if not times or abs(at - max(times)) > WINDOW:
            continue
        comp = signal.get("component") or ""
        comps = {s.get("component") or "" for s in sigs} - {""}
        if comp and comps:
            graph = graph or graph_for(engine.services.snapshot(product_id))
            other = next((c for c in sorted(comps) if graph.related(comp, c)), "")
            if other:
                down = other if short(other) in graph.downstream(comp) else comp
                up = comp if down == other else other
                found.append((STRENGTH["calls"], iid,
                              f"calls: {short(up)} calls {short(down)}, and both failed within "
                              f"{int(WINDOW.total_seconds() // 60)} minutes"))
                continue
        released = _released_to(engine, product_id, comp, at)
        if released:
            for c in comps:
                common = released & _released_to(engine, product_id, c, at)
                if common:
                    found.append((STRENGTH["release"], iid,
                                  f"release: {sorted(common)[0]} went out to both shortly before"))
                    break
    found.sort(key=lambda f: f[0])
    return [(iid, why) for _, iid, why in dict.fromkeys(found)]


def _released_to(engine, product_id: str, component: str, at: datetime) -> set[str]:
    if not component:
        return set()
    out = set()
    for s in engine.services.suspects(product_id, component) or []:
        t = _at(s.get("released_at"))
        if t and timedelta(0) <= at - t <= RELEASE_WINDOW:
            out.add(s["feature_id"])
    return out


def root(snap, signals: list[dict], fallback: str = "") -> tuple[str, str]:
    """The root cause candidate among an incident's signals and why: (repo or component, why)."""
    comps = [s.get("component") or "" for s in signals if s.get("category") != "cost"]
    comps = [c for c in comps if c]
    if not comps:
        return fallback, ""
    graph = graph_for(snap)
    r = graph.root(comps)
    if not r:
        return fallback, ""
    full = next((c for c in comps if short(c) == r), r)
    upstream = sorted({short(c) for c in comps if short(c) != r and r in graph.downstream(c)})
    return full, (f"{r} is downstream of {', '.join(upstream)}: the others are where it showed" if upstream else "")

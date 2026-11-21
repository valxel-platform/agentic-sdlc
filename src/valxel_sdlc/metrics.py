"""Valxel's own health in numbers, and alerts on them (T-118).

Each process counts what it serves and runs:

    valxel_http_requests_total{method,route,code}    API requests (route templates, never ids)
    valxel_http_request_seconds{route}               how long they took (a histogram)
    valxel_agent_sessions_total{role,outcome}        agent sessions: success, failure, unavailable
                                                     (the model provider or gateway refused)
    valxel_agent_session_seconds{role}               how long sessions took

and, read when scraped (GET /metrics, with VALXEL_METRICS_TOKEN as a bearer token):

    valxel_queue_jobs{state}, valxel_queue_oldest_seconds   the run queue
    valxel_audit_backlog                                    audit records not yet sent to the sink
    valxel_watcher_job_failing{job}                         housekeeping jobs failing now

Every replica publishes its counters to the cluster's shared notes each minute, so the watcher's
`check_tool_metrics` job sees the whole deployment, compares this five minutes with the last
check, and alerts (the watcher's webhook, once an hour per alert while it lasts) when:

    the run queue's oldest job has waited longer than VALXEL_ALERT_QUEUE_WAIT_S (900)
    more than VALXEL_ALERT_5XX_RATIO (0.05) of at least 20 API requests failed with a 5xx
    more than VALXEL_ALERT_SESSION_FAILURE_RATIO (0.3) of at least 5 agent sessions failed
    the audit backlog is over VALXEL_ALERT_AUDIT_BACKLOG (1000)
"""

from __future__ import annotations

import os
import socket
import threading
import time
from collections import defaultdict
from typing import Any

BUCKETS = (0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 300, 900, 1800, 3600)
REPLICA = f"{socket.gethostname()}:{os.getpid()}"


class Registry:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.counters: dict[str, dict[tuple, float]] = defaultdict(lambda: defaultdict(float))
        self.hists: dict[str, dict[tuple, list[float]]] = defaultdict(dict)  # buckets..., +Inf, sum

    def inc(self, name: str, labels: dict, by: float = 1.0) -> None:
        key = tuple(sorted(labels.items()))
        with self._lock:
            self.counters[name][key] += by

    def observe(self, name: str, labels: dict, value: float) -> None:
        key = tuple(sorted(labels.items()))
        with self._lock:
            h = self.hists[name].setdefault(key, [0.0] * (len(BUCKETS) + 2))
            for i, b in enumerate(BUCKETS):
                if value <= b:
                    h[i] += 1
            h[len(BUCKETS)] += 1
            h[-1] += value

    def snapshot(self) -> dict:
        """Counters as {name: [[labels, value], ...]}: what each replica publishes."""
        with self._lock:
            return {n: [[dict(k), v] for k, v in rows.items()] for n, rows in self.counters.items()}

    def text(self, gauges: dict[str, list[tuple[dict, float]]] | None = None) -> str:
        out: list[str] = []
        with self._lock:
            for name, rows in sorted(self.counters.items()):
                out.append(f"# TYPE {name} counter")
                out += [f"{name}{_labels(dict(k))} {v:g}" for k, v in sorted(rows.items())]
            for name, rows in sorted(self.hists.items()):
                out.append(f"# TYPE {name} histogram")
                for k, h in sorted(rows.items()):
                    lab = dict(k)
                    out += [f"{name}_bucket{_labels({**lab, 'le': f'{b:g}'})} {h[i]:g}" for i, b in enumerate(BUCKETS)]
                    out.append(f"{name}_bucket{_labels({**lab, 'le': '+Inf'})} {h[len(BUCKETS)]:g}")
                    out.append(f"{name}_sum{_labels(lab)} {h[-1]:g}")
                    out.append(f"{name}_count{_labels(lab)} {h[len(BUCKETS)]:g}")
        for name, rows in sorted((gauges or {}).items()):
            out.append(f"# TYPE {name} gauge")
            out += [f"{name}{_labels(lab)} {v:g}" for lab, v in rows]
        return "\n".join(out) + "\n"


def _labels(lab: dict) -> str:
    if not lab:
        return ""
    esc = {k: str(v).replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ") for k, v in lab.items()}
    return "{" + ",".join(f'{k}="{v}"' for k, v in sorted(esc.items())) + "}"


REGISTRY = Registry()


def request(method: str, route: str, status: int, seconds: float) -> None:
    REGISTRY.inc("valxel_http_requests_total", {"method": method, "route": route, "code": f"{status // 100}xx"})
    REGISTRY.observe("valxel_http_request_seconds", {"route": route}, seconds)


def session(role: str, outcome: str, seconds: float) -> None:
    REGISTRY.inc("valxel_agent_sessions_total", {"role": role, "outcome": outcome})
    REGISTRY.observe("valxel_agent_session_seconds", {"role": role}, seconds)


class MeteredRuntime:
    """Counts each agent session and how it ended."""

    def __init__(self, inner) -> None:
        self.inner = inner
        self.name = inner.name
        self.capabilities = getattr(inner, "capabilities", None)

    def __getattr__(self, item):
        return getattr(self.inner, item)

    def run(self, task):
        from .gateway import ProviderUnavailable

        started = time.monotonic()
        try:
            res = self.inner.run(task)
        except ProviderUnavailable:
            session(task.role, "unavailable", time.monotonic() - started)
            raise
        except Exception:
            session(task.role, "failure", time.monotonic() - started)
            raise
        session(task.role, "success", time.monotonic() - started)
        return res


# --- what the whole deployment looks like, and alerts --------------------------------------------

def publish(cluster, clock=time.time) -> None:
    """This replica's counters, for the watcher (each replica, every minute), and its name in the
    index of replicas. Two replicas writing the index at once can drop one: it is back a minute later."""
    now = clock()
    cluster.put(f"metrics:{REPLICA}", {"at": now, "counters": REGISTRY.snapshot()})
    index = {k: v for k, v in (cluster.get("metrics:replicas") or {}).items() if now - float(v) < 3600}
    index[REPLICA] = now
    cluster.put("metrics:replicas", index)


def _sum(snapshots: list[dict], name: str, where) -> float:
    return sum(v for s in snapshots for lab, v in (s.get("counters") or {}).get(name, []) if where(lab))


def totals(snapshots: list[dict]) -> dict:
    return {
        "requests": _sum(snapshots, "valxel_http_requests_total", lambda lab: True),
        "errors": _sum(snapshots, "valxel_http_requests_total", lambda lab: lab.get("code") == "5xx"),
        "sessions": _sum(snapshots, "valxel_agent_sessions_total", lambda lab: True),
        "failed": _sum(snapshots, "valxel_agent_sessions_total", lambda lab: lab.get("outcome") != "success"),
    }


def _f(env, k, d) -> float:
    try:
        return float(env.get(k) or d)
    except ValueError:
        return float(d)


def findings(now: dict, before: dict, queue: dict | None, backlog: int, env=None) -> list[str]:
    """What's wrong since the last check: plain sentences, one per alert."""
    env = os.environ if env is None else env
    d = {k: max(0.0, now.get(k, 0) - before.get(k, 0)) for k in ("requests", "errors", "sessions", "failed")}
    out = []
    wait = _f(env, "VALXEL_ALERT_QUEUE_WAIT_S", 900)
    if queue and queue.get("queued") and float(queue.get("oldest_s") or 0) > wait:
        out.append(f"queue: the oldest of {queue['queued']} queued runs has waited {queue['oldest_s'] / 60:.0f} "
                   f"minutes (alert above {wait / 60:.0f}); are the workers up?")
    ratio = _f(env, "VALXEL_ALERT_5XX_RATIO", 0.05)
    if d["requests"] >= 20 and d["errors"] / d["requests"] > ratio:
        out.append(f"api: {d['errors']:.0f} of {d['requests']:.0f} requests failed with a server error "
                   f"(alert above {ratio:.0%})")
    sratio = _f(env, "VALXEL_ALERT_SESSION_FAILURE_RATIO", 0.3)
    if d["sessions"] >= 5 and d["failed"] / d["sessions"] > sratio:
        out.append(f"sessions: {d['failed']:.0f} of {d['sessions']:.0f} agent sessions failed or found the model "
                   f"provider unavailable (alert above {sratio:.0%})")
    most = _f(env, "VALXEL_ALERT_AUDIT_BACKLOG", 1000)
    if backlog > most:
        out.append(f"audit: {backlog} records are waiting to reach the audit sink (alert above {most:.0f})")
    return out


class Checker:
    """The watcher's check_tool_metrics job: totals across replicas against the last check."""

    REALERT_S = 3600.0

    def __init__(self, engine: Any, alert=None, clock=time.time) -> None:
        """alert: where to say it (the engine passes the watcher's webhook)."""
        self.engine, self.clock = engine, clock
        self.alert = alert
        self.before: dict | None = None
        self.said: dict[str, float] = {}

    def snapshots(self) -> list[dict]:
        cluster = self.engine.cluster
        fresh = self.clock() - 600  # a replica gone quiet for ten minutes no longer counts
        out = []
        for replica in cluster.get("metrics:replicas") or {}:
            v = cluster.get(f"metrics:{replica}")
            if isinstance(v, dict) and float(v.get("at") or 0) > fresh:
                out.append(v)
        return out

    def run(self) -> list[str]:
        now = totals(self.snapshots())
        queue = getattr(self.engine, "queue", None)
        depth = queue.depth() if queue is not None else None
        audit = getattr(self.engine.services, "audit", None)
        backlog = audit.backlog() if audit is not None else 0
        found = findings(now, self.before if self.before is not None else now, depth, backlog)
        self.before = now
        alert = self.alert or (lambda text: None)
        said = []
        for f in found:
            kind = f.split(":", 1)[0]
            last = self.said.get(kind)
            if last is None or self.clock() - last >= self.REALERT_S:
                self.said[kind] = self.clock()
                alert(f"Valxel health: {f}")
                said.append(f)
        return said


def gauges(engine) -> dict[str, list[tuple[dict, float]]]:
    out: dict[str, list[tuple[dict, float]]] = {}
    queue = getattr(engine, "queue", None)
    if queue is not None:
        try:
            d = queue.depth()
            out["valxel_queue_jobs"] = [({"state": "queued"}, d["queued"]), ({"state": "running"}, d["running"])]
            out["valxel_queue_oldest_seconds"] = [({}, d["oldest_s"])]
        except Exception:  # noqa: BLE001 - a scrape shows what it can
            pass
    audit = getattr(engine.services, "audit", None)
    if audit is not None:
        try:
            out["valxel_audit_backlog"] = [({}, float(audit.backlog()))]
        except Exception:  # noqa: BLE001
            pass
    try:
        failing = [(j.name, engine.jobs.health(j.name)) for j in engine.watcher_jobs()]
        out["valxel_watcher_job_failing"] = [({"job": n}, 1.0 if h.get("failures") else 0.0) for n, h in failing]
    except Exception:  # noqa: BLE001
        pass
    return out

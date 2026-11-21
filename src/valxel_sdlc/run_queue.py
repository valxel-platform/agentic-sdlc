"""The run queue: API processes queue the work, worker processes run it.

Without a queue (VALXEL_RUN_QUEUE unset, or no database) a process runs what it is asked in a
thread pool of its own, as it always has. With VALXEL_RUN_QUEUE=postgres:

- **The API queues.** Starting a feature, a gate decision, a resume, a wait that ended: each is a
  row in `valxel_run_queue` (the thread, its workspace, and what to resume it with). The API process
  answers at once; the feature page shows it running.
- **Workers run.** `valxel worker` claims rows (`for update skip locked`, oldest first, and never a
  thread's later row before its earlier one), runs each thread in its own workspace's scope under
  the thread's advisory lock (cluster.py) as before, and deletes the row when the run ends, however
  it ends: a failed run's error is on the feature page and the reaper takes it from there
  (engine_parts/recovery.py). The watcher runs in workers too.
- **Leases.** A claimed row is the worker's while it keeps renewing the claim. A worker that dies
  stops renewing; the row is claimed again once the claim runs out (VALXEL_RUN_QUEUE_LEASE_S, default
  120), at most VALXEL_RUN_QUEUE_MAX_ATTEMPTS times (default 3), then dropped with an error note.
- **One thread at a time.** A row whose thread another worker is running waits a few seconds and
  is tried again.

Rows carry their workspace, so row-level security applies to them (tenancy.BY_WORKSPACE): a person's
request queues only work in its own workspace; workers claim as the platform.
"""

from __future__ import annotations

import logging
import os
import socket
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from .telemetry import current_traceparent, joined  # noqa: E402

log = logging.getLogger(__name__)

SCHEMA = (
    """create table if not exists valxel_run_queue (
         id bigserial primary key,
         thread_id text not null,
         workspace text not null default '',
         payload_type text not null,
         payload bytea not null,
         enqueued_at timestamptz not null default now(),
         available_at timestamptz not null default now(),
         claimed_by text,
         claimed_until timestamptz,
         attempts integer not null default 0)""",
    "create index if not exists valxel_run_queue_thread on valxel_run_queue (thread_id, id)",
    # The trace of the request that queued the run, so the run's agent sessions join it.
    "alter table valxel_run_queue add column if not exists traceparent text not null default ''",
)

_CLAIM = """
update valxel_run_queue set claimed_by = %(me)s, claimed_until = now() + make_interval(secs => %(lease)s),
       attempts = attempts + 1
 where id = (select q.id from valxel_run_queue q
              where q.available_at <= now() and (q.claimed_until is null or q.claimed_until < now())
                and not exists (select 1 from valxel_run_queue o where o.thread_id = q.thread_id and o.id < q.id)
              order by q.id
              for update skip locked
              limit 1)
returning id, thread_id, workspace, payload_type, payload, attempts, traceparent"""
_RENEW = ("update valxel_run_queue set claimed_until = now() + make_interval(secs => %(lease)s) "
          "where id = any(%(ids)s) and claimed_by = %(me)s")
_DONE = "delete from valxel_run_queue where id = %(id)s and claimed_by = %(me)s"
_LATER = ("update valxel_run_queue set claimed_by = null, claimed_until = null, attempts = attempts - 1, "
          "available_at = now() + make_interval(secs => %(delay)s) where id = %(id)s and claimed_by = %(me)s")
_QUEUED = "select distinct thread_id from valxel_run_queue where thread_id = any(%(ids)s)"
_ENQUEUE = ("insert into valxel_run_queue (thread_id, workspace, payload_type, payload, traceparent) "
            "values (%(thread)s, %(workspace)s, %(type)s, %(data)s, %(traceparent)s) returning id")
_DEPTH = ("select count(*) as queued, count(*) filter (where claimed_until > now()) as running, "
          "coalesce(extract(epoch from now() - min(enqueued_at)), 0) as oldest_s from valxel_run_queue")


# --------------------------------------------------------------------------- payloads

def encode(payload: Any) -> tuple[str, bytes]:
    """What a thread is run with: nothing (carry on from the checkpoint), a first state, or a
    Command resuming an interrupt. A Command is stored as its fields, never as an object to import."""
    from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
    from langgraph.types import Command

    if isinstance(payload, Command):
        payload = {"__command__": {"resume": payload.resume, "update": payload.update, "goto": payload.goto or None}}
    return JsonPlusSerializer().dumps_typed(payload)


def decode(kind: str, data: bytes) -> Any:
    from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
    from langgraph.types import Command

    value = JsonPlusSerializer().loads_typed((kind, bytes(data)))
    if isinstance(value, dict) and set(value) == {"__command__"}:
        c = value["__command__"]
        return Command(**{k: v for k, v in c.items() if v is not None})
    return value


# --------------------------------------------------------------------------- the queue

@dataclass
class Job:
    id: int
    thread_id: str
    workspace: str
    payload: Any
    attempts: int
    traceparent: str = ""  # the trace of the request that queued it


class RunQueue(Protocol):
    def enqueue(self, thread_id: str, payload: Any, workspace: str = "") -> int: ...
    def claim(self, me: str, lease_s: float) -> Job | None: ...
    def renew(self, me: str, ids: list[int], lease_s: float) -> None: ...
    def done(self, me: str, job_id: int) -> None: ...
    def later(self, me: str, job_id: int, delay_s: float) -> None: ...
    def queued_among(self, threads: list[str]) -> set[str]: ...
    def depth(self) -> dict: ...


class PostgresRunQueue:
    def __init__(self, pool: Any) -> None:
        self.pool = pool
        with pool.connection() as conn:
            for statement in SCHEMA:
                conn.execute(statement)

    def enqueue(self, thread_id: str, payload: Any, workspace: str = "") -> int:
        kind, data = encode(payload)
        with self.pool.connection() as conn:
            row = conn.execute(_ENQUEUE, {"thread": thread_id, "workspace": workspace, "type": kind,
                                          "data": data, "traceparent": current_traceparent()}).fetchone()
        return int(_get(row, "id", 0))

    def claim(self, me: str, lease_s: float) -> Job | None:
        with self.pool.connection() as conn:
            row = conn.execute(_CLAIM, {"me": me, "lease": float(lease_s)}).fetchone()
        if row is None:
            return None
        return Job(id=int(_get(row, "id", 0)), thread_id=_get(row, "thread_id", 1), workspace=_get(row, "workspace", 2),
                   payload=decode(_get(row, "payload_type", 3), _get(row, "payload", 4)),
                   attempts=int(_get(row, "attempts", 5)), traceparent=_get(row, "traceparent", 6) or "")

    def renew(self, me: str, ids: list[int], lease_s: float) -> None:
        if ids:
            with self.pool.connection() as conn:
                conn.execute(_RENEW, {"me": me, "ids": list(ids), "lease": float(lease_s)})

    def done(self, me: str, job_id: int) -> None:
        with self.pool.connection() as conn:
            conn.execute(_DONE, {"me": me, "id": job_id})

    def later(self, me: str, job_id: int, delay_s: float) -> None:
        with self.pool.connection() as conn:
            conn.execute(_LATER, {"me": me, "id": job_id, "delay": float(delay_s)})

    def queued_among(self, threads: list[str]) -> set[str]:
        if not threads:
            return set()
        with self.pool.connection() as conn:
            return {_get(r, "thread_id", 0) for r in conn.execute(_QUEUED, {"ids": list(threads)}).fetchall()}

    def depth(self) -> dict:
        with self.pool.connection() as conn:
            row = conn.execute(_DEPTH).fetchone()
        return {"queued": int(_get(row, "queued", 0)), "running": int(_get(row, "running", 1)),
                "oldest_s": float(_get(row, "oldest_s", 2))}


class MemoryRunQueue:
    """The same rules in memory, for tests and a single process."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self.clock = clock
        self.rows: dict[int, dict] = {}
        self._next = 0
        self._lock = threading.Lock()

    def enqueue(self, thread_id: str, payload: Any, workspace: str = "") -> int:
        kind, data = encode(payload)
        with self._lock:
            self._next += 1
            self.rows[self._next] = {"thread_id": thread_id, "workspace": workspace, "type": kind, "data": data,
                                     "available": self.clock(), "by": None, "until": 0.0, "attempts": 0,
                                     "at": self.clock(), "traceparent": current_traceparent()}
            return self._next

    def claim(self, me: str, lease_s: float) -> Job | None:
        now = self.clock()
        with self._lock:
            for i in sorted(self.rows):
                r = self.rows[i]
                earlier = any(j < i and o["thread_id"] == r["thread_id"] for j, o in self.rows.items())
                if r["available"] <= now and (r["by"] is None or r["until"] < now) and not earlier:
                    r.update(by=me, until=now + lease_s, attempts=r["attempts"] + 1)
                    return Job(i, r["thread_id"], r["workspace"], decode(r["type"], r["data"]), r["attempts"],
                               r["traceparent"])
        return None

    def renew(self, me: str, ids: list[int], lease_s: float) -> None:
        with self._lock:
            for i in ids:
                if i in self.rows and self.rows[i]["by"] == me:
                    self.rows[i]["until"] = self.clock() + lease_s

    def done(self, me: str, job_id: int) -> None:
        with self._lock:
            if self.rows.get(job_id, {}).get("by") == me:
                del self.rows[job_id]

    def later(self, me: str, job_id: int, delay_s: float) -> None:
        with self._lock:
            r = self.rows.get(job_id)
            if r and r["by"] == me:
                r.update(by=None, until=0.0, attempts=r["attempts"] - 1, available=self.clock() + delay_s)

    def queued_among(self, threads: list[str]) -> set[str]:
        with self._lock:
            return {r["thread_id"] for r in self.rows.values() if r["thread_id"] in set(threads)}

    def depth(self) -> dict:
        now = self.clock()
        with self._lock:
            rows = list(self.rows.values())
        return {"queued": len(rows), "running": sum(1 for r in rows if r["by"] and r["until"] > now),
                "oldest_s": max((now - r["at"] for r in rows), default=0.0)}


def _get(row: Any, key: str, i: int) -> Any:
    return row[key] if isinstance(row, dict) else row[i]


def mode(env: Any = None) -> str:
    e = os.environ if env is None else env
    m = str(e.get("VALXEL_RUN_QUEUE", "")).strip().lower() or "local"
    if m not in ("local", "postgres"):
        raise ValueError(f"VALXEL_RUN_QUEUE is {m!r}: use local or postgres")
    return m


# --------------------------------------------------------------------------- the worker

class Worker:
    """Claims queued runs and runs them on `engine`, `concurrency` at a time, renewing the claims of
    those under way. `run_once` is one pass, for tests; `serve` loops until `stop` is set."""

    def __init__(self, engine: Any, queue: RunQueue, *, concurrency: int = 4, lease_s: float | None = None,
                 max_attempts: int | None = None, poll_s: float = 1.0, busy_delay_s: float = 5.0,
                 name: str = "") -> None:
        from concurrent.futures import ThreadPoolExecutor

        self.engine, self.queue = engine, queue
        self.concurrency = concurrency
        self.lease_s = float(lease_s or os.environ.get("VALXEL_RUN_QUEUE_LEASE_S", "120"))
        self.max_attempts = int(max_attempts or os.environ.get("VALXEL_RUN_QUEUE_MAX_ATTEMPTS", "3"))
        self.poll_s, self.busy_delay_s = poll_s, busy_delay_s
        self.me = name or f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:6]}"
        self.stop = threading.Event()
        self.held: set[int] = set()
        self._lock = threading.Lock()
        self._pool = ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="run")
        self.last_tick = time.monotonic()

    def _free(self) -> int:
        with self._lock:
            return self.concurrency - len(self.held)

    def run_once(self, wait: bool = True) -> int:
        """Claim what there's room for and run it; returns how many were claimed."""
        from .tenancy import PLATFORM, bound

        self.last_tick = time.monotonic()
        started = []
        while self._free() > 0:
            with bound(PLATFORM):  # the queue is the platform's: each run is scoped to its own workspace
                job = self.queue.claim(self.me, self.lease_s)
            if job is None:
                break
            with self._lock:
                self.held.add(job.id)
            started.append(self._pool.submit(self._run, job))
        if wait:
            for f in started:
                f.result()
        return len(started)

    def _run(self, job: Job) -> None:
        from .cluster import Busy, LeaseLost
        from .tenancy import PLATFORM, bound

        try:
            if job.attempts > self.max_attempts:  # its worker died this many times: don't try again
                log.error("run %s of %s: dropped after %s attempts", job.id, job.thread_id, job.attempts - 1)
                self.engine.errors[job.thread_id] = (f"The run was claimed {job.attempts - 1} times and never "
                                                     "finished; resume it from the feature page")
                with bound(PLATFORM):
                    self.queue.done(self.me, job.id)
                    dead_letter(self.engine, job)
                return
            try:
                with joined(job.traceparent):  # the run continues the trace that queued it
                    self.engine.run_job(job.thread_id, job.payload)
            except (Busy, LeaseLost):  # another worker has the thread now: try again shortly
                with bound(PLATFORM):
                    self.queue.later(self.me, job.id, self.busy_delay_s)
                return
            except Exception:  # noqa: BLE001 - recorded on the thread (engine._execute); the reaper decides
                log.exception("run %s of %s failed", job.id, job.thread_id)
            with bound(PLATFORM):
                self.queue.done(self.me, job.id)
        finally:
            with self._lock:
                self.held.discard(job.id)

    def renew(self) -> None:
        from .tenancy import PLATFORM, bound

        with self._lock:
            ids = list(self.held)
        if ids:
            with bound(PLATFORM):
                self.queue.renew(self.me, ids, self.lease_s)

    def serve(self) -> None:
        log.info("worker %s: running queued work, %s at a time", self.me, self.concurrency)
        renew_every, renewed = self.lease_s / 3, time.monotonic()
        while not self.stop.is_set():
            try:
                claimed = self.run_once(wait=False)
                if time.monotonic() - renewed >= renew_every:
                    self.renew()
                    renewed = time.monotonic()
            except Exception:  # noqa: BLE001 - the database blinked: carry on at the next poll
                log.exception("worker %s: polling the run queue failed", self.me)
                claimed = 0
            if not claimed:
                self.stop.wait(self.poll_s)

    def drain(self, timeout_s: float) -> list[int]:
        """Stop: let runs under way finish for up to `timeout_s`, then hand back what's still running,
        so another worker carries it on from its last checkpoint at once (without counting it as a
        failed attempt) rather than when the claim runs out. Returns the jobs handed back."""
        from .tenancy import PLATFORM, bound

        self.stop.set()
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            with self._lock:
                if not self.held:
                    break
            time.sleep(0.2)
        with self._lock:
            left = sorted(self.held)
        with bound(PLATFORM):
            for job_id in left:
                try:
                    self.queue.later(self.me, job_id, 0)
                except Exception:  # noqa: BLE001 - its claim runs out instead
                    log.warning("worker %s: couldn't hand back run %s", self.me, job_id)
        self._pool.shutdown(wait=not left)
        return left

    def healthy(self, stale_s: float = 60.0) -> bool:
        return not self.stop.is_set() and time.monotonic() - self.last_tick < stale_s


def serve_health(worker: Worker, port: int) -> Any:
    """/healthz for the worker's probes: 200 while it polls the queue, with the queue's depth."""
    import json
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - http.server's name
            if self.path.rstrip("/") == "/metrics":
                return self._metrics()
            if self.path.rstrip("/") != "/healthz":
                self.send_response(404)
                self.end_headers()
                return
            ok = worker.healthy()
            body = {"ok": ok, "worker": worker.me, "running": len(worker.held)}
            try:
                from .tenancy import PLATFORM, bound

                with bound(PLATFORM):
                    body["queue"] = worker.queue.depth()
            except Exception:  # noqa: BLE001 - the probe answers from what it knows
                pass
            data = json.dumps(body).encode()
            self.send_response(200 if ok else 503)
            self.send_header("content-type", "application/json")
            self.end_headers()
            self.wfile.write(data)

        def _metrics(self):
            """This worker's sessions (valxel_sdlc.metrics), with VALXEL_METRICS_TOKEN as a bearer token."""
            import hmac
            import os

            from . import metrics

            want = os.environ.get("VALXEL_METRICS_TOKEN", "")
            got = (self.headers.get("authorization") or "").removeprefix("Bearer ").strip()
            code = 404 if not want else 200 if hmac.compare_digest(got.encode(), want.encode()) else 401
            data = metrics.REGISTRY.text().encode() if code == 200 else b""
            self.send_response(code)
            self.send_header("content-type", "text/plain; version=0.0.4")
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):  # probes every few seconds: not worth a log line each
            pass

    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)  # noqa: S104 - the pod's probe port
    threading.Thread(target=server.serve_forever, name="worker-health", daemon=True).start()
    return server


DEAD_LETTERS = "queue:dead_letters"


def dead_letter(engine, job) -> None:
    """Keep a note of a run the queue gave up on, for the tool's health (valxel_sdlc.health)."""
    from datetime import UTC, datetime

    try:
        cluster = engine.cluster
        seen = list(cluster.get(DEAD_LETTERS) or [])
        seen.insert(0, {"at": datetime.now(UTC).isoformat(), "thread_id": job.thread_id, "run_id": job.id,
                        "attempts": job.attempts - 1})
        cluster.put(DEAD_LETTERS, seen[:200])
    except Exception:  # the run is already dropped and logged; the note is extra
        log.exception("dead letter for %s not noted", job.id)

"""Valxel's own audit trail, sent to the audit sink (T-113).

Every record is one CloudEvent of type `valxel.audit.v1`, the contract the platform's audit sink
(valxel-shared-services/ss-audit-sink) holds every producer to: the LLM gateway, the templates,
and now the tool itself. The sink copies the topic into an append-only, hash-chained Postgres
table and an archive under Object Lock, so the tool's history no longer rests on a database the
tool can write to.

What gets recorded:

    workspace.*, member.*, group.*, connection.*   the workspace audit log's entries, mirrored
    gate.decide            a person's answer to a gate or question: approve, revise, add budget,
                           override, accept a risk...
    change.recorded        a production change: release, flag off, rollback (ops/changes.py)
    signin.*               sign-ins, failed sign-ins and sign-outs
    api.<method>           every request that changes something, with its route and outcome, so a
                           refused attempt (401, 403) is recorded too

Records name people and things by id. Never put field values in them: names, emails, notes,
document text. Details hold counts, codes and ids.

Delivery: a record is written to an outbox in the tool's own database first, in the request that
made it, so a Kafka outage loses nothing and a failed write fails the action. The watcher's
`publish_audit` job relays the outbox to Kafka in order, at least once; the sink drops duplicates
by event id. VALXEL_AUDIT_SINK picks where the relay sends: `kafka` (VALXEL_AUDIT_KAFKA_BOOTSTRAP,
topic VALXEL_AUDIT_TOPIC, default audit.v1), `log` (one JSON line per record on the `audit`
logger), or `off`. Strict mode requires kafka.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import uuid
from datetime import UTC, datetime
from functools import cache
from pathlib import Path
from typing import Any, Protocol

from . import append_only

log = logging.getLogger("audit")

EVENT_TYPE = "valxel.audit.v1"
SOURCE = "valxel/agentic-sdlc"
SERVICE = "valxel"
ACTOR_TYPES = ("user", "service", "agent")
OUTCOMES = ("success", "denied", "failure")
SCHEMA_FILE = Path(__file__).with_name("valxel.audit.v1.schema.json")  # contracts/audit/'s copy, byte for byte
# What a workspace audit entry's detail may carry into the sink: ids and codes, never names or emails.
_ID_KEYS = ("member", "group", "tool", "account", "product", "plan", "role", "mode", "region", "gate", "version")


class AuditUnavailable(RuntimeError):
    """The record could not be written, so the action must not be reported as done."""


@cache
def _validator():
    from jsonschema import Draft202012Validator

    return Draft202012Validator(json.loads(SCHEMA_FILE.read_text(encoding="utf-8")))


def problems(event: dict) -> list[str]:
    return [f"{'/'.join(map(str, e.absolute_path)) or 'event'}: {e.message}"[:200]
            for e in _validator().iter_errors(event)]


def _clean(details: dict | None) -> dict:
    """Counts, codes and ids: scalars only, strings cut short, nothing nested deeper than a list."""
    out: dict[str, Any] = {}
    for k, v in (details or {}).items():
        if v is None or v == "" or v == [] or v == {}:
            continue
        if isinstance(v, bool | int | float):
            out[str(k)[:60]] = v
        elif isinstance(v, str):
            out[str(k)[:60]] = v[:200]
        elif isinstance(v, list | tuple) and all(isinstance(x, str | int | float | bool) for x in v):
            out[str(k)[:60]] = [x[:200] if isinstance(x, str) else x for x in list(v)[:50]]
    return out


def event(action: str, *, actor: str, resource: tuple[str, list[str]], actor_type: str = "user",
          outcome: str = "success", workspace: str = "", product: str = "", reason: str = "",
          details: dict | None = None, client: str = "", on_behalf_of: str = "", trace_id: str = "",
          at: str = "") -> dict:
    if actor_type not in ACTOR_TYPES or outcome not in OUTCOMES:
        raise ValueError(f"actor_type is one of {ACTOR_TYPES}, outcome one of {OUTCOMES}")
    rtype, ids = resource
    ids = [str(i)[:300] for i in ids if str(i)][:1000]
    data: dict[str, Any] = {"service": SERVICE, "action": action[:200],
                            "actor": {"id": (actor or "unknown")[:300], "type": actor_type,
                                      **({"client": client[:300]} if client else {})},
                            "resource": {"type": rtype[:200], "ids": ids}, "outcome": outcome}
    env = os.environ.get("VALXEL_ENVIRONMENT", "")
    for k, v in (("product", product[:100]), ("environment", env[:100]), ("on_behalf_of", on_behalf_of[:300]),
                 ("reason", reason[:300]), ("trace_id", trace_id if len(trace_id) == 32 else "")):
        if v:
            data[k] = v
    det = _clean({**({"workspace": workspace} if workspace else {}), **(details or {})})
    if det:
        data["details"] = det
    return {"specversion": "1.0", "id": str(uuid.uuid4()), "source": SOURCE, "type": EVENT_TYPE,
            "time": at or datetime.now(UTC).isoformat(), "subject": f"{rtype}/{ids[0]}" if ids else rtype,
            "datacontenttype": "application/json", "data": data}


# --- the outbox: written in the request, relayed by the watcher ------------------------------------

class Outbox(Protocol):
    def add(self, workspace: str, ev: dict) -> None: ...
    def after(self, seq: int, limit: int) -> list[tuple[int, dict]]: ...  # (seq, event), oldest first
    def cursor(self, name: str) -> int: ...
    def advance(self, name: str, seq: int) -> None: ...


class InMemoryOutbox:
    def __init__(self) -> None:
        self.rows: list[tuple[int, str, dict]] = []
        self.cursors: dict[str, int] = {}
        self._lock = threading.Lock()

    def add(self, workspace, ev):
        with self._lock:
            self.rows.append((len(self.rows) + 1, workspace, json.loads(json.dumps(ev))))

    def after(self, seq, limit):
        with self._lock:
            return [(s, e) for s, _, e in self.rows if s > seq][:limit]

    def cursor(self, name):
        return self.cursors.get(name, 0)

    def advance(self, name, seq):
        self.cursors[name] = max(self.cursors.get(name, 0), seq)

    def events(self) -> list[dict]:
        return [e for _, _, e in self.rows]


class PostgresOutbox:
    """Append-only like the audit log it feeds (valxel_sdlc.append_only); only the relay's cursor moves."""

    DDL = (
        """CREATE TABLE IF NOT EXISTS valxel_audit_outbox (
             seq BIGSERIAL PRIMARY KEY, id TEXT UNIQUE NOT NULL, workspace TEXT NOT NULL DEFAULT '',
             at TIMESTAMPTZ NOT NULL DEFAULT now(), event JSONB NOT NULL)""",
        *append_only.statements("valxel_audit_outbox"),
        """CREATE TABLE IF NOT EXISTS valxel_audit_relay (
             name TEXT PRIMARY KEY, seq BIGINT NOT NULL, at TIMESTAMPTZ NOT NULL DEFAULT now())""",
    )

    def __init__(self, pool) -> None:
        self.pool = pool
        with pool.connection() as conn:
            for stmt in self.DDL:
                conn.execute(stmt)

    def add(self, workspace, ev):
        from .tenancy import PLATFORM, bound

        with bound(PLATFORM), self.pool.connection() as conn:  # whoever acted, the record is the platform's
            conn.execute("INSERT INTO valxel_audit_outbox (id, workspace, event) VALUES (%s, %s, %s::jsonb)",
                         (ev["id"], workspace, json.dumps(ev)))

    def after(self, seq, limit):
        with self.pool.connection() as conn:
            rows = conn.execute("SELECT seq, event FROM valxel_audit_outbox WHERE seq > %s ORDER BY seq LIMIT %s",
                                (seq, limit)).fetchall()
        return [(r["seq"], r["event"]) for r in rows]

    def cursor(self, name):
        with self.pool.connection() as conn:
            r = conn.execute("SELECT seq FROM valxel_audit_relay WHERE name = %s", (name,)).fetchone()
        return int(r["seq"]) if r else 0

    def advance(self, name, seq):
        with self.pool.connection() as conn:
            conn.execute("INSERT INTO valxel_audit_relay (name, seq) VALUES (%s, %s) ON CONFLICT (name) DO UPDATE "
                         "SET seq = GREATEST(valxel_audit_relay.seq, EXCLUDED.seq), at = now()", (name, seq))


# --- where the relay sends ---------------------------------------------------------------------------

class Sink(Protocol):
    def publish(self, key: str, ev: dict) -> None: ...  # raises AuditUnavailable when it can't
    def flush(self) -> None: ...


class LogSink:
    def publish(self, key, ev):
        log.info(json.dumps(ev, default=str, separators=(",", ":")))

    def flush(self):
        pass


class MemorySink:
    def __init__(self) -> None:
        self.events: list[dict] = []
        self.fail = False

    def publish(self, key, ev):
        if self.fail:
            raise AuditUnavailable("sink down")
        self.events.append(ev)

    def flush(self):
        pass


class KafkaSink:
    """An idempotent producer (acks=all): flush() returns only once the broker has every record."""

    def __init__(self, bootstrap: str, topic: str, config: dict | None = None, producer: Any = None,
                 timeout: float = 10.0) -> None:
        if producer is None:
            from confluent_kafka import Producer

            producer = Producer({"bootstrap.servers": bootstrap, "enable.idempotence": True, "acks": "all",
                                 "compression.type": "zstd", "message.timeout.ms": int(timeout * 1000),
                                 "client.id": "valxel-audit", **(config or {})})
        self.producer, self.topic, self.timeout = producer, topic, timeout
        self.errors: list[Any] = []

    def publish(self, key, ev):
        def delivered(err, _msg):
            if err is not None:
                self.errors.append(err)

        try:
            self.producer.produce(self.topic, key=key.encode(), value=json.dumps(ev, separators=(",", ":")).encode(),
                                  headers={"ce_type": EVENT_TYPE, "ce_id": ev["id"]}, on_delivery=delivered)
        except BufferError as exc:
            raise AuditUnavailable("audit producer queue is full") from exc
        self.producer.poll(0)

    def flush(self):
        left = self.producer.flush(self.timeout)
        errors, self.errors = self.errors, []
        if left or errors:
            raise AuditUnavailable(f"{left} audit records not delivered; {errors[:1]}")


def sink_from_env(env=None) -> Sink | None:
    env = os.environ if env is None else env
    kind = (env.get("VALXEL_AUDIT_SINK") or "log").strip().lower()
    if kind == "off":
        return None
    if kind == "log":
        return LogSink()
    if kind == "kafka":
        bootstrap = env.get("VALXEL_AUDIT_KAFKA_BOOTSTRAP", "")
        if not bootstrap:
            raise RuntimeError("VALXEL_AUDIT_SINK is kafka: set VALXEL_AUDIT_KAFKA_BOOTSTRAP")
        config = json.loads(env.get("VALXEL_AUDIT_KAFKA_CONFIG") or "{}")  # security.protocol, sasl.* ...
        return KafkaSink(bootstrap, env.get("VALXEL_AUDIT_TOPIC") or "audit.v1", config)
    raise RuntimeError(f"VALXEL_AUDIT_SINK is {kind!r}: use kafka, log or off")


# --- the tool's auditor ----------------------------------------------------------------------------

class Auditor:
    RELAY = "kafka"

    def __init__(self, outbox: Outbox | None = None, sink: Sink | None = None) -> None:
        self.outbox = outbox or InMemoryOutbox()
        self.sink = sink

    def record(self, action: str, *, workspace: str = "", **kw) -> dict:
        ev = event(action, workspace=workspace, **kw)
        bad = problems(ev)
        if bad:  # the sink would drop it: fail here, where someone sees it
            raise ValueError(f"audit event {action} breaks valxel.audit.v1: {bad[0]}")
        try:
            self.outbox.add(workspace, ev)
        except Exception as exc:
            raise AuditUnavailable(f"audit record {action} not written") from exc
        return ev

    def from_workspace(self, workspace: str, actor: str, action: str, detail: dict) -> None:
        """A workspace audit log entry, mirrored: ids and codes only."""
        detail = detail or {}
        rtype = action.split(".", 1)[0]
        ids = [str(detail[k]) for k in ("member", "group", "tool", "account") if detail.get(k)] or [workspace]
        actor_type = "service" if actor in ("valxel", "system", "") else "user"
        self.record(action, actor=actor or "valxel", actor_type=actor_type, resource=(rtype, ids),
                    workspace=workspace, details={k: detail[k] for k in _ID_KEYS if k in detail})

    def relay(self, batch: int = 500) -> int:
        """Send what the sink hasn't had yet, in order; the cursor moves only past what it confirmed."""
        if self.sink is None:
            return 0
        sent = 0
        while True:
            start = self.outbox.cursor(self.RELAY)
            rows = self.outbox.after(start, batch)
            if not rows:
                return sent
            for _, ev in rows:
                key = (ev.get("data") or {}).get("details", {}).get("workspace") or SERVICE
                self.sink.publish(key, ev)
            self.sink.flush()
            self.outbox.advance(self.RELAY, rows[-1][0])
            sent += len(rows)

    def backlog(self) -> int:
        return len(self.outbox.after(self.outbox.cursor(self.RELAY), 10_000))

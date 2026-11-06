"""The current state of every Vrixel CloudWatch alarm, in every environment.

Production alarms also go into the incident flow; the board is what G6 reads: an alarm firing in
an earlier environment on a resource the feature created, or a component it changes, is shown to
the approvers before production.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone


def _age_minutes(at: str, now: datetime) -> float:
    try:
        t = datetime.fromisoformat(str(at).replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    return (now - (t if t.tzinfo else t.replace(tzinfo=timezone.utc))).total_seconds() / 60


class InMemoryAlarmBoard:
    def __init__(self) -> None:
        self.latest: dict[str, dict] = {}
        self._lock = threading.Lock()

    def record(self, signal: dict) -> None:
        with self._lock:
            old = self.latest.get(signal["fingerprint"])
            # Keep when it started firing; a state change can arrive late, never let it go back in time.
            if old and old.get("at", "") > signal.get("at", ""):
                return
            since = old.get("since") if old and old["status"] == "firing" and signal["status"] == "firing" else signal["at"]
            self.latest[signal["fingerprint"]] = {**signal, "since": since}

    def firing(self, product_id: str) -> list[dict]:
        with self._lock:
            return sorted((dict(s) for s in self.latest.values()
                           if s["product_id"] == product_id and s["status"] == "firing"), key=lambda s: s["since"])


class PostgresAlarmBoard:
    DDL = ("""CREATE TABLE IF NOT EXISTS vrixel_alarm_states (
                fingerprint TEXT PRIMARY KEY, product_id TEXT NOT NULL, status TEXT NOT NULL,
                at TEXT NOT NULL, data JSONB NOT NULL)""",
           "CREATE INDEX IF NOT EXISTS vrixel_alarm_states_product ON vrixel_alarm_states (product_id, status)")

    def __init__(self, pool) -> None:
        self.pool = pool
        with self.pool.connection() as conn:
            for statement in self.DDL:
                conn.execute(statement)

    def record(self, signal: dict) -> None:
        with self.pool.connection() as conn:
            row = conn.execute("SELECT status, at, data FROM vrixel_alarm_states WHERE fingerprint = %s",
                               (signal["fingerprint"],)).fetchone()
            if row and row["at"] > signal.get("at", ""):
                return
            keep = row and row["status"] == "firing" and signal["status"] == "firing"
            data = {**signal, "since": dict(row["data"]).get("since") if keep else signal["at"]}
            conn.execute(
                "INSERT INTO vrixel_alarm_states (fingerprint, product_id, status, at, data) VALUES (%s, %s, %s, %s, %s::jsonb)"
                " ON CONFLICT (fingerprint) DO UPDATE SET status = EXCLUDED.status, at = EXCLUDED.at, data = EXCLUDED.data",
                (signal["fingerprint"], signal["product_id"], signal["status"], signal.get("at", ""), json.dumps(data)))

    def firing(self, product_id: str) -> list[dict]:
        with self.pool.connection() as conn:
            rows = conn.execute("SELECT data FROM vrixel_alarm_states WHERE product_id = %s AND status = 'firing'",
                                (product_id,)).fetchall()
        return sorted((dict(r["data"]) for r in rows), key=lambda s: s.get("since", ""))


def for_feature(board, state: dict, *, before: str = "production", now: datetime | None = None,
                fresh_minutes: int = 60) -> list[dict]:
    """Alarms firing outside `before` on the feature's own resources, on resources its components
    use, or on its components themselves: what G6 approvers should see before production. Alerts
    that never send a resolution (OpenChoreo's) count only while recent."""
    if board is None:
        return []
    now = now or datetime.now(timezone.utc)
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

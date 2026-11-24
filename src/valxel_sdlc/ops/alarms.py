"""The current state of every Valxel CloudWatch alarm, in every environment.

Production alarms also go into the incident flow; the board is what G6 reads: an alarm firing in
an earlier environment on a resource the feature created, or a component it changes, is shown to
the approvers before production.
"""

from __future__ import annotations

import json
import threading

from .firing import _age_minutes, for_feature  # noqa: F401  (the core's; re-exported)


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
    DDL = ("""CREATE TABLE IF NOT EXISTS valxel_alarm_states (
                fingerprint TEXT PRIMARY KEY, product_id TEXT NOT NULL, status TEXT NOT NULL,
                at TEXT NOT NULL, data JSONB NOT NULL)""",
           "CREATE INDEX IF NOT EXISTS valxel_alarm_states_product ON valxel_alarm_states (product_id, status)")

    def __init__(self, pool) -> None:
        self.pool = pool
        with self.pool.connection() as conn:
            for statement in self.DDL:
                conn.execute(statement)

    def record(self, signal: dict) -> None:
        with self.pool.connection() as conn:
            row = conn.execute("SELECT status, at, data FROM valxel_alarm_states WHERE fingerprint = %s",
                               (signal["fingerprint"],)).fetchone()
            if row and row["at"] > signal.get("at", ""):
                return
            keep = row and row["status"] == "firing" and signal["status"] == "firing"
            data = {**signal, "since": dict(row["data"]).get("since") if keep else signal["at"]}
            conn.execute(
                "INSERT INTO valxel_alarm_states (fingerprint, product_id, status, at, data) VALUES (%s, %s, %s, %s, %s::jsonb)"
                " ON CONFLICT (fingerprint) DO UPDATE SET status = EXCLUDED.status, at = EXCLUDED.at, data = EXCLUDED.data",
                (signal["fingerprint"], signal["product_id"], signal["status"], signal.get("at", ""), json.dumps(data)))

    def firing(self, product_id: str) -> list[dict]:
        with self.pool.connection() as conn:
            rows = conn.execute("SELECT data FROM valxel_alarm_states WHERE product_id = %s AND status = 'firing'",
                                (product_id,)).fetchall()
        return sorted((dict(r["data"]) for r in rows), key=lambda s: s.get("since", ""))



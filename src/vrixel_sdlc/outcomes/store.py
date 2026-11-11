"""Readings the tool has taken, and the ones people typed in: aggregates only, per product and goal.
In the platform database when there is one."""

from __future__ import annotations

import json
import threading

from . import Reading


class InMemoryReadingStore:
    def __init__(self) -> None:
        self.rows: list[dict] = []
        self._lock = threading.Lock()

    def add(self, reading: Reading) -> None:
        with self._lock:
            self.rows.append(reading.as_dict())

    def history(self, product_id: str, goal_id: str) -> list[dict]:
        """Oldest first."""
        with self._lock:
            return [dict(r) for r in self.rows if r["product_id"] == product_id and r["goal_id"] == goal_id]


class PostgresReadingStore:
    DDL = (
        """CREATE TABLE IF NOT EXISTS vrixel_metric_readings (
             id BIGSERIAL PRIMARY KEY, product_id TEXT NOT NULL, goal_id TEXT NOT NULL, source TEXT NOT NULL,
             status TEXT NOT NULL, period_start TIMESTAMPTZ NOT NULL, period_end TIMESTAMPTZ NOT NULL,
             value DOUBLE PRECISION, n INTEGER, data JSONB NOT NULL, at TIMESTAMPTZ NOT NULL DEFAULT now())""",
        "CREATE INDEX IF NOT EXISTS vrixel_metric_readings_goal ON vrixel_metric_readings (product_id, goal_id, id)",
    )

    def __init__(self, pool) -> None:
        self.pool = pool
        with self.pool.connection() as conn:
            for statement in self.DDL:
                conn.execute(statement)

    def add(self, reading: Reading) -> None:
        r = reading.as_dict()
        with self.pool.connection() as conn:
            conn.execute("INSERT INTO vrixel_metric_readings (product_id, goal_id, source, status, period_start, "
                         "period_end, value, n, data) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)",
                         (r["product_id"], r["goal_id"], r["source"], r["status"], r["start"], r["end"], r["value"],
                          r["n"], json.dumps(r, default=str)))

    def history(self, product_id: str, goal_id: str) -> list[dict]:
        with self.pool.connection() as conn:
            rows = conn.execute("SELECT data FROM vrixel_metric_readings WHERE product_id = %s AND goal_id = %s "
                                "ORDER BY id", (product_id, goal_id)).fetchall()
        return [dict(r["data"]) for r in rows]

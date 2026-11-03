"""Capability catalog: what has been built (live) and what is designed and in flight. In
production it can be mirrored to the Backstage catalog in the OpenChoreo portal; the pipeline's
own copy lives in the platform database."""

from __future__ import annotations

import threading
from typing import Protocol


class CapabilityCatalog(Protocol):
    def lookup(self, product_id: str) -> list[dict]: ...
    def register(self, capability: dict) -> dict: ...


class InMemoryCatalog:
    def __init__(self, seed: dict[str, list[dict]] | None = None) -> None:
        self._caps: dict[str, list[dict]] = {k: list(v) for k, v in (seed or {}).items()}
        self._lock = threading.Lock()

    def lookup(self, product_id: str) -> list[dict]:
        with self._lock:
            return [dict(c) for c in self._caps.get(product_id, [])]

    def register(self, capability: dict) -> dict:
        with self._lock:
            caps = self._caps.setdefault(capability["product_id"], [])
            caps[:] = [c for c in caps if c["id"] != capability["id"]] + [dict(capability)]
            return dict(capability)


class PostgresCatalog:
    """The catalog in the platform database, so what was designed and released survives restarts."""

    DDL = (
        """CREATE TABLE IF NOT EXISTS vrixel_capabilities (
             id TEXT PRIMARY KEY, product_id TEXT NOT NULL, status TEXT NOT NULL, data JSONB NOT NULL,
             updated_at TIMESTAMPTZ NOT NULL DEFAULT now())""",
        "CREATE INDEX IF NOT EXISTS vrixel_capabilities_product ON vrixel_capabilities (product_id)",
    )

    def __init__(self, pool) -> None:
        self.pool = pool
        with self.pool.connection() as conn:
            for statement in self.DDL:
                conn.execute(statement)

    def lookup(self, product_id: str) -> list[dict]:
        with self.pool.connection() as conn:
            rows = conn.execute("SELECT data FROM vrixel_capabilities WHERE product_id = %s ORDER BY updated_at",
                                (product_id,)).fetchall()
        return [dict(r["data"]) for r in rows]

    def register(self, capability: dict) -> dict:
        import json

        with self.pool.connection() as conn:
            conn.execute(
                "INSERT INTO vrixel_capabilities (id, product_id, status, data) VALUES (%s, %s, %s, %s::jsonb) "
                "ON CONFLICT (id) DO UPDATE SET status = EXCLUDED.status, data = EXCLUDED.data, updated_at = now()",
                (capability["id"], capability["product_id"], capability.get("status", "live"),
                 json.dumps(capability, default=str)))
        return dict(capability)


def default_catalog() -> InMemoryCatalog:
    """Empty: capabilities enter the catalog when the pipeline approves a design (in flight) and
    releases it (live). Seed it only with capabilities that really exist."""
    return InMemoryCatalog()

"""Usage ledger: one tagged record per model call. System of record for attribution;
reconciled nightly against Anthropic's Usage and Cost API for billing."""

from __future__ import annotations

import json
import threading
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from typing import Iterable, Protocol


@dataclass
class UsageRecord:
    ts: str
    product_id: str
    feature_id: str
    phase: str
    role: str
    runtime: str
    provider: str
    model: str
    workspace: str
    input_tokens: int
    cache_creation_input_tokens: int
    cache_read_input_tokens: int
    output_tokens: int
    cache_ttl: str
    prompt_version: str
    prefix_hashes: dict = field(default_factory=dict)
    est_cost_usd: float = 0.0
    cost_basis: str = "list"
    request_id: str = ""

    @property
    def total_input(self) -> int:
        return self.input_tokens + self.cache_creation_input_tokens + self.cache_read_input_tokens


class Ledger(Protocol):
    def record(self, rec: UsageRecord) -> None: ...
    def records(self, feature_id: str | None = None) -> list[UsageRecord]: ...


class InMemoryLedger:
    def __init__(self) -> None:
        self._rows: list[UsageRecord] = []
        self._lock = threading.Lock()

    def record(self, rec: UsageRecord) -> None:
        with self._lock:
            self._rows.append(rec)

    def records(self, feature_id: str | None = None) -> list[UsageRecord]:
        with self._lock:
            return [r for r in self._rows if feature_id is None or r.feature_id == feature_id]


class PostgresLedger:
    DDL = """
    CREATE TABLE IF NOT EXISTS vrixel_usage (
      id BIGSERIAL PRIMARY KEY,
      ts TIMESTAMPTZ NOT NULL,
      product_id TEXT NOT NULL, feature_id TEXT NOT NULL, phase TEXT NOT NULL, role TEXT NOT NULL,
      runtime TEXT NOT NULL, provider TEXT NOT NULL, model TEXT NOT NULL, workspace TEXT NOT NULL,
      input_tokens INT NOT NULL, cache_creation_input_tokens INT NOT NULL,
      cache_read_input_tokens INT NOT NULL, output_tokens INT NOT NULL,
      cache_ttl TEXT NOT NULL, prompt_version TEXT NOT NULL, prefix_hashes JSONB NOT NULL,
      est_cost_usd NUMERIC(12,6) NOT NULL, cost_basis TEXT NOT NULL, request_id TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS vrixel_usage_feature ON vrixel_usage (feature_id);
    CREATE INDEX IF NOT EXISTS vrixel_usage_product_ts ON vrixel_usage (product_id, ts);
    """

    def __init__(self, pool) -> None:
        self.pool = pool
        with self.pool.connection() as conn:
            for stmt in filter(None, (s.strip() for s in self.DDL.split(";"))):
                conn.execute(stmt)

    def record(self, rec: UsageRecord) -> None:
        d = asdict(rec)
        d["prefix_hashes"] = json.dumps(d["prefix_hashes"])
        cols = ", ".join(d)
        vals = ", ".join(f"%({k})s" for k in d)
        with self.pool.connection() as conn:
            conn.execute(f"INSERT INTO vrixel_usage ({cols}) VALUES ({vals})", d)

    def records(self, feature_id: str | None = None) -> list[UsageRecord]:
        sql = "SELECT * FROM vrixel_usage" + (" WHERE feature_id = %s" if feature_id else "") + " ORDER BY id"
        with self.pool.connection() as conn:
            rows = conn.execute(sql, (feature_id,) if feature_id else ()).fetchall()
        out = []
        for r in rows:
            r = dict(r)
            r.pop("id", None)
            r["ts"] = r["ts"].isoformat() if hasattr(r["ts"], "isoformat") else r["ts"]
            r["est_cost_usd"] = float(r["est_cost_usd"])
            if isinstance(r["prefix_hashes"], str):
                r["prefix_hashes"] = json.loads(r["prefix_hashes"])
            out.append(UsageRecord(**r))
        return out


def summarize(records: Iterable[UsageRecord]) -> dict:
    """KPIs from the token deep dive: cache-hit rate, write share, cost by phase and model."""
    records = list(records)
    total_in = sum(r.total_input for r in records)
    reads = sum(r.cache_read_input_tokens for r in records)
    writes = sum(r.cache_creation_input_tokens for r in records)
    by_phase: dict[str, float] = defaultdict(float)
    by_model: dict[str, float] = defaultdict(float)
    for r in records:
        by_phase[r.phase] += r.est_cost_usd
        by_model[r.model] += r.est_cost_usd
    return {
        "calls": len(records),
        "input_tokens": total_in,
        "output_tokens": sum(r.output_tokens for r in records),
        "cache_hit_rate": round(reads / total_in, 4) if total_in else 0.0,
        "cache_write_share": round(writes / total_in, 4) if total_in else 0.0,
        "est_cost_usd": round(sum(r.est_cost_usd for r in records), 4),
        "cost_by_phase": {k: round(v, 4) for k, v in sorted(by_phase.items())},
        "cost_by_model": {k: round(v, 4) for k, v in sorted(by_model.items())},
    }

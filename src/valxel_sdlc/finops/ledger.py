"""The cost ledger: every dollar a product spends, once, in FinOps FOCUS columns.

A record is one cost for one period: what it was spent on (category, service, resource), whose it
is (product, and feature when one feature caused it), whether it was spent building the feature
(stage build: until production) or running the product (stage run), and how sure the figure is:

    basis     estimate   a planning figure (G4's AWS sizes)
              metered    usage times a list price (model tokens, CI minutes, build minutes)
              billed     from the provider's bill (AWS Cost Explorer)
              allocated  a share of something shared (an environment several features used,
                         the platform all products use)
    status    provisional until the source stops changing it (AWS bills settle in about two
              days), then final

Collectors upsert by `key`, so running one again replaces its figures rather than adding to them.
Feature records are never also counted in their product's own records: the product's AWS record
is what carries no feature tag, its environments' idle time is what no feature was using.
"""

from __future__ import annotations

import csv
import io
import json
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Iterable, Protocol

STAGES = ("build", "run")
CATEGORIES = ("llm", "ci", "test", "build", "environment", "aws", "compute", "incident", "people", "platform")
BASES = ("estimate", "metered", "billed", "allocated")

# FOCUS ServiceCategory and provider for each of our categories.
_FOCUS = {"llm": ("AI and Machine Learning", "Anthropic"), "ci": ("Developer Tools", "GitHub"),
          "test": ("Developer Tools", "GitHub"), "build": ("Developer Tools", "OpenChoreo"),
          "environment": ("Compute", "OpenChoreo"), "aws": ("", "AWS"), "compute": ("Compute", "OpenChoreo"),
          "incident": ("AI and Machine Learning", "Anthropic"), "people": ("Other", "People"),
          "platform": ("Other", "Valxel platform")}


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class CostRecord:
    key: str
    product_id: str
    stage: str  # build | run
    category: str
    usd: float
    period_start: str
    period_end: str
    feature_id: str = ""
    env: str = ""
    source: str = ""  # usage-ledger, github-actions, openchoreo-finops, cost-explorer, rates, allocation
    service: str = ""  # e.g. Amazon DynamoDB, claude-opus-5-5, GitHub Actions (Linux)
    resource: str = ""  # repo, component, run, PR, gate
    quantity: float = 0.0
    unit: str = ""  # tokens, minutes, hours
    basis: str = "metered"
    status: str = "final"
    detail: dict = field(default_factory=dict)
    updated_at: str = ""

    def __post_init__(self) -> None:
        if self.stage not in STAGES or self.category not in CATEGORIES or self.basis not in BASES:
            raise ValueError(f"cost record {self.key}: bad stage, category or basis")
        self.usd = round(float(self.usd), 6)


class CostLedger(Protocol):
    def upsert(self, records: Iterable[CostRecord]) -> int: ...
    def query(self, *, product_id: str | None = None, feature_id: str | None = None, stage: str | None = None,
              start: str | None = None, end: str | None = None) -> list[CostRecord]: ...


def _match(r: CostRecord, product_id, feature_id, stage, start, end) -> bool:
    return ((product_id is None or r.product_id == product_id) and (feature_id is None or r.feature_id == feature_id)
            and (stage is None or r.stage == stage) and (start is None or r.period_end > start)
            and (end is None or r.period_start < end))


class InMemoryCostLedger:
    def __init__(self) -> None:
        self.rows: dict[str, CostRecord] = {}
        self._lock = threading.Lock()

    def upsert(self, records) -> int:
        n = 0
        with self._lock:
            for r in records:
                r.updated_at = r.updated_at or now()
                self.rows[r.key] = r
                n += 1
        return n

    def query(self, *, product_id=None, feature_id=None, stage=None, start=None, end=None) -> list[CostRecord]:
        with self._lock:
            return sorted((r for r in self.rows.values() if _match(r, product_id, feature_id, stage, start, end)),
                          key=lambda r: (r.period_start, r.key))


class PostgresCostLedger:
    DDL = ("""CREATE TABLE IF NOT EXISTS valxel_costs (
                key TEXT PRIMARY KEY, product_id TEXT NOT NULL, feature_id TEXT NOT NULL, stage TEXT NOT NULL,
                category TEXT NOT NULL, env TEXT NOT NULL, usd NUMERIC(18, 6) NOT NULL,
                period_start TIMESTAMPTZ NOT NULL, period_end TIMESTAMPTZ NOT NULL, data JSONB NOT NULL,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now())""",
           "CREATE INDEX IF NOT EXISTS valxel_costs_feature ON valxel_costs (feature_id)",
           "CREATE INDEX IF NOT EXISTS valxel_costs_product ON valxel_costs (product_id, period_start)")

    def __init__(self, pool) -> None:
        self.pool = pool
        with self.pool.connection() as conn:
            for statement in self.DDL:
                conn.execute(statement)

    def upsert(self, records) -> int:
        n = 0
        with self.pool.connection() as conn:
            for r in records:
                r.updated_at = now()
                conn.execute(
                    "INSERT INTO valxel_costs (key, product_id, feature_id, stage, category, env, usd, period_start,"
                    " period_end, data, updated_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, now())"
                    " ON CONFLICT (key) DO UPDATE SET usd = EXCLUDED.usd, data = EXCLUDED.data,"
                    " period_start = EXCLUDED.period_start, period_end = EXCLUDED.period_end, updated_at = now()",
                    (r.key, r.product_id, r.feature_id, r.stage, r.category, r.env, r.usd, r.period_start,
                     r.period_end, json.dumps(asdict(r), default=str)))
                n += 1
        return n

    def query(self, *, product_id=None, feature_id=None, stage=None, start=None, end=None) -> list[CostRecord]:
        where, args = [], []
        for col, val, op in (("product_id", product_id, "="), ("feature_id", feature_id, "="), ("stage", stage, "="),
                             ("period_end", start, ">"), ("period_start", end, "<")):
            if val is not None:
                where.append(f"{col} {op} %s")
                args.append(val)
        sql = "SELECT data FROM valxel_costs" + (" WHERE " + " AND ".join(where) if where else "") + \
              " ORDER BY period_start, key"
        with self.pool.connection() as conn:
            rows = conn.execute(sql, tuple(args)).fetchall()
        return [CostRecord(**dict(r["data"])) for r in rows]


# --------------------------------------------------------------------------- FOCUS export
FOCUS_COLUMNS = ("BilledCost", "EffectiveCost", "BillingCurrency", "ChargePeriodStart", "ChargePeriodEnd",
                 "ChargeCategory", "ChargeDescription", "ProviderName", "ServiceName", "ServiceCategory",
                 "ResourceId", "ConsumedQuantity", "ConsumedUnit", "Tags",
                 "x_ProductId", "x_FeatureId", "x_Stage", "x_CostCategory", "x_Environment", "x_Basis",
                 "x_Status", "x_Source")


def focus_rows(records: Iterable[CostRecord]) -> list[dict]:
    """Records as FinOps FOCUS rows. BilledCost is only what a provider bills; estimates and
    allocations carry EffectiveCost alone."""
    out = []
    for r in records:
        service_category, provider = _FOCUS[r.category]
        out.append({
            "BilledCost": r.usd if r.basis in ("billed", "metered") else 0.0, "EffectiveCost": r.usd,
            "BillingCurrency": "USD", "ChargePeriodStart": r.period_start, "ChargePeriodEnd": r.period_end,
            "ChargeCategory": "Usage", "ChargeDescription": r.detail.get("description", "") or f"{r.category} {r.resource}".strip(),
            "ProviderName": provider, "ServiceName": r.service,
            "ServiceCategory": service_category or ("Databases" if "DynamoDB" in r.service else "Other"),
            "ResourceId": r.resource, "ConsumedQuantity": r.quantity or "", "ConsumedUnit": r.unit,
            "Tags": json.dumps({"valxel:product": r.product_id, **({"valxel:feature": r.feature_id} if r.feature_id else {})}),
            "x_ProductId": r.product_id, "x_FeatureId": r.feature_id, "x_Stage": r.stage,
            "x_CostCategory": r.category, "x_Environment": r.env, "x_Basis": r.basis, "x_Status": r.status,
            "x_Source": r.source})
    return out


def focus_csv(records: Iterable[CostRecord]) -> str:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=FOCUS_COLUMNS, lineterminator="\n")
    w.writeheader()
    w.writerows(focus_rows(records))
    return buf.getvalue()

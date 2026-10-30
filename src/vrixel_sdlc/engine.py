"""FeatureEngine: start features, list the gate inbox, record human decisions, resume threads."""

from __future__ import annotations

import threading
from typing import Protocol

from langgraph.types import Command

from .graph import build_pipeline
from .graph.pipeline import normalize
from .gateway import summarize
from .services import Services


class FeatureIndex(Protocol):
    def add(self, feature_id: str, product_id: str, title: str, seq: int) -> None: ...
    def next_number(self, product_id: str) -> int: ...
    def all(self) -> list[dict]: ...


class InMemoryIndex:
    def __init__(self) -> None:
        self._rows: list[dict] = []
        self._lock = threading.Lock()

    def add(self, feature_id, product_id, title, seq) -> None:
        with self._lock:
            self._rows.append({"feature_id": feature_id, "product_id": product_id, "title": title, "seq": seq})

    def next_number(self, product_id) -> int:
        with self._lock:
            return 1 + max((r["seq"] for r in self._rows if r["product_id"] == product_id), default=0)

    def all(self) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._rows]


class PostgresIndex:
    DDL = """
    CREATE TABLE IF NOT EXISTS vrixel_features (
      feature_id TEXT PRIMARY KEY, product_id TEXT NOT NULL, title TEXT NOT NULL,
      seq INT NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    );
    """

    def __init__(self, pool) -> None:
        self.pool = pool
        with self.pool.connection() as conn:
            conn.execute(self.DDL)

    def add(self, feature_id, product_id, title, seq) -> None:
        with self.pool.connection() as conn:
            conn.execute("INSERT INTO vrixel_features (feature_id, product_id, title, seq) VALUES (%s, %s, %s, %s)",
                         (feature_id, product_id, title, seq))

    def next_number(self, product_id) -> int:
        with self.pool.connection() as conn:
            row = conn.execute("SELECT COALESCE(MAX(seq), 0) + 1 AS n FROM vrixel_features WHERE product_id = %s",
                               (product_id,)).fetchone()
        return int(row["n"])

    def all(self) -> list[dict]:
        with self.pool.connection() as conn:
            rows = conn.execute("SELECT feature_id, product_id, title, seq FROM vrixel_features ORDER BY created_at").fetchall()
        return [dict(r) for r in rows]


class FeatureEngine:
    def __init__(self, services: Services, checkpointer, index: FeatureIndex | None = None) -> None:
        self.services = services
        self.graph = build_pipeline(services, checkpointer=checkpointer)
        self.index = index or InMemoryIndex()

    @staticmethod
    def _config(feature_id: str) -> dict:
        return {"configurable": {"thread_id": feature_id}, "recursion_limit": 200}

    def start(self, *, product_id: str, title: str, description: str = "",
              simulate_faults: dict[str, int] | None = None, budget_usd: float | None = None) -> dict:
        self.services.settings.product(product_id)  # validates product id
        seq = self.index.next_number(product_id)
        feature_id = f"{product_id}-FEAT-{seq}"
        self.index.add(feature_id, product_id, title, seq)
        inp = {"feature_id": feature_id, "product_id": product_id, "title": title, "description": description,
               "simulate_faults": dict(simulate_faults or {})}
        if budget_usd:
            inp["token_budget_usd"] = float(budget_usd)
        self.graph.invoke(inp, self._config(feature_id))
        return self.view(feature_id)

    def pending(self, feature_id: str) -> list[dict]:
        snap = self.graph.get_state(self._config(feature_id))
        return [{"interrupt_id": i.id, **i.value} for i in snap.interrupts]

    def decide(self, feature_id: str, interrupt_id: str, decision: dict) -> dict:
        pending = {p["interrupt_id"]: p for p in self.pending(feature_id)}
        if interrupt_id not in pending:
            raise KeyError(f"No pending interrupt {interrupt_id} on {feature_id}")
        d = normalize(decision, tuple(pending[interrupt_id]["allowed"]))  # validate before resuming
        self.graph.invoke(Command(resume={interrupt_id: d}), self._config(feature_id))
        return self.view(feature_id)

    def view(self, feature_id: str) -> dict:
        snap = self.graph.get_state(self._config(feature_id))
        s = snap.values
        if not s:
            raise KeyError(f"Unknown feature {feature_id}")
        records = self.services.ledger.records(feature_id)
        human = sum(g.get("active_minutes", 0.0) for g in s.get("gate_decisions", []))
        return {
            "feature_id": feature_id,
            "product_id": s.get("product_id"),
            "title": s.get("title"),
            "status": s.get("status"),
            "constitution_version": s.get("constitution_version"),
            "phase_status": s.get("phase_status", {}),
            "scores": s.get("scores", {}),
            "loop_count": s.get("loop_count", {}),
            "risk_tier": s.get("risk_tier"),
            "artifacts": {k: v["id"] for k, v in s.get("artifacts", {}).items()},
            "pull_requests": s.get("pull_requests", {}),
            "change_set": s.get("change_set", {}),
            "release": s.get("release", {}),
            "gate_decisions": s.get("gate_decisions", []),
            "pending": [{"interrupt_id": i.id, **i.value} for i in snap.interrupts],
            "cost_usd": round(float(s.get("cost_usd", 0.0)), 4),
            "budget_usd": s.get("token_budget_usd"),
            "tokens_used": s.get("tokens_used", 0),
            "human_active_minutes": human,
            "usage": summarize(records),
            "events": s.get("events", []),
        }

    def inbox(self, product_id: str | None = None) -> list[dict]:
        out = []
        for row in self.index.all():
            if product_id and row["product_id"] != product_id:
                continue
            for p in self.pending(row["feature_id"]):
                out.append({"feature_id": row["feature_id"], "title": row["title"], **p})
        return out

    def auto_drive(self, feature_id: str, *, approver: str = "auto-approver", decisions: dict | None = None,
                   max_steps: int = 50) -> dict:
        """Demo and test helper: answer every pending interrupt until the thread stops.
        `decisions` maps a gate or 'escalation:<phase>' to a decision dict used once."""
        scripted = {k: list(v) if isinstance(v, list) else [v] for k, v in (decisions or {}).items()}
        for _ in range(max_steps):
            pending = self.pending(feature_id)
            if not pending:
                break
            p = pending[0]
            key = p.get("gate") if p["type"] in ("gate", "handback") else f"escalation:{p['phase']}"
            if p["type"] == "handback":
                d = {"decision": "handback"}
            elif scripted.get(key):
                d = scripted[key].pop(0)
            elif p["type"] == "gate":
                d = {"decision": "approve"}
            else:
                d = {"decision": "resolved", "feedback": "resolved by a human"}
            self.decide(feature_id, p["interrupt_id"], {"approver": approver, "active_minutes": 5, **d})
        return self.view(feature_id)

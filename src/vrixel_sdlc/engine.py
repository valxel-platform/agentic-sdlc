"""FeatureEngine: start features, list the gate inbox, record human decisions, resume threads."""

from __future__ import annotations

import os
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Protocol

from langgraph.types import Command

from .events import EventBus
from .graph import build_pipeline
from .graph.pipeline import normalize
from .gateway import summarize
from .services import Services
from .worksync import InMemoryLinkStore, WorkSync


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
    def __init__(self, services: Services, checkpointer, index: FeatureIndex | None = None,
                 bus: EventBus | None = None, workers: int = 4, work: WorkSync | None = None) -> None:
        self.services = services
        self.graph = build_pipeline(services, checkpointer=checkpointer)
        self.index = index or InMemoryIndex()
        self.bus = bus or EventBus()
        self._pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="feature")
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()
        self.running: set[str] = set()
        self.errors: dict[str, str] = {}
        self.work = work or WorkSync(services, InMemoryLinkStore(),
                                     os.environ.get("VRIXEL_PUBLIC_URL") or "http://localhost:8080")
        self.sync_errors: dict[str, str] = {}

    @staticmethod
    def _config(feature_id: str) -> dict:
        return {"configurable": {"thread_id": feature_id}, "recursion_limit": 200}

    def _lock(self, feature_id: str) -> threading.Lock:
        with self._locks_guard:
            return self._locks.setdefault(feature_id, threading.Lock())

    def _execute(self, feature_id: str, payload) -> None:
        """Run the thread until it finishes or waits on a human, publishing each node as it ends."""
        with self._lock(feature_id):
            self.running.add(feature_id)
            self.errors.pop(feature_id, None)
            self.bus.publish({"type": "feature.running", "feature_id": feature_id})
            try:
                for chunk in self.graph.stream(payload, self._config(feature_id), stream_mode="updates"):
                    for node in chunk:
                        if node != "__interrupt__":
                            self.bus.publish({"type": "feature.node", "feature_id": feature_id, "node": node})
            except Exception as exc:  # surfaced on the feature page; the checkpoint keeps the last good step
                self.errors[feature_id] = f"{type(exc).__name__}: {exc}"
                raise
            finally:
                self.running.discard(feature_id)
                self.sync_work(feature_id)
                self.bus.publish({"type": "feature.updated", "feature_id": feature_id,
                                  "pending": len(self.pending(feature_id))})

    def sync_work(self, feature_id: str) -> dict:
        """Bring the product's work tracker up to date. Errors are recorded, never raised: the
        next run of the feature, or POST /api/features/{id}/sync, catches up."""
        try:
            s = self.graph.get_state(self._config(feature_id)).values
            result = self.work.sync(s) if s else {}
            self.sync_errors.pop(feature_id, None)
            return result
        except Exception as exc:
            self.sync_errors[feature_id] = f"{type(exc).__name__}: {exc}"
            return {"error": self.sync_errors[feature_id]}

    def _submit(self, feature_id: str, payload) -> None:
        def run():
            try:
                self._execute(feature_id, payload)
            except Exception:
                pass  # recorded in self.errors by _execute
        self._pool.submit(run)

    def create(self, *, product_id: str, title: str, description: str = "", created_by: str = "",
               simulate_faults: dict[str, int] | None = None, budget_usd: float | None = None,
               background: bool = False) -> str:
        # Read and validate the product's governance from its own org, and pin the commits.
        snap = self.services.snapshot(product_id)
        seq = self.index.next_number(product_id)
        feature_id = f"{product_id}-FEAT-{seq}"
        self.index.add(feature_id, product_id, title, seq)
        inp = {"feature_id": feature_id, "product_id": product_id, "title": title, "description": description,
               "created_by": created_by, "simulate_faults": dict(simulate_faults or {}), "governance": snap.pins}
        if budget_usd:
            inp["token_budget_usd"] = float(budget_usd)
        self.bus.publish({"type": "feature.created", "feature_id": feature_id, "product_id": product_id})
        if background:
            self._submit(feature_id, inp)
        else:
            self._execute(feature_id, inp)
        return feature_id

    def start(self, **kwargs) -> dict:
        """Create a feature and run it until its first human gate (synchronous)."""
        return self.view(self.create(**kwargs))

    def pending(self, feature_id: str) -> list[dict]:
        snap = self.graph.get_state(self._config(feature_id))
        return [{"interrupt_id": i.id, **i.value} for i in snap.interrupts]

    def item(self, feature_id: str, interrupt_id: str) -> dict:
        for p in self.pending(feature_id):
            if p["interrupt_id"] == interrupt_id:
                return p
        raise KeyError(f"No pending interrupt {interrupt_id} on {feature_id}")

    def decide(self, feature_id: str, interrupt_id: str, decision: dict, *, background: bool = False) -> dict:
        if feature_id in self.running:
            raise RuntimeError(f"{feature_id} is still running; decide once it is waiting")
        item = self.item(feature_id, interrupt_id)
        d = normalize(decision, tuple(item["allowed"]))  # validate before resuming
        cmd = Command(resume={interrupt_id: d})
        if background:
            self._submit(feature_id, cmd)
            return {"feature_id": feature_id, "accepted": True}
        self._execute(feature_id, cmd)
        return self.view(feature_id)

    def view(self, feature_id: str) -> dict:
        snap = self.graph.get_state(self._config(feature_id))
        s = snap.values
        if not s:
            raise KeyError(f"Unknown feature {feature_id}")
        records = self.services.ledger.records(feature_id)
        human = sum(g.get("active_minutes", 0.0) for g in s.get("gate_decisions", []))
        pending = [{"interrupt_id": i.id, **i.value} for i in snap.interrupts]
        return {
            "feature_id": feature_id,
            "product_id": s.get("product_id"),
            "title": s.get("title"),
            "description": s.get("description", ""),
            "created_by": s.get("created_by", ""),
            "status": s.get("status"),
            "running": feature_id in self.running,
            "error": self.errors.get(feature_id),
            "rail": rail(s, pending, running=feature_id in self.running),
            "constitution_version": s.get("constitution_version"),
            "governance": s.get("governance"),
            "phase_status": s.get("phase_status", {}),
            "scores": s.get("scores", {}),
            "loop_count": s.get("loop_count", {}),
            "risk_tier": s.get("risk_tier"),
            "lanes_touched": s.get("lanes_touched", []),
            "artifacts": {k: v["id"] for k, v in s.get("artifacts", {}).items()},
            "pull_requests": s.get("pull_requests", {}),
            "change_set": s.get("change_set", {}),
            "release": s.get("release", {}),
            "gate_decisions": s.get("gate_decisions", []),
            "pending": pending,
            "cost_usd": round(float(s.get("cost_usd", 0.0)), 4),
            "budget_usd": s.get("token_budget_usd"),
            "tokens_used": s.get("tokens_used", 0),
            "human_active_minutes": human,
            "usage": summarize(records),
            "events": s.get("events", []),
            "diagrams": s.get("diagrams", []),
            "work_tracking": self._work_view(s),
        }

    def _work_view(self, s: dict) -> dict | None:
        try:
            cfg, _ = self.work.config(s)
        except Exception:
            return None
        if not cfg:
            return None
        fid = s["feature_id"]
        return {"provider": cfg["provider"], "project": cfg.get("project"),
                "items": self.work.items(fid), "error": self.sync_errors.get(fid)}

    def summary(self, feature_id: str) -> dict:
        """The board's light view of a feature."""
        v = self.view(feature_id)
        keep = ("feature_id", "product_id", "title", "created_by", "status", "running", "error", "rail",
                "risk_tier", "lanes_touched", "cost_usd", "budget_usd")
        out = {k: v[k] for k in keep}
        out["pending"] = [{k: p.get(k) for k in ("interrupt_id", "type", "gate", "phase", "lane", "approvers")}
                          for p in v["pending"]]
        out["updated_at"] = v["events"][-1]["at"] if v["events"] else None
        return out

    def artifact(self, feature_id: str, kind: str) -> dict:
        s = self.graph.get_state(self._config(feature_id)).values
        ref = (s.get("artifacts") or {}).get(kind)
        if not ref:
            raise KeyError(f"{feature_id} has no {kind} artifact")
        return {**ref, "content": self.services.artifacts.load(ref)}

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


RAIL = [("discovery", "phase"), ("g1", "gate"), ("spec", "phase"), ("g2", "gate"), ("design", "phase"),
        ("g3", "gate"), ("architecture", "phase"), ("g4", "gate"), ("plan", "phase"), ("build", "phase"),
        ("g5", "gate"), ("release", "phase"), ("g6", "gate"), ("operate", "phase")]


def rail(s: dict, pending: list[dict], *, running: bool) -> list[dict]:
    """Each station's state for the UI: done, skipped, waiting (on a human), current, stopped or todo."""
    status = s.get("status")
    phase_status, approvals = s.get("phase_status") or {}, s.get("approvals") or {}
    waiting_gates = {p.get("gate") for p in pending if p["type"] in ("gate", "handback")}
    waiting_phases = {p.get("phase") for p in pending if p["type"] == "escalation"}
    merged = (s.get("change_set") or {}).get("merged")
    stations = []
    for key, kind in RAIL:
        if kind == "gate":
            if key in waiting_gates:
                st = "waiting"
            elif approvals.get(key):
                st = "done"
            elif key == "g5" and merged:
                st = "skipped"  # low-risk change sets auto-merge
            else:
                st = "todo"
        else:
            ps = phase_status.get(key)
            if key in waiting_phases:
                st = "waiting"
            elif ps in ("passed", "resolved"):
                st = "done"
            else:
                st = "todo"
        stations.append({"key": key, "kind": kind, "state": st})
    if running and not any(x["state"] == "waiting" for x in stations):
        nxt = next((x for x in stations if x["state"] == "todo"), None)
        if nxt:
            nxt["state"] = "current"
    if status == "aborted":
        nxt = next((x for x in stations if x["state"] in ("todo", "waiting")), None)
        if nxt:
            nxt["state"] = "stopped"
    return stations

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
from .waits import WaitChecker
from .worksync import InMemoryLinkStore, WorkSync


KINDS = {"FEAT": "feature", "INI": "initiative", "REL": "release"}


def kind_of(thread_id: str) -> str:
    """EMR-FEAT-3 -> feature, EMR-INI-1 -> initiative, EMR-REL-2 -> release."""
    parts = thread_id.split("-")
    return KINDS.get(parts[1], "feature") if len(parts) >= 3 else "feature"


class FeatureIndex(Protocol):
    """Every thread the engine has started: features, initiatives and releases."""

    def add(self, feature_id: str, product_id: str, title: str, seq: int, kind: str = "feature") -> None: ...
    def next_number(self, product_id: str, kind: str = "feature") -> int: ...
    def all(self) -> list[dict]: ...


class InMemoryIndex:
    def __init__(self) -> None:
        self._rows: list[dict] = []
        self._lock = threading.Lock()

    def add(self, feature_id, product_id, title, seq, kind="feature") -> None:
        with self._lock:
            self._rows.append({"feature_id": feature_id, "product_id": product_id, "title": title, "seq": seq,
                               "kind": kind})

    def next_number(self, product_id, kind="feature") -> int:
        with self._lock:
            return 1 + max((r["seq"] for r in self._rows if r["product_id"] == product_id and r["kind"] == kind),
                           default=0)

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
            # Initiatives and releases share the index (added after features; existing rows are features).
            conn.execute("ALTER TABLE vrixel_features ADD COLUMN IF NOT EXISTS kind TEXT NOT NULL DEFAULT 'feature'")

    def add(self, feature_id, product_id, title, seq, kind="feature") -> None:
        with self.pool.connection() as conn:
            conn.execute("INSERT INTO vrixel_features (feature_id, product_id, title, seq, kind) "
                         "VALUES (%s, %s, %s, %s, %s)", (feature_id, product_id, title, seq, kind))

    def next_number(self, product_id, kind="feature") -> int:
        with self.pool.connection() as conn:
            row = conn.execute("SELECT COALESCE(MAX(seq), 0) + 1 AS n FROM vrixel_features "
                               "WHERE product_id = %s AND kind = %s", (product_id, kind)).fetchone()
        return int(row["n"])

    def all(self) -> list[dict]:
        with self.pool.connection() as conn:
            rows = conn.execute("SELECT feature_id, product_id, title, seq, kind FROM vrixel_features "
                                "ORDER BY created_at").fetchall()
        return [dict(r) for r in rows]


class FeatureEngine:
    def __init__(self, services: Services, checkpointer, index: FeatureIndex | None = None,
                 bus: EventBus | None = None, workers: int = 4, work: WorkSync | None = None) -> None:
        self.services = services
        from .roadmap.graphs import build_initiative_graph, build_release_graph

        self.graph = build_pipeline(services, checkpointer=checkpointer)
        self.graphs = {"feature": self.graph,
                       "initiative": build_initiative_graph(services, checkpointer=checkpointer),
                       "release": build_release_graph(services, checkpointer=checkpointer)}
        self.index = index or InMemoryIndex()
        self._create_lock = threading.Lock()
        services.features = self.feature_summaries  # what the product has built, for agents and G4
        services.initiatives = self.initiative_summaries
        services.feature_titles = lambda ids: {i: (self._state(i).get("title") or i) for i in ids}
        services.release_waits = self._release_waits
        services.release_for = self.release_for
        services.roadmap_fit = self.roadmap_fit
        self.bus = bus or EventBus()
        self._pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="feature")
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()
        self.running: set[str] = set()
        self.errors: dict[str, str] = {}
        self.work = work or WorkSync(services, InMemoryLinkStore(),
                                     os.environ.get("VRIXEL_PUBLIC_URL") or "http://localhost:8080")
        self.sync_errors: dict[str, str] = {}
        self.waits = WaitChecker(services)
        self._watcher: threading.Thread | None = None
        self._stop = threading.Event()

    def _g(self, thread_id: str):
        return self.graphs[kind_of(thread_id)]

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
                while payload is not None:
                    for chunk in self._g(feature_id).stream(payload, self._config(feature_id), stream_mode="updates"):
                        for node in chunk:
                            if node != "__interrupt__":
                                self.bus.publish({"type": "feature.node", "feature_id": feature_id, "node": node})
                    payload = self._ready_wait(feature_id)  # CI or a deploy already done: carry on
            except Exception as exc:  # surfaced on the feature page; the checkpoint keeps the last good step
                self.errors[feature_id] = f"{type(exc).__name__}: {exc}"
                raise
            finally:
                self.running.discard(feature_id)
                self.sync_work(feature_id)
                self.bus.publish({"type": "feature.updated", "feature_id": feature_id,
                                  "pending": len(self.pending(feature_id))})
        self._wake_release_waits(feature_id)

    def _flag_on(self, s: dict) -> bool | None:
        """Whether the feature is visible to users: its flag as the flag service has it now."""
        rel = s.get("release") or {}
        if not rel.get("flag"):
            return None
        on = self.services.flags.get(s["product_id"], rel["flag"])
        return rel.get("flag_on") if on is None else on

    def _wake_release_waits(self, thread_id: str) -> None:
        """A feature reaching its train or production, or a release being approved, can end another
        thread's release wait in the same product: check those now instead of on the watcher's tick."""
        product_id = thread_id.split("-", 1)[0]
        for row in self.index.all():
            other = row["feature_id"]
            if row["product_id"] != product_id or other == thread_id or other in self.running:
                continue
            if kind_of(thread_id) == "feature" and row.get("kind") != "release":
                continue  # a feature's progress only matters to releases
            try:
                waiting = any(p.get("wait") in ("release_ready", "release_train") for p in self.pending(other))
                cmd = self._ready_wait(other) if waiting else None
            except Exception:
                continue
            if cmd is not None:
                try:
                    self._execute(other, cmd)
                except Exception:
                    pass  # recorded in self.errors

    # --- external waits (CI, OpenChoreo builds and deployments) ---
    def _ready_wait(self, feature_id: str) -> Command | None:
        for p in self.pending(feature_id):
            if p["type"] == "external":
                res = self.waits.check(p)
                if res is not None:
                    return Command(resume={p["interrupt_id"]: res})
        return None

    def poke(self, feature_id: str) -> bool:
        """Check a feature's external wait now (the watcher's tick, or a GitHub webhook). Resumes
        in the background when the wait is over; returns whether it did."""
        if feature_id in self.running:
            return False
        try:
            cmd = self._ready_wait(feature_id)
        except Exception:
            return False
        if cmd is None:
            return False
        self._submit(feature_id, cmd)
        return True

    def waiting_on_external(self) -> list[str]:
        out = []
        for row in self.index.all():
            try:
                if any(p["type"] == "external" for p in self.pending(row["feature_id"])):
                    out.append(row["feature_id"])
            except Exception:
                continue
        return out

    def start_watcher(self, interval_s: float = 30.0) -> None:
        """Poll features that wait on CI or OpenChoreo. One replica runs it (see README)."""
        if self._watcher is not None:
            return

        def loop():
            while not self._stop.wait(interval_s):
                for fid in self.waiting_on_external():
                    self.poke(fid)

        self._watcher = threading.Thread(target=loop, name="vrixel-watcher", daemon=True)
        self._watcher.start()

    def stop_watcher(self) -> None:
        self._stop.set()

    def sync_work(self, feature_id: str) -> dict:
        """Bring the product's work tracker up to date. Errors are recorded, never raised: the
        next run of the feature, or POST /api/features/{id}/sync, catches up."""
        try:
            s = self._state(feature_id)
            kind = kind_of(feature_id)
            result = ({} if not s else self.work.sync(s) if kind == "feature" else
                      self.work.sync_initiative(s) if kind == "initiative" else self.work.sync_release(s))
            self.sync_errors.pop(feature_id, None)
            return result
        except Exception as exc:
            self.sync_errors[feature_id] = f"{type(exc).__name__}: {exc}"
            return {"error": self.sync_errors[feature_id]}

    def _submit(self, feature_id: str, payload) -> None:
        # Running from the moment the work is queued, not when a worker picks it up: otherwise a
        # caller (or a second decision) can see the old state between the two.
        self.running.add(feature_id)

        def run():
            try:
                self._execute(feature_id, payload)
            except Exception:
                pass  # recorded in self.errors by _execute
        self._pool.submit(run)

    def feature_summaries(self, product_id: str) -> list[dict]:
        """Every feature of a product the pipeline has run, with where it got to: released, in
        progress (and at which station), or stopped. Read from the durable index and checkpoints,
        so it survives restarts."""
        out = []
        for row in self.index.all():
            if row["product_id"] != product_id or row.get("kind", "feature") != "feature":
                continue
            snap = self.graph.get_state(self._config(row["feature_id"]))
            s = snap.values or {}
            pending = [{"interrupt_id": i.id, **i.value} for i in snap.interrupts]
            stations = rail(s, pending, running=row["feature_id"] in self.running) if s else []
            stage = next((x["key"] for x in stations if x["state"] in ("waiting", "current", "stopped")), "")
            status = ("stopped" if s.get("status") == "aborted" else
                      "released" if s.get("status") == "done" or (s.get("release") or {}).get("production") else
                      "in_progress")
            out.append({"feature_id": row["feature_id"], "title": s.get("title") or row["title"],
                        "description": s.get("description", ""), "status": status,
                        "initiative_id": s.get("initiative_id", ""), "cost_usd": float(s.get("cost_usd", 0.0)),
                        "flag_on": self._flag_on(s),
                        "stage": "" if status != "in_progress" else stage.upper() if stage.startswith("g") else stage})
        return out

    # --- roadmap: initiatives and releases ------------------------------------------------
    def _state(self, thread_id: str) -> dict:
        return self._g(thread_id).get_state(self._config(thread_id)).values or {}

    def _rows(self, product_id: str, kind: str) -> list[dict]:
        return [r for r in self.index.all() if r["product_id"] == product_id and r.get("kind", "feature") == kind]

    def _new_id(self, product_id: str, kind: str, title: str) -> str:
        tag = {v: k for k, v in KINDS.items()}[kind]
        with self._create_lock:
            seq = self.index.next_number(product_id, kind)
            thread_id = f"{product_id}-{tag}-{seq}"
            self.index.add(thread_id, product_id, title, seq, kind)
        return thread_id

    def initiative_summaries(self, product_id: str) -> list[dict]:
        out = []
        for row in self._rows(product_id, "initiative"):
            s = self._state(row["feature_id"])
            d = s.get("draft") or {}
            out.append({"id": row["feature_id"], "title": s.get("title") or row["title"],
                        "status": s.get("status", "drafting"), "outcome": d.get("outcome", s.get("request", "")),
                        "goal": d.get("goal") or ((d.get("new_goal") or {}).get("title", "")), "horizon": d.get("horizon", ""),
                        "budget_usd": d.get("budget_usd", 0.0), "lanes": d.get("lanes", []),
                        "release_model": d.get("release_model", "launch"), "candidates": d.get("candidates", []),
                        "record_pr": (s.get("record_pr") or {}).get("url", "")})
        return out

    def create_initiative(self, *, product_id: str, title: str, request: str, created_by: str = "",
                          measure: str = "", horizon: str = "", background: bool = False) -> str:
        self.services.snapshot(product_id)  # validates the product's governance
        iid = self._new_id(product_id, "initiative", title)
        inp = {"initiative_id": iid, "product_id": product_id, "title": title, "request": request,
               "created_by": created_by, "measure_hint": measure, "horizon_hint": horizon, "attempt": 0}
        self.bus.publish({"type": "feature.created", "feature_id": iid, "product_id": product_id})
        (self._submit if background else self._execute)(iid, inp)
        return iid

    def approved_initiative(self, product_id: str, initiative_id: str) -> dict | None:
        """An initiative features may link to: approved at G0 (record merged or still a PR)."""
        for i in self.initiative_summaries(product_id):
            if i["id"] == initiative_id:
                return i if i["status"] == "approved" else None
        from .roadmap import read_roadmap

        try:
            for i in read_roadmap(self.services, product_id)["initiatives"]:
                if i["id"] == initiative_id and i["status"] in ("approved", "active"):
                    return i
        except Exception:
            return None
        return None

    def roadmap_fit(self, state: dict) -> dict:
        """For G1: the initiative a feature serves and what is left of its envelope, or 'unplanned'."""
        iid = state.get("initiative_id")
        if not iid:
            return {"unplanned": True}
        i = self.approved_initiative(state["product_id"], iid) or {"id": iid, "title": iid}
        spent = sum(f["cost_usd"] for f in self.feature_summaries(state["product_id"]) if f.get("initiative_id") == iid)
        envelope = float(i.get("budget_usd") or 0)
        return {"initiative": {k: i.get(k) for k in ("id", "title", "goal", "horizon", "release_model")},
                "budget_usd": envelope, "spent_usd": round(spent, 2), "remaining_usd": round(envelope - spent, 2),
                "over": bool(envelope) and spent > envelope}

    def release_for(self, feature_id: str, *, active: bool = False) -> dict | None:
        """The latest release a feature was put in. Cancelled ones count (a feature's G6 replays the
        same steps on resume), unless `active` asks for releases still under way."""
        product_id = feature_id.split("-", 1)[0]
        found = None
        for row in self._rows(product_id, "release"):
            s = self._state(row["feature_id"])
            if feature_id in (s.get("features") or []) and not (active and s.get("status") == "cancelled"):
                found = {"id": row["feature_id"], "model": s.get("model", "launch"), "status": s.get("status", "")}
        return found

    def create_release(self, *, product_id: str, title: str, model: str, features: list[str], target_date: str = "",
                       initiative: str = "", created_by: str = "", background: bool = False) -> str:
        if model not in ("launch", "train"):
            raise ValueError("model must be launch (default) or train")
        if not features:
            raise ValueError("A release needs at least one feature")
        known = {f["feature_id"]: f for f in self.feature_summaries(product_id)}
        for fid in features:
            f = known.get(fid)
            if not f:
                raise ValueError(f"{fid} is not a feature of {product_id}")
            if f["status"] != "in_progress":
                raise ValueError(f"{fid} is {f['status']}; only features still in progress can join a release")
            if (self._state(fid).get("approvals") or {}).get("g6"):
                raise ValueError(f"{fid} has already passed G6")
            other = self.release_for(fid, active=True)
            if other:
                raise ValueError(f"{fid} is already in {other['id']}")
        rid = self._new_id(product_id, "release", title)
        inp = {"release_id": rid, "product_id": product_id, "title": title, "model": model,
               "features": list(dict.fromkeys(features)), "target_date": target_date, "initiative": initiative,
               "created_by": created_by}
        self.bus.publish({"type": "feature.created", "feature_id": rid, "product_id": product_id})
        (self._submit if background else self._execute)(rid, inp)
        return rid

    def _release_waits(self, packet: dict) -> dict | None:
        if packet["wait"] == "release_train":  # a feature waiting for its train's approval
            s = self._state(packet["release_id"])
            if s.get("status") in ("approved", "shipped"):
                return {"status": "success", "approver": s.get("approved_by", ""), "release_id": packet["release_id"]}
            if s.get("status") == "cancelled":
                return {"status": "failure", "reason": f"{packet['release_id']} was cancelled"}
            return None
        # release_ready: every member is ready (launch: in production, flag off; train: waiting at
        # its train after staging) or was stopped.
        ready, dropped, lanes, flags, evidence = [], [], set(), {}, []
        for fid in packet["features"]:
            s = self._state(fid)
            if s.get("status") == "aborted":
                dropped.append(fid)
                continue
            if packet["model"] == "launch":
                ok = bool((s.get("release") or {}).get("production"))
            else:
                ok = any(p.get("wait") == "release_train" for p in self.pending(fid))
            if not ok:
                return None
            ready.append(fid)
            lanes |= set(s.get("lanes_touched") or [])
            if (s.get("release") or {}).get("flag"):
                flags[fid] = s["release"]["flag"]
            suites = [r for q in (s.get("quality") or {}).values() for r in q.get("suites", [])]
            passed = sum(1 for r in suites if r.get("state") == "passed")
            evidence.append(f"- {fid} {s.get('title', '')}: {len(s.get('pull_requests') or {})} pull requests, "
                            f"{passed}/{len(suites)} suites passed, {len(s.get('waivers') or [])} accepted risks, "
                            + (f"G5 by {g5}" if (g5 := ', '.join(g['approver'] for g in s.get('gate_decisions', []) if g['gate'] == 'g5'))
                               else f"no G5 needed (risk tier {s.get('risk_tier') or 'low'})"))
        return {"status": "success", "dropped": dropped, "lanes": sorted(lanes), "flags": flags,
                "evidence": "\n".join(evidence)}

    def similar(self, product_id: str, title: str, description: str = "", exclude: str = "") -> list[dict]:
        from .duplicates import find_similar

        return find_similar(title, description, self.feature_summaries(product_id),
                            self.services.catalog.lookup(product_id), exclude=exclude)

    def create(self, *, product_id: str, title: str, description: str = "", created_by: str = "",
               simulate_faults: dict[str, int] | None = None, budget_usd: float | None = None,
               background: bool = False, not_duplicate_reason: str = "", initiative_id: str = "") -> str:
        from .duplicates import DuplicateIntent

        # Read and validate the product's governance from its own org, and pin the commits.
        snap = self.services.snapshot(product_id)
        if initiative_id and not self.approved_initiative(product_id, initiative_id):
            raise ValueError(f"{initiative_id} is not an approved initiative of {product_id}")
        with self._create_lock:  # two identical requests at once must not both pass the check
            similar = self.similar(product_id, title, description)
            if any(m["duplicate"] for m in similar) and not not_duplicate_reason.strip():
                raise DuplicateIntent(similar)
            seq = self.index.next_number(product_id)
            feature_id = f"{product_id}-FEAT-{seq}"
            self.index.add(feature_id, product_id, title, seq)
        inp = {"feature_id": feature_id, "product_id": product_id, "title": title, "description": description,
               "created_by": created_by, "simulate_faults": dict(simulate_faults or {}), "governance": snap.pins,
               "related_features": similar, "not_duplicate_reason": not_duplicate_reason.strip(),
               "initiative_id": initiative_id}
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
        snap = self._g(feature_id).get_state(self._config(feature_id))
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
        if item["type"] == "external":
            raise RuntimeError(f"{feature_id} is waiting on {item.get('label', 'an external system')}, not a person")
        d = normalize(decision, tuple(item["allowed"]))  # validate before resuming
        if d["decision"] == "accept_risk" and not d["feedback"].strip():
            from .graph import InvalidDecision

            raise InvalidDecision("Accepting a security risk needs a reason")
        cmd = Command(resume={interrupt_id: d})
        if background:
            self._submit(feature_id, cmd)
            return {"feature_id": feature_id, "accepted": True}
        self._execute(feature_id, cmd)
        return self.view(feature_id)

    def view(self, feature_id: str) -> dict:
        if kind_of(feature_id) != "feature":
            from .roadmap.views import thread_view

            return thread_view(self, feature_id)
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
            "release": {**(s.get("release") or {}),
                        **({"flag_on": self._flag_on(s)} if (s.get("release") or {}).get("flag") else {}),
                        "release_id": (s.get("release") or {}).get("release_id")
                        or (self.release_for(feature_id, active=True) or {}).get("id", "")},
            "initiative_id": s.get("initiative_id", ""),
            "gate_decisions": s.get("gate_decisions", []),
            "pending": pending,
            "cost_usd": round(float(s.get("cost_usd", 0.0)), 4),
            "budget_usd": s.get("token_budget_usd"),
            "tokens_used": s.get("tokens_used", 0),
            "human_active_minutes": human,
            "usage": summarize(records),
            "events": s.get("events", []),
            "diagrams": s.get("diagrams", []),
            "delivery": s.get("delivery", {}),
            "deployments": s.get("deployments", {}),
            "runs": (s.get("change_set") or {}).get("runs", {}),
            "quality": s.get("quality", {}),
            "new_repos": s.get("new_repos", []),
            "waivers": s.get("waivers", []),
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
        out["pending"] = [{k: p.get(k) for k in ("interrupt_id", "type", "gate", "phase", "lane", "approvers",
                                                 "wait", "label")}
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
                if p["type"] != "external":  # the inbox is for people
                    out.append({"feature_id": row["feature_id"], "title": row["title"],
                                "kind": row.get("kind", "feature"), **p})
        return out

    def auto_drive(self, feature_id: str, *, approver: str = "auto-approver", decisions: dict | None = None,
                   max_steps: int = 50) -> dict:
        """Demo and test helper: answer every pending interrupt until the thread stops.
        `decisions` maps a gate or 'escalation:<phase>' to a decision dict used once."""
        scripted = {k: list(v) if isinstance(v, list) else [v] for k, v in (decisions or {}).items()}
        for _ in range(max_steps):
            pending = [p for p in self.pending(feature_id) if p["type"] != "external"]
            if not pending:
                break  # done, or waiting on CI or OpenChoreo
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


WAIT_STATION = {"ci": "build", "build": "build", "dev": "build", "staging": "release", "production": "operate"}


def rail(s: dict, pending: list[dict], *, running: bool) -> list[dict]:
    """Each station's state for the UI: done, skipped, waiting (on a human), current, stopped or todo."""
    status = s.get("status")
    phase_status, approvals = s.get("phase_status") or {}, s.get("approvals") or {}
    waiting_gates = {p.get("gate") for p in pending if p["type"] in ("gate", "handback")}
    waiting_phases = {p.get("phase") for p in pending if p["type"] == "escalation"}
    if "provisioning" in waiting_phases:
        waiting_phases.add("plan")
    if "delivery" in waiting_phases:  # a failed build or deploy: show it on the release station
        waiting_phases.add("release")
    for p in pending:  # blocking findings: show them where the scan ran
        if p["type"] == "escalation" and p.get("phase") == "security":
            waiting_phases.add({"pr": "build", "build": "build", "dev": "build", "staging": "release",
                                "production": "operate"}.get(p.get("stage"), "build"))
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
    for p in pending:  # CI or OpenChoreo: work in progress, not a person's turn
        if p["type"] == "external":
            st = next((x for x in stations if x["key"] == WAIT_STATION.get(p.get("wait"), "build")), None)
            if st:
                st["state"] = "current"
    if running and not any(x["state"] in ("waiting", "current") for x in stations):
        nxt = next((x for x in stations if x["state"] == "todo"), None)
        if nxt:
            nxt["state"] = "current"
    if status == "aborted":
        nxt = next((x for x in stations if x["state"] in ("todo", "waiting")), None)
        if nxt:
            nxt["state"] = "stopped"
    return stations

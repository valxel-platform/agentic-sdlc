"""FeatureEngine: start features, list the gate inbox, record human decisions, resume threads."""

from __future__ import annotations

import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Protocol

from datetime import datetime, timedelta, timezone

from langgraph.types import Command

from .cluster import Busy, Cluster, LeaseLost, MemoryCluster, Notes
from .events import EventBus
from .graph import build_pipeline
from .graph.pipeline import normalize
from .gateway import summarize
from .services import Services
from .waits import WaitChecker
from .worksync import InMemoryLinkStore, WorkSync

log = logging.getLogger(__name__)


KINDS = {"FEAT": "feature", "INI": "initiative", "REL": "release", "INC": "incident", "PRD": "inception",
         "REV": "review"}


def kind_of(thread_id: str) -> str:
    """CLN-FEAT-3 -> feature, CLN-INI-1 -> initiative, CLN-REL-2 -> release."""
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


def openchoreo_auth():
    """The tool's OpenChoreo client-credentials token, for OpenChoreo's agents (when configured)."""
    url, secret = os.environ.get("VRIXEL_OPENCHOREO_TOKEN_URL", ""), os.environ.get("VRIXEL_OPENCHOREO_CLIENT_SECRET", "")
    if not (url and secret):
        return None
    from .integrations.mcp_client import ClientCredentials

    return ClientCredentials(url, os.environ.get("VRIXEL_OPENCHOREO_CLIENT_ID") or "service_mcp_client", secret,
                             scope=os.environ.get("VRIXEL_OPENCHOREO_SCOPE", ""))


class FeatureEngine:
    def __init__(self, services: Services, checkpointer, index: FeatureIndex | None = None,
                 bus: EventBus | None = None, workers: int = 4, work: WorkSync | None = None,
                 cluster: Cluster | None = None) -> None:
        self.services = services
        # Several replicas: thread locks, the watcher's leadership and shared notes (cluster.py).
        self.cluster: Cluster = cluster or MemoryCluster()
        from .ops.incident import build_incident_graph
        from .ops.signals import InMemorySignalStore
        from .inception.graph import build_inception_graph
        from .outcomes.review import build_review_graph
        from .roadmap.graphs import build_initiative_graph, build_release_graph

        self.graph = build_pipeline(services, checkpointer=checkpointer)
        self.graphs = {"feature": self.graph,
                       "initiative": build_initiative_graph(services, checkpointer=checkpointer),
                       "release": build_release_graph(services, checkpointer=checkpointer),
                       "incident": build_incident_graph(services, checkpointer=checkpointer),
                       "inception": build_inception_graph(services, checkpointer=checkpointer),
                       "review": build_review_graph(services, checkpointer=checkpointer)}
        if getattr(services, "signals", None) is None:
            services.signals = InMemorySignalStore()
        if getattr(services, "readings", None) is None:
            from .outcomes.store import InMemoryReadingStore

            services.readings = InMemoryReadingStore()
        if getattr(services, "effort", None) is None:
            from .pilot import InMemoryEffortStore

            services.effort = InMemoryEffortStore()
        if getattr(services, "lessons", None) is None:
            from .lessons import InMemoryLessonStore

            services.lessons = InMemoryLessonStore()
        from .ops import cost

        if getattr(services, "costs", None) is None:
            services.costs = cost.default_sources(services.settings)
        if getattr(services, "cost_reviews", None) is None:
            services.cost_reviews = cost.InMemoryCostReviews()
        if getattr(services, "alarms", None) is None:
            from .ops.alarms import InMemoryAlarmBoard

            services.alarms = InMemoryAlarmBoard()
        if getattr(services, "cost_ledger", None) is None:
            from .finops.ledger import InMemoryCostLedger

            services.cost_ledger = InMemoryCostLedger()
        # Days after release when each feature's cost is reviewed against its plan.
        self.cost_review_days = tuple(float(d) for d in (os.environ.get("VRIXEL_COST_REVIEW_DAYS") or "7,30").split(",")
                                      if d.strip())
        self.cost_tolerance = float(os.environ.get("VRIXEL_COST_TOLERANCE") or 0.2)
        self._cost_checked = 0.0
        self.index = index or InMemoryIndex()
        self._create_lock = threading.Lock()
        services.features = self.feature_summaries  # what the product has built, for agents and G4
        services.initiatives = self.initiative_summaries
        services.feature_titles = lambda ids: {i: (self._state(i).get("title") or i) for i in ids}
        services.release_waits = self._release_waits
        services.release_for = self.release_for
        services.roadmap_fit = self.roadmap_fit
        services.incident_signals = lambda iid: services.signals.for_incident(iid)
        services.suspects = self.suspects
        services.feature_state = self._state
        services.flag_of = lambda fid: (self._state(fid).get("release") or {}).get("flag", "")
        services.start_hotfix = self.start_hotfix
        services.create_feature = self.create  # an initiative review opens intents (outcomes/review.py)
        services.start_intent_change = self.start_intent_change
        services.close_incident = lambda iid: services.signals.close(iid)
        self.merged: dict[str, str] = {}  # incident -> the one it was merged into
        services.merged_into = self.merged.get
        services.incidents = self.incident_summaries
        if getattr(services, "changes", None) is None:
            from .ops.changes import InMemoryChangeLog

            services.changes = InMemoryChangeLog()
        from .ops.servicenow import InMemoryReceipts, ServiceNowReceipts, WithReceipts

        if getattr(services, "change_receipts", None) is None:
            services.change_receipts = InMemoryReceipts()
        if not isinstance(services.changes, WithReceipts):
            services.changes = WithReceipts(services.changes, ServiceNowReceipts(
                lambda pid: (services.snapshot(pid).product.tools or {}).get("change_management"),
                services.change_receipts))
        if getattr(services, "inventory_snapshots", None) is None:
            from .ops.inventory import InMemorySnapshots

            services.inventory_snapshots = InMemorySnapshots()
        if getattr(services, "aws_tags", None) is None:
            from .ops.inventory import aws_tags_from_env

            services.aws_tags = aws_tags_from_env()
        self._inventory_day = ""
        self._outcomes_day = ""
        self._envs_swept = 0.0
        if getattr(services, "agents", None) is None:
            from .integrations.openchoreo_agents import from_env as agents_from_env

            services.agents = agents_from_env(services.settings, openchoreo_auth())
        services.remediation_record = self.remediation_record
        services.feature_info = self.feature_info
        if getattr(services, "cohort_logs", None) is None:
            from .ops.cohorts import from_env as cohorts_from_env

            services.cohort_logs = cohorts_from_env(openchoreo_auth())
        if getattr(services, "pager", None) is None:
            from .ops.paging import from_env

            services.pager = from_env(os.environ.get("VRIXEL_PUBLIC_URL") or "http://localhost:8080")
        self.bus = bus or EventBus()
        self._pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="feature")
        self.running: set[str] = set()  # queued or running on this replica
        self.errors = Notes(self.cluster, "error")  # a thread's last error, whichever replica ran it
        self.work = work or WorkSync(services, InMemoryLinkStore(),
                                     os.environ.get("VRIXEL_PUBLIC_URL") or "http://localhost:8080")
        self.sync_errors = Notes(self.cluster, "sync_error")
        services.work_sync = self.work  # G6 reconciles the tracker with the traceability matrix
        self.waits = WaitChecker(services, self.cluster)
        self._watcher: threading.Thread | None = None
        self._stop = threading.Event()

    def _g(self, thread_id: str):
        return self.graphs[kind_of(thread_id)]

    @staticmethod
    def _config(feature_id: str) -> dict:
        return {"configurable": {"thread_id": feature_id}, "recursion_limit": 200}

    def is_running(self, thread_id: str) -> bool:
        """Running (or queued) here, or running on another replica."""
        return thread_id in self.running or self.cluster.busy(thread_id)

    def _execute(self, feature_id: str, payload) -> None:
        """Run the thread until it finishes or waits on a human, publishing each node as it ends.
        Only one replica runs a thread at a time: the one holding its lock."""
        lease = self.cluster.lock(feature_id)
        if lease is None:
            self.running.discard(feature_id)
            raise Busy(f"{feature_id} is running on another replica")
        try:
            self.running.add(feature_id)
            self.errors.pop(feature_id, None)
            self.bus.publish({"type": "feature.running", "feature_id": feature_id})
            try:
                while payload is not None:
                    for chunk in self._g(feature_id).stream(payload, self._config(feature_id), stream_mode="updates"):
                        for node in chunk:
                            if node != "__interrupt__":
                                self.bus.publish({"type": "feature.node", "feature_id": feature_id, "node": node})
                        if not lease.alive():  # another replica may carry it on from the last checkpoint
                            raise LeaseLost(f"{feature_id}: this replica lost the thread's lock")
                    payload = self._ready_wait(feature_id)  # CI or a deploy already done: carry on
            except LeaseLost:
                raise
            except Exception as exc:  # surfaced on the feature page; the checkpoint keeps the last good step
                self.errors[feature_id] = f"{type(exc).__name__}: {exc}"
                raise
            finally:
                self.running.discard(feature_id)
                lease.release()
                self._note_asked(feature_id)
                self.sync_work(feature_id)
                self.bus.publish({"type": "feature.updated", "feature_id": feature_id,
                                  "pending": len(self.pending(feature_id))})
        finally:
            lease.release()
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
            if row["product_id"] != product_id or other == thread_id or self.is_running(other):
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
        if self.is_running(feature_id):
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
        """Poll features that wait on CI or OpenChoreo. Every replica starts it; only the one that
        holds the watcher's lock polls (cluster.py)."""
        if self._watcher is not None:
            return

        def loop():
            import time

            while not self._stop.wait(interval_s):
                if not self.cluster.leader("watcher"):
                    continue  # another replica watches; this one takes over if that one goes
                for fid in self.waiting_on_external():
                    self.poke(fid)
                today = time.strftime("%Y-%m-%d", time.gmtime())
                if today != self._inventory_day:  # one inventory snapshot a day, for the trend
                    self._inventory_day = today
                    try:
                        self.snapshot_inventories()
                    except Exception:
                        pass
                if today != self._outcomes_day:  # readouts and goal trends move daily at most
                    self._outcomes_day = today
                    try:
                        self.collect_outcomes()
                    except Exception:
                        pass
                if time.time() - self._envs_swept > 900:  # change-set environments left behind
                    self._envs_swept = time.time()
                    try:
                        self.sweep_change_set_envs()
                    except Exception:
                        pass
                if time.time() - self._cost_checked > 3600:  # cost data moves daily; hourly is plenty
                    self._cost_checked = time.time()
                    try:
                        self.cluster.prune(30)
                    except Exception:
                        pass
                    for job in (self.collect_costs, self.review_costs):
                        try:
                            job()
                        except Exception:
                            pass

        self._watcher = threading.Thread(target=loop, name="vrixel-watcher", daemon=True)
        self._watcher.start()

    def collect_outcomes(self, product_id: str | None = None) -> list[dict]:
        """Readouts that are due, each goal's weekly trend reading (vrixel_sdlc.outcomes.readouts),
        then the initiative reviews that are due (outcomes/review.py)."""
        from .outcomes import readouts

        taken = readouts.run(self, product_id)
        for pid in [product_id] if product_id else sorted({r["product_id"] for r in self.index.all()}):
            try:
                self.start_due_reviews(pid)
            except Exception:
                continue
        return taken

    def review_summaries(self, product_id: str) -> list[dict]:
        out = []
        for row in self._rows(product_id, "review"):
            s = self._state(row["feature_id"])
            out.append({"id": row["feature_id"], "initiative_id": s.get("initiative_id", ""), "title": s.get("title") or row["title"],
                        "status": s.get("status", "preparing"), "why": s.get("why", ""), "opened": s.get("opened") or []})
        return out

    def create_review(self, *, product_id: str, initiative_id: str, why: str, created_by: str = "vrixel",
                      background: bool = False) -> str:
        """Ask the product council whether an initiative should continue, change course or stop."""
        open_ = [r["id"] for r in self.review_summaries(product_id)
                 if r["initiative_id"] == initiative_id and r["status"] in ("preparing", "waiting")]
        if open_:
            return open_[0]
        from .outcomes.review import initiative_of

        i = initiative_of(self.services, product_id, initiative_id)
        tid = self._new_id(product_id, "review", f"Review: {i['title']}")
        inp = {"review_id": tid, "product_id": product_id, "initiative_id": initiative_id,
               "title": f"Review: {i['title']}", "why": why, "created_by": created_by, "status": "preparing"}
        self.bus.publish({"type": "feature.created", "feature_id": tid, "product_id": product_id})
        (self._submit if background else self._execute)(tid, inp)
        return tid

    def start_due_reviews(self, product_id: str, today=None) -> list[str]:
        from .outcomes import review
        from .outcomes.readouts import product_outcomes
        from .roadmap import read_roadmap

        road = read_roadmap(self.services, product_id)
        outcomes = product_outcomes(self, product_id, road)
        return [self.create_review(product_id=product_id, initiative_id=i["id"], why=why, background=True)
                for i, why in review.due(self.services, product_id, road, outcomes, today)]

    def trace_link(self, trace_id: str) -> str:
        tel = getattr(self.services, "telemetry", None)
        return tel.cfg.link(trace_id) if tel is not None else ""

    def sweep_change_set_envs(self) -> list[str]:
        """Delete change-set environments whose feature stopped, finished or outlived the product's
        time to live (vrixel_sdlc.changeset_env). The merge train deletes them in the normal case."""
        from . import changeset_env

        namespaces, ttl = set(), {}
        for pid in self.services.settings.registry:
            try:
                rt = self.services.snapshot(pid).product.runtime
            except Exception:
                continue
            if rt:
                namespaces.add(rt.get("namespace"))
                ttl[pid] = changeset_env.config(rt)["ttl_hours"]

        def is_active(fid: str):
            pid = fid.rsplit("-FEAT-", 1)[0]
            try:
                st = self._state(fid)
            except Exception:
                return False, ttl.get(pid, changeset_env.DEFAULTS["ttl_hours"])
            env = (st.get("change_set") or {}).get("env") or {}
            active = bool(st) and st.get("status") not in ("done", "aborted") and not env.get("deleted")
            return active, ttl.get(pid, changeset_env.DEFAULTS["ttl_hours"])

        return changeset_env.sweep(self.services, is_active, sorted(namespaces, key=str))

    def stop_watcher(self) -> None:
        self._stop.set()

    def sync_work(self, feature_id: str) -> dict:
        """Bring the product's work tracker up to date. Errors are recorded, never raised: the
        next run of the feature, or POST /api/features/{id}/sync, catches up."""
        try:
            s = self._state(feature_id)
            kind = kind_of(feature_id)
            result = ({} if not s else self.work.sync(s) if kind == "feature" else
                      self.work.sync_initiative(s) if kind == "initiative" else
                      self.work.sync_incident(s) if kind == "incident" else self.work.sync_release(s))
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
        rows = [r for r in self.index.all() if r["product_id"] == product_id and r.get("kind", "feature") == "feature"]
        busy = self.running | self.cluster.busy_among([r["feature_id"] for r in rows])
        for row in rows:
            snap = self.graph.get_state(self._config(row["feature_id"]))
            s = snap.values or {}
            pending = [{"interrupt_id": i.id, **i.value} for i in snap.interrupts]
            stations = rail(s, pending, running=row["feature_id"] in busy) if s else []
            stage = next((x["key"] for x in stations if x["state"] in ("waiting", "current", "stopped")), "")
            status = ("stopped" if s.get("status") == "aborted" else
                      "released" if s.get("status") == "done" or (s.get("release") or {}).get("production") else
                      "in_progress")
            out.append({"feature_id": row["feature_id"], "title": s.get("title") or row["title"],
                        "description": s.get("description", ""), "status": status,
                        "initiative_id": s.get("initiative_id", ""), "cost_usd": float(s.get("cost_usd", 0.0)),
                        "flag_on": self._flag_on(s), "profile": s.get("profile", "feature"),
                        "stage": "" if status != "in_progress" else stage.upper() if stage.startswith("g") else stage})
        return out

    # --- roadmap: initiatives and releases ------------------------------------------------
    def _state(self, thread_id: str) -> dict:
        return self._g(thread_id).get_state(self._config(thread_id)).values or {}

    def inventory(self, product_id: str) -> dict:
        """What the product runs in each environment, read live (ops/inventory.py)."""
        from .ops.inventory import collect

        known: dict = {"components": {}, "resources": {}, "store_apps": {}}
        for row in self._rows(product_id, "feature"):
            s = self._state(row["feature_id"])
            for r in s.get("resources") or []:
                known["resources"].setdefault(r["name"], {"type": r.get("type", ""), "sizes": r.get("sizes") or {},
                                                          "feature_id": row["feature_id"]})
            for key, d in (s.get("deployments") or {}).items():
                if d.get("kind") == "resource":
                    continue
                if d.get("kind") == "store":
                    if d.get("env") == "production" or key not in known["store_apps"]:
                        known["store_apps"][key] = {"env": d.get("env", ""), "version": str(d.get("release") or d.get("sha", ""))[:40],
                                                    "feature_id": row["feature_id"]}
                    continue
                known["components"][d.get("repo") or key] = {
                    "component": d.get("component") or key, "project": d.get("project"), "namespace": d.get("namespace"),
                    "feature_id": row["feature_id"]}  # features in order: the latest release wins
        snap = self.services.snapshot(product_id)
        costs = self.services.cost_ledger.query(product_id=product_id, stage="run") + \
            self.services.cost_ledger.query(product_id=product_id, stage="build")
        return collect(self.services.openchoreo, snap, known, costs=costs, aws=self.services.aws_tags)

    def snapshot_inventories(self, day: str | None = None) -> int:
        """Keep today's counts for every product (the watcher, once a day)."""
        from .ops.inventory import snapshot_row

        n = 0
        for pid in self.services.settings.registry:
            try:
                if self.services.snapshot(pid).product.runtime is None:
                    continue  # a shared lane runs nothing
                row = snapshot_row(self.inventory(pid))
                self.services.inventory_snapshots.put({**row, "day": day or row["day"]})
                n += 1
            except Exception:
                continue
        return n

    def lookback(self, product_id: str, months: int = 6, now=None) -> dict:
        """Incidents and changes per month (ops/lookback.py)."""
        from .ops.lookback import months_back, report

        incidents = [{**self._state(r["feature_id"]), "incident_id": r["feature_id"]}
                     for r in self._rows(product_id, "incident")]
        log = self.services.changes
        return {"product_id": product_id,
                **report(incidents, log.list(product_id), log.links(product_id), months_back(months, now))}

    def _rows(self, product_id: str, kind: str) -> list[dict]:
        return [r for r in self.index.all() if r["product_id"] == product_id and r.get("kind", "feature") == kind]

    def _new_id(self, product_id: str, kind: str, title: str) -> str:
        with self._create_lock:
            return self._new_id_locked(product_id, kind, title)

    def _new_id_locked(self, product_id: str, kind: str, title: str) -> str:
        tag = {v: k for k, v in KINDS.items()}[kind]
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
                          measure: str = "", horizon: str = "", background: bool = False,
                          research: str = "quick") -> str:
        self.services.snapshot(product_id)  # validates the product's governance
        iid = self._new_id(product_id, "initiative", title)
        inp = {"initiative_id": iid, "product_id": product_id, "title": title, "request": request,
               "created_by": created_by, "measure_hint": measure, "horizon_hint": horizon, "attempt": 0,
               "research_depth": research}
        self.bus.publish({"type": "feature.created", "feature_id": iid, "product_id": product_id})
        (self._submit if background else self._execute)(iid, inp)
        return iid

    def resize(self, thread_id: str, *, by: str, background: bool = False) -> str:
        """A feature still at its first gate becomes an initiative, or an initiative not yet approved
        becomes one feature. What was written carries over; the old thread stops, saying where it went."""
        state = self._state(thread_id)
        kind = kind_of(thread_id)
        gate = {"feature": "g1", "initiative": "g0"}.get(kind)
        if gate is None:
            raise ValueError("Only a feature or an initiative can change size")
        waiting = [p for p in self.pending(thread_id) if p.get("gate") == gate]
        if not waiting:
            raise ValueError(f"{thread_id} can change size only while it waits at {gate.upper()}, before it's approved")
        if kind == "feature":
            new = self.create_initiative(product_id=state["product_id"], title=state["title"],
                                         request=state.get("description") or state["title"], created_by=by,
                                         background=background, research="quick")
        else:
            new = self.create(product_id=state["product_id"], title=state["title"], description=state.get("request", ""),
                              created_by=by, background=background,
                              not_duplicate_reason=f"Was initiative {thread_id}")
        self.decide(thread_id, waiting[0]["interrupt_id"],
                    {"decision": "abort", "approver": by,
                     "feedback": f"Became {'initiative' if kind == 'feature' else 'feature'} {new}"},
                    background=background)
        return new

    def change_request(self, feature_id: str, description: str, *, by: str) -> dict:
        """The person who asked changes what they asked for, before the build starts. The new request
        replaces the old; a gate waiting now goes back to its agents with the change as feedback."""
        import difflib as _difflib

        s = self._state(feature_id)
        if (s.get("approvals") or {}).get("g4"):
            raise ValueError("The build has started: start a follow-up feature for the change instead")
        if s.get("status") in ("done", "aborted"):
            raise ValueError("This feature has finished")
        old = s.get("description", "")
        if description.strip() == old.strip():
            raise ValueError("Nothing changed")
        if self.is_running(feature_id):
            raise RuntimeError("Agents are working on it right now: try again when it's waiting")
        added = [ln[2:] for ln in _difflib.ndiff(old.splitlines(), description.splitlines()) if ln.startswith("+ ")]
        removed = [ln[2:] for ln in _difflib.ndiff(old.splitlines(), description.splitlines()) if ln.startswith("- ")]
        note = ("The person who asked for this changed the request."
                + (" Added: " + " / ".join(added) if added else "") + (" Removed: " + " / ".join(removed) if removed else ""))
        _now = lambda: datetime.now(timezone.utc).isoformat()  # noqa: E731

        change = {"description": description.strip(),
                  "events": [{"at": _now(), "node": "request", "kind": "changed", "detail": note[:500], "by": by}]}
        gate = next((p for p in self.pending(feature_id) if p.get("type") == "gate" and "revise" in (p.get("allowed") or [])), None)
        if gate:  # the new request and the send-back in one step, so the agents revise against it
            self.decide(feature_id, gate["interrupt_id"], {"decision": "revise", "approver": by, "feedback": note,
                                                           "reason": "wrong_scope"}, background=True, update=change)
        else:
            self._g(feature_id).update_state(self._config(feature_id), change)
        return {"feature_id": feature_id, "sent_back": gate.get("gate") if gate else "", "note": note}

    # ------------------------------------------------------------------ no dead ends after approval

    def record_prs(self, thread_id: str) -> list[dict]:
        """The governance pull requests an approval opened, and whether each is merged yet."""
        import re as _re

        s = self._state(thread_id)
        out = []
        if s.get("record_pr", {}).get("url"):
            pr = s["record_pr"]
            out.append({"key": "record", "label": "Roadmap record", "url": pr["url"], "merged": bool(pr.get("merged")),
                        "repo": self.services.settings.entry(s["product_id"]).governance.full_name,
                        "number": pr.get("number") or int(_re.search(r"/pull/(\d+)", pr["url"]).group(1))})
        for st in (s.get("setup") or {}).get("steps") or []:
            if st.get("url") and st["step"] in ("governance_pr", "registry_pr"):
                out.append({"key": st["step"], "url": st["url"], "merged": bool(st.get("merged")), "repo": st["repo"],
                            "label": "Governance" if st["step"] == "governance_pr" else "Registry",
                            "number": int(_re.search(r"/pull/(\d+)", st["url"]).group(1))})
        return out

    def merge_record(self, thread_id: str, key: str, *, by: str) -> dict:
        """Merge one of those pull requests from the tool: the person approved it already, so no second
        click on GitHub. Branch protection still applies; GitHub's refusal is passed on."""
        pr = next((p for p in self.record_prs(thread_id) if p["key"] == key), None)
        if pr is None:
            raise KeyError(f"{thread_id} has no {key} pull request")
        if not pr["merged"]:
            res = self.services.github.merge(repo=pr["repo"], number=pr["number"], actor=by)
            if res.get("state") != "merged":
                raise RuntimeError(f"GitHub didn't merge {pr['url']}")
        s = self._state(thread_id)
        if key == "record":
            update = {"record_pr": {**s["record_pr"], "merged": True, "merged_by": by}}
        else:
            setup = dict(s.get("setup") or {})
            setup["steps"] = [{**st, "merged": True, "merged_by": by} if st["step"] == key else st
                              for st in setup.get("steps") or []]
            update = {"setup": setup}
        self._g(thread_id).update_state(self._config(thread_id), update)
        self.bus.publish({"type": "feature.updated", "feature_id": thread_id})
        return {**pr, "merged": True}

    def _queue(self) -> dict:
        import json as _json
        from pathlib import Path as _P

        p = _P(self.services.settings.artifact_dir) / "start-queues.json"
        try:
            return _json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
        except ValueError:
            return {}

    def _save_queue(self, q: dict) -> None:
        import json as _json
        from pathlib import Path as _P

        (_P(self.services.settings.artifact_dir) / "start-queues.json").write_text(_json.dumps(q), encoding="utf-8")

    def start_candidates(self, initiative_id: str, indexes: list[int], *, order: str, by: str) -> dict:
        """Start an approved initiative's candidate features: together, or in order (the next starts
        when the one before reaches everyone)."""
        v = self.view(initiative_id)
        if v["status"] != "approved":
            raise ValueError("Only an approved initiative's features can be started")
        cands = v["candidates"]
        picked = [i for i in dict.fromkeys(indexes) if 0 <= i < len(cands) and not cands[i]["started"]]
        if not picked:
            raise ValueError("Nothing to start: those features have started already")
        first = picked if order == "together" else picked[:1]
        started = [self.create(product_id=v["product_id"], title=cands[i]["title"], description=cands[i]["description"],
                               created_by=by, background=True, initiative_id=initiative_id,
                               not_duplicate_reason=f"A candidate feature of {initiative_id}") for i in first]
        q = self._queue()
        if order == "sequence" and len(picked) > 1:
            q[initiative_id] = {"after": started[-1], "next": picked[1:], "by": by}
        else:
            q.pop(initiative_id, None)
        self._save_queue(q)
        return {"started": started, "queued": [cands[i]["title"] for i in picked[1:]] if order == "sequence" else []}

    def _start_next_in_order(self, feature_id: str) -> str | None:
        """A feature that reached everyone: start the next one its initiative queued after it."""
        q = self._queue()
        hit = next(((iid, e) for iid, e in q.items() if e.get("after") == feature_id), None)
        if hit is None:
            return None
        iid, e = hit
        s = self._state(feature_id)
        if s.get("status") != "done":
            return None
        try:
            v = self.view(iid)
            nxt = next((i for i in e["next"] if i < len(v["candidates"]) and not v["candidates"][i]["started"]), None)
            if nxt is None:
                q.pop(iid)
                self._save_queue(q)
                return None
            c = v["candidates"][nxt]
            fid = self.create(product_id=v["product_id"], title=c["title"], description=c["description"],
                              created_by=e.get("by", ""), background=True, initiative_id=iid,
                              not_duplicate_reason=f"A candidate feature of {iid}, started in order")
        except Exception:
            log.warning("starting the next feature of %s failed", iid, exc_info=True)
            return None
        rest = [i for i in e["next"] if i != nxt]
        if rest:
            q[iid] = {**e, "after": fid, "next": rest}
        else:
            q.pop(iid)
        self._save_queue(q)
        return fid

    def start_first_feature(self, inception_id: str, *, by: str) -> str:
        """After inception: the first initiative's first feature, once the product is registered."""
        from .inception import formats
        from .inception.setup import ids

        s = self._state(inception_id)
        if s.get("status") != "set_up":
            raise ValueError("The product isn't set up yet: approve the product gate first")
        pid = ids(s["brief"])["product_id"]
        if pid not in self.services.settings.registry:
            raise ValueError(f"{pid} isn't registered yet: merge the governance and registry pull requests, then "
                             "reload the config")
        inis = [i for i in formats.initiatives(s.get("roadmap", "")) if i.get("id")]
        if not inis or not inis[0]["features"]:
            raise ValueError("The roadmap has no first feature to start")
        f = inis[0]["features"][0]
        return self.create(product_id=pid, title=f["title"], description=f.get("does", ""), created_by=by,
                           background=True, initiative_id=f"{pid}-INI-1")

    def create_inception(self, vision, *, created_by: str = "", background: bool = False) -> str:
        """Start a product inception: the brief first, then the product council's brief gate."""
        from dataclasses import asdict

        from .inception.graph import PRODUCT_ID

        tid = self._new_id(PRODUCT_ID, "inception", vision.name)
        inp = {"inception_id": tid, "product_id": PRODUCT_ID, "title": vision.name, "vision": asdict(vision),
               "created_by": created_by, "status": "researching"}
        self.bus.publish({"type": "feature.created", "feature_id": tid, "product_id": PRODUCT_ID})
        (self._submit if background else self._execute)(tid, inp)
        return tid

    def inception_summaries(self) -> list[dict]:
        from .inception.graph import PRODUCT_ID

        out = []
        for row in self._rows(PRODUCT_ID, "inception"):
            s = self._state(row["feature_id"])
            v = s.get("vision") or {}
            out.append({"id": row["feature_id"], "title": s.get("title") or row["title"], "org": v.get("org", ""),
                        "existing": bool(v.get("existing")), "product_id": v.get("product_id", ""),
                        "status": s.get("status", "drafting_brief"), "created_by": s.get("created_by", ""),
                        "cost_usd": round(float(s.get("cost_usd", 0.0)), 4),
                        "running": self.is_running(row["feature_id"])})
        return out

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

    # --- operations: production signals and incidents --------------------------------------
    def ingest(self, signals: list[dict], background: bool = False) -> list[str]:
        """Attach each signal to the open incident it belongs to (same alert, issue or component),
        or open a new incident for a firing one, paging on-call. Returns the incidents touched.
        With background, triage runs after this returns (webhooks answer at once)."""
        import logging

        from .ops import correlate
        from .ops.signals import resolve_product, unmatched_reason

        touched: list[str] = []
        for sig in signals:
            product_id, repo = resolve_product(self.services, sig)
            if not product_id:
                # Kept, not dropped: a mislabelled alert source shows up in the unmatched list.
                reason = unmatched_reason(self.services, sig)
                logging.getLogger(__name__).warning("signal %s matched no product: %s", sig.get("fingerprint"), reason)
                keep = getattr(self.services.signals, "add_unmatched", None)
                if callable(keep):
                    keep(sig, reason)
                self.bus.publish({"type": "signal.unmatched", "fingerprint": sig.get("fingerprint", "")})
                continue
            sig = {**sig, "product_id": product_id, "component": repo or sig.get("component", "")}
            sig = self._with_traces(sig)
            joined, others = "", []
            with self._create_lock:
                # A cost finding joins only the incident for the same finding, never a component's outage.
                match = "" if sig.get("category") == "cost" else repo
                iid = self.services.signals.open_incident_for(product_id, sig["fingerprint"], match)
                created = False
                if sig["status"] == "firing" and sig.get("category") != "cost" and not sig.get("no_correlate"):
                    # The same failure seen from another component (ops/correlate.py). A signal that
                    # names the same request as other open incidents ties them into one.
                    matches = correlate.related(self, product_id, sig)
                    if iid is None and matches:
                        iid, joined = matches[0]
                        sig = {**sig, "labels": {**(sig.get("labels") or {}), "joined_by": joined}}
                    others = [m for m, why in matches if m != iid and why.startswith("trace")]
                if iid is None and sig["status"] == "firing":
                    iid = self._new_id_locked(product_id, "incident", sig["title"])
                    created = True
                if iid is None:
                    continue  # a resolution for an incident already closed
                self.services.signals.add(iid, product_id, sig)
            for other in others:  # this signal ties them together: one incident
                self.merge_incidents(other, iid, approver="agent:correlate",
                                     reason=f"{sig['title']} ties it to {iid} ({joined})")
            if joined:
                self._root_moved(iid)
            if created:
                inp = {"incident_id": iid, "product_id": product_id, "title": sig["title"], "severity": sig["severity"],
                       "component": repo, "opened_at": sig["at"], "status": "triaging",
                       "events": [self._page(iid, product_id, repo, sig)]}
                self.bus.publish({"type": "feature.created", "feature_id": iid, "product_id": product_id})
                (self._submit if background else self._execute)(iid, inp)
            else:
                (self.poke if background else self.poke_now)(iid)  # a resolution may end its recovery wait
            touched.append(iid)
        return list(dict.fromkeys(touched))

    def _page(self, iid: str, product_id: str, repo: str, sig: dict) -> dict:
        """A new incident: tell on-call (low urgency) if its severity calls for a heads-up while the
        tool works on it (platform/remediation.yaml page.heads_up). Otherwise nobody is paged
        unless the tool can't fix it (ops/incident.py escalate)."""
        from .roadmap.graphs import event

        try:
            snap = self.services.snapshot(product_id)
            data_class, heads_up = snap.product.data_class, (snap.remediation.get("page") or {}).get("heads_up", ["sev1"])
        except Exception:
            data_class, heads_up = "phi", ["sev1"]  # unknown: say as little as possible
        if sig.get("category") == "cost" or sig["severity"] not in heads_up:
            return event("page", "not_paged", "the tool is working on it; on-call is paged only if it can't fix it")
        try:
            said = self.services.pager.trigger({
                "incident_id": iid, "product_id": product_id, "title": sig["title"], "severity": sig["severity"],
                "component": repo or sig.get("component", ""), "env": sig.get("env", "production"),
                "source": sig.get("source", ""), "data_class": data_class})
        except Exception as exc:
            said = f"not paged: {type(exc).__name__}: {exc}"
        return event("page", "not_paged" if said.startswith("not paged") else "paged", said)

    def error_lines(self, product_id: str, component: str, minutes: int = 5) -> int | None:
        """Error lines in a production component's recent logs, or None when they can't be read."""
        try:
            rt = self.services.snapshot(product_id).product.runtime or {}
            got = self.services.openchoreo.observe(
                component=(rt.get("components") or {}).get(component, component), env="production",
                project=rt.get("openchoreo_project"), namespace=rt.get("namespace"), minutes=minutes)
        except Exception:
            return None
        if not got.get("available", True):
            return None
        return sum(1 for line in got.get("logs") or [] if "stack_trace" in line or '"ERROR"' in line)

    def unmatched_signals(self, limit: int = 100) -> list[dict]:
        """Signals that matched no product, newest first, with why (ops/signals.py unmatched_reason)."""
        lister = getattr(self.services.signals, "unmatched", None)
        return lister(limit) if callable(lister) else []

    def alarm_changes(self, signals: list[dict], background: bool = False) -> dict:
        """CloudWatch alarm state changes: every one goes on the alarm board (what G6 reads);
        production ones also go into the incident flow."""
        from .ops.signals import resolve_product

        kept, production = 0, []
        for sig in signals:
            product_id, _ = resolve_product(self.services, sig)
            if not product_id:
                continue
            sig = {**sig, "product_id": product_id}
            self.services.alarms.record(sig)
            kept += 1
            if sig["env"] == "production":
                production.append(sig)
        return {"recorded": kept, "incidents": self.ingest(production, background=background) if production else []}

    # --- correlation: one incident per failure ------------------------------------------------
    def _with_traces(self, sig: dict) -> dict:
        """An OpenChoreo log alert names no request: read the trace ids of the component's recent
        error logs, so it can be matched to the apps' and other services' reports (best effort)."""
        from .ops.correlate import traces_in_logs

        labels = sig.get("labels") or {}
        if sig.get("source") != "openchoreo" or labels.get("alertType") != "log" or labels.get("trace_ids"):
            return sig
        try:
            snap = self.services.snapshot(sig["product_id"])
            rt = snap.product.runtime or {}
            got = self.services.openchoreo.observe(
                component=(rt.get("components") or {}).get(sig["component"], sig["component"]), env=sig.get("env", "production"),
                project=rt.get("openchoreo_project"), namespace=rt.get("namespace"), minutes=10)
        except Exception:
            return sig
        ids = traces_in_logs(got.get("logs") or [])
        return {**sig, "labels": {**labels, "trace_ids": ",".join(ids)}} if ids else sig

    def _root_moved(self, incident_id: str) -> None:
        """A joined signal may show the failure starts further downstream: if the incident is
        waiting for on-call, triage again around the new root cause candidate."""
        from .ops.correlate import root

        s = self._state(incident_id)
        if not s or s.get("status") != "awaiting_decision":
            return
        cand, why = root(self.services.snapshot(s["product_id"]), self.services.signals.for_incident(incident_id),
                         s.get("component", ""))
        if not cand or cand == s.get("component"):
            return
        item = next((p for p in self.pending(incident_id) if p.get("gate") == "incident"), None)
        if item:
            try:
                self.decide(incident_id, item["interrupt_id"], {
                    "decision": "revise", "approver": "agent:correlate",
                    "feedback": f"A related signal joined; the root cause candidate is now {cand}: {why}"})
            except RuntimeError:
                pass  # running: its next triage reads the signals anyway

    def merge_incidents(self, incident_id: str, into: str, *, approver: str, reason: str = "") -> dict:
        """Move one incident's signals into another and end it as merged. On-call can do this; the
        tool does it when a new signal ties two open incidents together."""
        if incident_id == into:
            raise ValueError("An incident can't be merged into itself")
        moved = self.services.signals.move(incident_id, into)
        self.merged[incident_id] = into
        pending = self.pending(incident_id)
        item = next((p for p in pending if p.get("gate") == "incident"), None)
        wait = next((p for p in pending if p.get("wait") == "incident_recovery"), None)
        ended = False
        if item:
            self.decide(incident_id, item["interrupt_id"], {"decision": "merge", "into": into, "approver": approver,
                                                            "feedback": reason or f"Merged into {into}"})
            ended = True
        elif wait and not self.is_running(incident_id):  # the tool was watching it recover
            self.services.close_incident(incident_id)
            self._execute(incident_id, Command(resume={wait["interrupt_id"]: {"status": "merged", "into": into}}))
            ended = True
        else:  # busy: it ends at its next step (ops/incident.py merged)
            self.services.close_incident(incident_id)
        product_id = self._state(incident_id).get("product_id", "")
        self.services.pager.resolve(incident_id, product_id, f"merged into {into}")
        self._root_moved(into)
        self.bus.publish({"type": "feature.updated", "feature_id": into})
        return {"merged": incident_id, "into": into, "signals": moved, "ended": ended}

    def split_signal(self, incident_id: str, fingerprint: str, *, approver: str) -> dict:
        """Take a signal that was joined wrongly out of an incident: it opens its own."""
        sigs = [s for s in self.services.signals.for_incident(incident_id) if s["fingerprint"] == fingerprint]
        if not sigs:
            raise KeyError(f"{incident_id} has no signal {fingerprint}")
        latest = dict(sigs[-1])
        latest.pop("incident_id", None)
        placeholder = f"{incident_id}:split:{fingerprint}"
        self.services.signals.move(incident_id, placeholder, fingerprint)
        self.services.signals.close(placeholder)
        labels = {k: v for k, v in (latest.get("labels") or {}).items() if k != "joined_by"}
        new = self.ingest([{**latest, "status": "firing", "no_correlate": True,
                            "labels": {**labels, "split_from": incident_id, "split_by": approver}}])
        self.bus.publish({"type": "feature.updated", "feature_id": incident_id})
        return {"incident_id": incident_id, "signal": fingerprint, "new_incident": new[0] if new else None}

    # --- cost: released features against what G4 approved -----------------------------------
    def review_costs(self, at=None) -> list[dict]:
        """Review each released feature that has reached a review day without a review for it:
        AWS spend against its G4 plan, production compute the week after release against the week
        before. Findings go into the incident flow as cost signals. Returns the reviews made."""
        from .ops import cost

        at = at or cost.now()
        made = []
        for row in self.index.all():
            if row.get("kind", "feature") != "feature":
                continue
            try:
                s = self._state(row["feature_id"])
                released = cost.released_at(s) if s.get("status") == "done" else None
                if not released or not s.get("cost_plan"):
                    continue
                elapsed = (at - released).total_seconds() / 86400
                due = [d for d in self.cost_review_days if elapsed >= d]
                done = {r.get("review_day") for r in self.services.cost_reviews.for_feature(row["feature_id"])}
                todo = [d for d in due if d not in done]
                if not todo:
                    continue
                rv = self.review_cost(s, at=at, released=released)
                rv["review_day"] = max(todo)  # catching up on several days makes one review
                for d in todo[:-1]:
                    self.services.cost_reviews.add({**rv, "review_day": d, "skipped": True})
                self.services.cost_reviews.add(rv)
                made.append(rv)
            except Exception as exc:
                self.errors[f"cost:{row['feature_id']}"] = f"{type(exc).__name__}: {exc}"
        return made

    def review_cost(self, s: dict, *, at, released) -> dict:
        from datetime import timedelta

        from .ops import cost

        src = self.services.costs
        plan = s["cost_plan"]
        window = min(7.0, max(1.0, (at - released).total_seconds() / 86400))
        start = at - timedelta(days=window)
        aws_actual = {}
        for env, account in sorted((plan.get("accounts") or {}).items()):
            if env in (plan.get("aws_monthly_usd") or {}):
                aws_actual[env] = src.aws_costs(product_id=s["product_id"], feature_id=s["feature_id"],
                                                account=account, start=start, end=at)
        before, after, repo_of = {}, {}, {}
        groups: dict[tuple, list[str]] = {}
        for repo, dep in sorted((s.get("deployments") or {}).items()):
            if dep.get("kind") in ("resource", "store") or not dep.get("component"):
                continue
            if dep.get("env") != (list(dep.get("pipeline") or ["production"]))[-1]:
                continue  # production only
            repo_of[dep["component"]] = repo
            groups.setdefault((dep.get("namespace") or "default", dep.get("project") or "", dep["env"]), []).append(dep["component"])
        for (ns, project, env), comps in groups.items():
            after |= src.component_costs(namespace=ns, project=project, env=env, components=comps,
                                         start=max(start, released), end=at)
            before |= src.component_costs(namespace=ns, project=project, env=env, components=comps,
                                          start=released - timedelta(days=window), end=released)
        rv = cost.review(plan, feature_id=s["feature_id"], product_id=s["product_id"], released=released, at=at,
                         aws_actual=aws_actual, window_days=window, compute_before=before, compute_after=after,
                         tolerance=self.cost_tolerance)
        sigs = cost.signals(rv, title=s.get("title", ""), repo_of=lambda c: repo_of.get(c, c))
        rv["incidents"] = self.ingest(sigs) if sigs else []
        return rv

    def _run_cost(self, feature_id: str, s: dict) -> dict | None:
        """For the feature page: production's approved monthly AWS cost and the latest review."""
        plan = s.get("cost_plan") or {}
        if not plan.get("resources"):
            return None
        reviews = [r for r in self.services.cost_reviews.for_feature(feature_id) if not r.get("skipped")]
        last = reviews[-1] if reviews else None
        return {"approved_monthly_usd": plan.get("aws_monthly_usd") or {},
                "projected_monthly_usd": ({e: v["projected_monthly_usd"] for e, v in last["aws"].items()} if last else None),
                "reviewed_at": last["at"] if last else None,
                "findings": [f["text"] for f in last["findings"]] if last else [],
                "incidents": last.get("incidents", []) if last else []}

    def collect_costs(self, at=None, product_ids=None) -> int:
        """Bring the cost ledger up to date (vrixel_sdlc.finops): every product's agent calls, CI,
        builds, environments, production compute and AWS, then the shared platform's share.
        Returns the records written."""
        from .finops import collect as fc
        from .finops.views import month_bounds
        from .ops import cost

        at = at or cost.now()
        r = fc.rates()
        ledger = self.services.cost_ledger
        since = at.timestamp() - fc.LOOKBACK_DAYS * 86400
        usage = [u for u in self.services.ledger.records() if fc.utc(u.ts).timestamp() >= since]
        written, compute = 0, {}
        reg = self.services.settings.registry
        for pid in (product_ids or list(reg)):
            try:
                written += self._collect_product(pid, at, r, [u for u in usage if u.product_id == pid])
            except Exception as exc:
                self.errors[f"cost:{pid}"] = f"{type(exc).__name__}: {exc}"
                continue
            _, start, end = month_bounds(None, at)
            compute[pid] = sum(x.usd for x in ledger.query(product_id=pid, stage="run", start=start, end=end)
                               if x.category == "compute")
        if not product_ids:
            written += ledger.upsert(fc.collect_platform(compute, at.date(), at, r))
        return written

    def _collect_product(self, pid: str, at, r: dict, usage: list) -> int:
        from .finops import collect as fc
        from .ops import cost

        ledger = self.services.cost_ledger
        recs = fc.collect_llm(usage, kind_of, lambda t: None)
        snap = self.services.snapshot(pid)
        windows: dict[tuple, list] = {}
        releases = {}
        for row in self._rows(pid, "feature"):
            fid = row["feature_id"]
            s = self._state(fid)
            if not s:
                continue
            releases[fid] = cost.released_at(s) if s.get("status") == "done" else None
            end = fc.ended_at(s, at)
            if (at - end).days > fc.LOOKBACK_DAYS:
                continue  # settled long ago
            recs += fc.collect_ci(self.services.github, s, r)
            recs += fc.collect_builds(self.services.openchoreo, s, r)
            recs += fc.collect_people(s, r)
            for env, (a, b) in fc.env_windows(s, at).items():
                for dep in (s.get("deployments") or {}).values():
                    if dep.get("kind") in ("resource", "store") or not dep.get("component"):
                        continue
                    windows.setdefault((dep.get("project") or "", env, dep["component"]), []).append((fid, a, b))
        targets = set()
        runtimes = [snap.product.runtime or {}] + [lane.runtime or {} for lane in snap.lane_of_repo().values()]
        for rt in runtimes:
            if not rt.get("openchoreo_project"):
                continue
            pipeline = list(rt.get("pipeline") or ["dev", "staging", "production"])
            for env in pipeline:
                targets.add((rt.get("namespace") or "default", rt["openchoreo_project"], env, env == pipeline[-1]))
        recs += fc.collect_runtime(self.services.costs, pid, sorted(targets), windows, at, r)
        aws = (snap.product.runtime or {}).get("aws") or {}
        recs += fc.collect_aws(self.services.costs, pid, dict(aws.get("accounts") or {}), releases, at, r)
        return ledger.upsert(recs)

    def feature_cost(self, feature_id: str) -> dict:
        """The feature's cost card: build cost by category, its own resources' run cost, the plan G4
        approved, and the reviews since release."""
        from .finops import collect as fc
        from .finops.views import feature_card

        s = self._state(feature_id) if kind_of(feature_id) == "feature" else {}
        if not s:
            raise KeyError(feature_id)
        # Agent spend is always current: it is in the tool's own ledger.
        self.services.cost_ledger.upsert(fc.collect_llm(self.services.ledger.records(feature_id), kind_of,
                                                        lambda t: None))
        return feature_card(self.services.cost_ledger, s,
                            reviews=self.services.cost_reviews.for_feature(feature_id))

    def product_cost(self, product_id: str, month: str | None = None) -> dict:
        """The product's month: run cost by category and environment, build cost by feature."""
        from .finops.views import product_rollup

        features = []
        for f in self.feature_summaries(product_id):
            plan = self._state(f["feature_id"]).get("cost_plan") or {}
            features.append({**f, "approved_monthly_usd": plan.get("aws_monthly_usd") or {}})
        return product_rollup(self.services.cost_ledger, product_id, features, month=month)

    def focus_export(self, product_id: str, month: str | None = None) -> str:
        from .finops.ledger import focus_csv
        from .finops.views import month_bounds
        from .ops.cost import now

        _, start, end = month_bounds(month, now())
        return focus_csv(self.services.cost_ledger.query(product_id=product_id, start=start, end=end))

    def incident_summaries(self, product_id: str, limit: int = 20) -> list[dict]:
        out = []
        for row in reversed(self._rows(product_id, "incident")):
            s = self._state(row["feature_id"])
            cause = ""
            from .ops.incident import section

            cause = section(s.get("triage", ""), "Likely cause").split("\n")[0][:300]
            out.append({"id": row["feature_id"], "title": s.get("title") or row["title"], "severity": s.get("severity"),
                        "status": s.get("status"), "component": s.get("component", ""), "cause": cause,
                        "links": s.get("links") or {}})
            if len(out) >= limit:
                break
        return out

    # --- traceability: from a feature's reason to production, and back -------------------------
    def trace(self, feature_id: str) -> dict:
        """The feature's trace: metrics, goals, criteria, tasks, PRs, acceptance results,
        deployments and the production incidents on its components (vrixel_sdlc.traceability)."""
        from . import traceability

        s = self._state(feature_id)
        if not s:
            raise KeyError(f"Unknown feature {feature_id}")
        arts = s.get("artifacts") or {}

        def load(kind: str) -> str:
            try:
                return self.services.artifacts.load(arts[kind]) if kind in arts else ""
            except Exception:
                return ""

        components = sorted({r for r, d in (s.get("deployments") or {}).items()
                             if d.get("env") and d.get("kind") != "resource"})
        incidents = [{"id": i.get("feature_id", ""), "title": i.get("title", ""), "status": i.get("status", ""),
                      "severity": i.get("severity", ""), "touch": i.get("touch", "")}
                     for i in (self._touching(s["product_id"], components) if components else [])]
        return traceability.build(s, load, incidents)

    def trace_search(self, product_id: str, *, repo: str = "", pr: str = "") -> list[dict]:
        """Which of a product's features touched a repo or a pull request, and through which criteria."""
        from . import traceability

        rows = [{"feature_id": r["feature_id"], "title": r["title"], "state": self._state(r["feature_id"])}
                for r in self.index.all()
                if r["product_id"] == product_id and r.get("kind", "feature") == "feature"]
        return traceability.search(rows, repo=repo, pr=pr)

    def poke_now(self, thread_id: str) -> None:
        if self.is_running(thread_id):
            return
        try:
            cmd = self._ready_wait(thread_id)
        except Exception:
            return
        if cmd is not None:
            try:
                self._execute(thread_id, cmd)
            except Exception:
                pass

    def suspects(self, product_id: str, repo: str = "") -> list[dict]:
        """Released features that changed the component, most recent first (all recent ones when the
        component is unknown): the first places triage looks. Features of the products built on
        this one count too (they can change its repos and they call its components).

        A feature still rolling out (its flag on for some users) is a suspect too when it changed a
        component that calls the failing one or that the failing one calls, so the incident can
        turn its flag off: a broken provider shows up as errors in its callers, the web app
        included, and a caller's new traffic can break what it calls."""
        from .ops.correlate import graph_for, short

        products = [product_id]
        if repo:
            for pid in self.services.settings.registry:
                if pid == product_id:
                    continue
                try:
                    if product_id in self.services.snapshot(pid).product.depends_on:
                        products.append(pid)
                except Exception:
                    continue
        graphs: dict = {}
        out = []
        for pid in products:
            for row in self._rows(pid, "feature"):
                s = self._state(row["feature_id"])
                rel = s.get("release") or {}
                if not rel.get("production"):
                    continue
                repos = set((s.get("change_set") or {}).get("merge_order") or []) | \
                    {pr["repo"] for pr in (s.get("pull_requests") or {}).values()}
                via = ""
                if repo and repo not in repos:
                    rolling = bool(rel.get("rollout_plan")) and 0 < float(rel.get("flag_percent") or 0) < 100
                    if not rolling:
                        continue
                    if pid not in graphs:
                        try:
                            graphs[pid] = graph_for(self.services.snapshot(pid))
                        except Exception:
                            graphs[pid] = None
                    if graphs[pid] is None or not any(graphs[pid].related(r, repo) for r in repos):
                        continue
                    via = (f"rolling out to {float(rel.get('flag_percent')):g}% in a component that {short(repo)} "
                           f"calls or is called by")
                at = next((g["at"] for g in reversed(s.get("gate_decisions") or []) if g["gate"] == "g6"), "")
                extra: dict = {"via": via} if via else {}
                if pid != product_id:
                    # Another product's flag: the tool turns it off by itself only if that product lets it.
                    try:
                        mine = "flag_off" in ((self.services.snapshot(pid).remediation or {}).get("autonomous") or {})
                    except Exception:
                        mine = False
                    extra |= {"product_id": pid, "flag_off_allowed": mine}
                out.append({"feature_id": row["feature_id"], "title": s.get("title", ""), "released_at": at,
                            "flag": rel.get("flag", ""), "flag_on": self._flag_on(s), "repos": sorted(repos),
                            "track": s.get("track", "standard"), **extra})
        return sorted(out, key=lambda x: x["released_at"], reverse=True)

    def start_hotfix(self, incident: dict, *, repo: str, change: str, approver: str) -> str:
        if not repo:
            raise ValueError("A hotfix needs the repo to change")
        return self.create(product_id=incident["product_id"], title=f"Hotfix: {change}"[:150],
                           description=f"Fix for incident {incident['incident_id']} ({incident['severity']}): "
                                       f"{incident['title']}.\n\n{change}",
                           created_by=approver, background=True, track="hotfix",
                           hotfix={"incident_id": incident["incident_id"], "repo": repo, "change": change,
                                   "severity": incident["severity"], "approved_by": approver})

    def start_intent_change(self, incident: dict, *, scope: str, intent: str, approver: str) -> str:
        signals = self.services.signals.for_incident(incident["incident_id"])
        evidence = "\n".join(f"- {s['source']}: {s['title']}" for s in signals[:10])
        request = (f"{intent}\n\nRaised by incident {incident['incident_id']} ({incident['severity']}): "
                   f"{incident['title']}.\nWhat users reported or what failed:\n{evidence}")
        if scope == "initiative":
            return self.create_initiative(product_id=incident["product_id"], title=intent[:120], request=request,
                                          created_by=approver, background=True)
        suspects = self.suspects(incident["product_id"], incident.get("component", ""))
        initiative = next((self._state(x["feature_id"]).get("initiative_id") for x in suspects
                           if self._state(x["feature_id"]).get("initiative_id")), "")
        return self.create(product_id=incident["product_id"], title=intent[:150], description=request,
                           created_by=approver, background=True, initiative_id=initiative or "",
                           not_duplicate_reason=f"Changes what was built, after incident {incident['incident_id']}")

    def _cohorts(self, product_id: str, flags: list[str], packet: dict) -> dict:
        """Users with each flag on against off, since the step began, per component and per app
        (ops/cohorts.py): {flag: {group: comparison}}. The whole project's summary lines count, not
        only the feature's components: anything that checks the flag is watched. Empty when there's
        no log source or no summary lines (a UI-only feature on an older template)."""
        from .ops import cohorts as co
        from .ops.rollout import _at, _clock

        logs = getattr(self.services, "cohort_logs", None)
        if logs is None:
            return {}
        try:
            rt = self.services.snapshot(product_id).product.runtime or {}
            since = _at(packet["since"]) or _clock()
            entries = logs.entries(namespace=rt.get("namespace") or "default", project=rt.get("openchoreo_project") or "",
                                   environment=list(rt.get("pipeline") or ["production"])[-1], since=since, until=_clock())
        except Exception:
            return {}
        out: dict[str, dict] = {}
        parsed = co.parse_entries(entries, flags)
        for k, groups in parsed.items():
            got = {g: {**co.compare(c, packet.get("compare")), "group": g} for g, c in groups.items() if c["lines"]}
            if got:
                out[k] = got
        # The feature's goal events, per side (outcome loop, step 4): reported, never a reason to stop.
        events = list(packet.get("goal_events") or [])
        key = packet.get("flag")
        if events and key in parsed and any(c["lines"] for c in parsed[key].values()):
            out["__goal__"] = co.goal_events(parsed[key], events, packet.get("goal_compare"))
        return out

    def _touching(self, product_id: str, components) -> list[dict]:
        """Incidents a rollout of these components should wait out: in one of them, or in a component
        that calls them or that they call (the call graph, across the lanes the product builds on),
        web and mobile apps included.
        A feature that breaks a provider shows up first as errors in its callers."""
        from .ops.correlate import graph_for, short

        comps = {short(c) for c in components or [] if short(c)}
        if not comps:
            return []
        try:
            snap = self.services.snapshot(product_id)
            graph, lanes = graph_for(snap), list(snap.product.depends_on)
        except Exception:
            graph, lanes = None, []
        out = []
        # The lanes it builds on too: their components are called by this product's.
        for row in [r for pid in [product_id, *lanes] for r in self._rows(pid, "incident")]:
            s = self._state(row["feature_id"])
            theirs = {short(s.get("component", ""))} | {short(x.get("component") or "")
                                                         for x in self.services.signals.for_incident(row["feature_id"])}
            theirs.discard("")
            if comps & theirs:
                out.append({**s, "touch": "same component"})
            elif graph is not None and any(graph.related(a, b) for a in comps for b in theirs):
                out.append({**s, "touch": "calls or is called by it"})
        return out

    def feature_info(self, feature_id: str) -> dict:
        """A feature's risk tier and production components (repos), for a launch's gradual release."""
        s = self._state(feature_id)
        comps = sorted({d.get("repo") for d in (s.get("deployments") or {}).values()
                        if d.get("env") == "production" and d.get("kind") not in ("resource", "store") and d.get("repo")})
        return {"risk_tier": s.get("risk_tier"), "components": comps}

    def remediation_record(self, product_id: str, window_days: int = 90) -> dict:
        """How the tool's own fixes went for a product (ops/remediation.py calibration)."""
        from .ops.remediation import track_record

        return track_record([self._state(r["feature_id"]) for r in self._rows(product_id, "incident")], window_days)

    def _release_waits(self, packet: dict) -> dict | None:
        if packet["wait"] == "flag_step":
            from .ops import rollout as ro

            pid, key = packet["product_id"], packet["flag"]
            touching = self._touching(pid, packet.get("components") or [])
            keys = list(packet.get("flags") or [key])
            off = [k for k in keys if self.services.flags.get(pid, k) is False or self.services.flags.percent(pid, k) == 0]
            if off:
                return {"status": "failure", "reason": f"{', '.join(off)} {'was' if len(off) == 1 else 'were'} turned off"}
            cohorts = self._cohorts(pid, keys, packet)
            goal = cohorts.pop("__goal__", None)
            # Any one component (or app) where its users fail more stops the step: the worst one per flag.
            worse = {}
            for k, groups in cohorts.items():
                bad = [r for r in groups.values() if r["verdict"] == "regression"]
                if bad:
                    worse[k] = max(bad, key=lambda r: (r["on_rate"] - r["off_rate"]) + ((r.get("slow") or {}).get("on_rate", 0) - (r.get("slow") or {}).get("off_rate", 0)))
            if worse:
                from .ops.cohorts import describe

                return {"status": "failure", "regression": worse, "cohorts": cohorts,
                        "reason": ("users with it on fail more: " if any("errors" in r.get("worse", ["errors"]) for r in worse.values())
                                   else "users with it on are slower: ") + "; ".join(describe(k, r) for k, r in worse.items())}
            res = ro.check(on=self.services.flags.get(pid, key), percent=self.services.flags.percent(pid, key),
                           since=packet["since"], bake_minutes=int(packet.get("bake_minutes") or 60), incidents=touching,
                           stub=self.services.settings.mode == "stub")
            if res and res.get("status") == "success":
                res["cohorts"] = cohorts
                if goal:
                    res["goal_cohorts"] = goal
            return res
        if packet["wait"] == "analysis_report":
            agents = self.services.agents
            got = None
            if agents is not None:
                got = (agents.rca_report if packet.get("kind") == "rca" else agents.cost_report)(packet["report_id"])
            status = (got or {}).get("status")
            if status in ("completed", "failed"):
                return {"status": "success" if status == "completed" else "failure", "report_status": status,
                        "report": got.get("report"), "reason": "" if status == "completed" else "the analysis failed"}
            started = datetime.fromisoformat(str(packet["since"]).replace("Z", "+00:00"))
            if datetime.now(timezone.utc) - started > timedelta(minutes=float(packet.get("minutes") or 10)):
                return {"status": "failure", "report_status": "timed_out",
                        "reason": f"no report after {packet.get('minutes')} minutes"}
            return None
        if packet["wait"] == "incident_recovery":
            from .ops.incident import recovery

            comp, pid = packet.get("component") or "", packet.get("product_id") or ""
            return recovery(self.services.signals.for_incident(packet["feature_id"]), packet["since"],
                            int(packet.get("minutes") or 240), quiet_minutes=int(packet.get("quiet") or 0),
                            errors=(lambda: self.error_lines(pid, comp)) if comp else None)
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
            waiting = any(p.get("wait") == "release_train" for p in self.pending(fid))
            if packet["model"] == "launch":
                # In production with its flag off, or (no flag) held after staging like a train.
                ok = bool((s.get("release") or {}).get("production")) or waiting
            else:
                ok = waiting
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
               background: bool = False, not_duplicate_reason: str = "", initiative_id: str = "",
               track: str = "standard", hotfix: dict | None = None, pilot: bool = False,
               profile: str = "", attachments: tuple[str, list[dict]] | None = None, ui: bool | None = None) -> str:
        from . import profiles
        from .duplicates import DuplicateIntent

        # Read and validate the product's governance from its own org, and pin the commits.
        snap = self.services.snapshot(product_id)
        workflow = getattr(snap.product, "workflow", {}) or {}
        profile = "feature" if track == "hotfix" else (profile or profiles.default(workflow))
        if track != "hotfix" and profile not in profiles.allowed(workflow, snap.product.data_class):
            raise ValueError(f"{product_id} doesn't allow the {profile} profile "
                             f"(allowed: {', '.join(profiles.allowed(workflow, snap.product.data_class))})")
        if initiative_id and not self.approved_initiative(product_id, initiative_id):
            raise ValueError(f"{initiative_id} is not an approved initiative of {product_id}")
        with self._create_lock:  # two identical requests at once must not both pass the check
            similar = self.similar(product_id, title, description)
            if track == "hotfix":  # a fix for an incident, decided by on-call: not a new intent
                not_duplicate_reason = not_duplicate_reason or f"Hotfix for {(hotfix or {}).get('incident_id', 'an incident')}"
            if any(m["duplicate"] for m in similar) and not not_duplicate_reason.strip():
                raise DuplicateIntent(similar)
            seq = self.index.next_number(product_id)
            feature_id = f"{product_id}-FEAT-{seq}"
            self.index.add(feature_id, product_id, title, seq)
        inp = {"feature_id": feature_id, "product_id": product_id, "title": title, "description": description,
               "created_by": created_by, "simulate_faults": dict(simulate_faults or {}), "governance": snap.pins,
               "related_features": similar, "not_duplicate_reason": not_duplicate_reason.strip(),
               "initiative_id": initiative_id, "track": track, "hotfix": dict(hotfix or {}), "pilot": bool(pilot),
               "profile": profile,
               # Whether it changes what people see (UX design and G3 run only then); G2 can change it.
               **self._ui(product_id, title, description, ui),
               # A share of new features runs on the candidate config, when one is set (VRIXEL_CANARY_*).
               "config_variant": self.services.canary_variant(feature_id, snap.product.data_class, track)}
        if budget_usd:
            inp["token_budget_usd"] = float(budget_usd)
        if attachments and attachments[1]:  # (intake conversation, files): moved under the feature first
            inp["attachments"] = self.services.attachments.to_feature(attachments[0], feature_id, attachments[1])
        self.bus.publish({"type": "feature.created", "feature_id": feature_id, "product_id": product_id})
        if background:
            self._submit(feature_id, inp)
        else:
            self._execute(feature_id, inp)
        return feature_id

    def path(self, product_id: str, stations: list[dict]) -> dict:
        from . import progress

        cached = self._path_history.get(product_id) if hasattr(self, "_path_history") else None
        if cached is None or time.monotonic() - cached[0] > 600:
            states = []
            for row in self.index.all():
                if row["product_id"] == product_id and kind_of(row["feature_id"]) == "feature":
                    try:
                        states.append(self._state(row["feature_id"]))
                    except Exception:
                        continue
            if not hasattr(self, "_path_history"):
                self._path_history = {}
            cached = self._path_history[product_id] = (time.monotonic(), progress.history(states, RAIL))
        return progress.estimate(stations, cached[1])

    def _ui(self, product_id: str, title: str, description: str, said: bool | None) -> dict:
        """Whether UX design runs. The intake's judgement when there was one; otherwise only a product
        with no web or mobile app skips it (a guess from words alone is too often wrong). G2 can change it."""
        if said is not None:
            return {"ui": bool(said), "ui_why": "The intake conversation found " + (
                "something people see changes" if said else "nothing people see changes")}
        from .intake import UI_ROLES

        try:
            apps = any(r.role in UI_ROLES for r in self.services.snapshot(product_id).product.repos)
        except Exception:
            apps = True
        return {"ui": apps, "ui_why": "" if apps else "The product has no web or mobile app"}

    def _note_asked(self, feature_id: str) -> None:
        """When each question to a person was first asked, so its wait can be measured."""
        try:
            pending = self.pending(feature_id)
        except Exception:
            return
        for p in pending:
            if p.get("type") != "external":
                self.services.effort.add({"feature_id": feature_id, "product_id": p.get("product_id", ""),
                                          "kind": "asked", "ref": p["interrupt_id"],
                                          "detail": {"type": p.get("type"), "gate": p.get("gate", ""),
                                                     "phase": p.get("phase", "")}})
        try:  # an initiative's features started in order: the next one, once this reached everyone
            if kind_of(feature_id) == "feature":
                self._start_next_in_order(feature_id)
        except Exception:
            log.warning("starting the next feature after %s failed", feature_id, exc_info=True)
        notifier = getattr(self.services, "notifier", None)
        if notifier is None:
            return
        try:  # tell the people who are needed, and the watchers when it reaches staging (vrixel_sdlc.notify)
            s = self._state(feature_id)
            for p in pending:
                if p.get("type") != "external":
                    notifier.needed({**p, "feature_id": feature_id}, s.get("title", ""))
            if any(e.get("node") == "staging_wait" and e.get("kind") == "passed" for e in s.get("events") or []):
                notifier.reached(feature_id, s.get("title", ""), "staging")
        except Exception:  # never stops the work
            log.warning("notifying about %s failed", feature_id, exc_info=True)

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

    def decide(self, feature_id: str, interrupt_id: str, decision: dict, *, background: bool = False,
               update: dict | None = None) -> dict:
        if self.is_running(feature_id):
            raise RuntimeError(f"{feature_id} is still running; decide once it is waiting")
        item = self.item(feature_id, interrupt_id)
        if item["type"] == "external":
            raise RuntimeError(f"{feature_id} is waiting on {item.get('label', 'an external system')}, not a person")
        d = normalize(decision, tuple(item["allowed"]))  # validate before resuming
        if d["decision"] == "merge" and (not d.get("into") or d["into"] == feature_id):
            from .graph import InvalidDecision

            raise InvalidDecision("Merging needs another incident to merge into")
        if d["decision"] == "accept_risk" and not d["feedback"].strip():
            from .graph import InvalidDecision

            raise InvalidDecision("Accepting a security risk needs a reason")
        asked = self.services.effort.asked_at(feature_id, interrupt_id)
        if asked:  # how long the question waited for a person
            from .pilot import minutes_between, now as _now

            d["waited_minutes"] = round(minutes_between(asked, _now()), 2)
        if kind_of(feature_id) == "incident":  # on-call's work counts in the product's pilot totals
            self.services.effort.add({"feature_id": feature_id, "product_id": item.get("product_id", ""),
                                      "kind": "incident_decision", "who": d.get("approver", ""),
                                      "ref": interrupt_id, "minutes": d["active_minutes"]})
        cmd = Command(resume={interrupt_id: d}, update=update) if update else Command(resume={interrupt_id: d})
        if background:
            self._submit(feature_id, cmd)
            return {"feature_id": feature_id, "accepted": True}
        self._execute(feature_id, cmd)
        return self.view(feature_id)

    def view(self, feature_id: str) -> dict:
        if kind_of(feature_id) != "feature":
            from .roadmap.views import thread_view

            v = thread_view(self, feature_id)
            if kind_of(feature_id) == "incident":
                v["paging"] = self.services.pager.history(feature_id)
            return v
        snap = self.graph.get_state(self._config(feature_id))
        s = snap.values
        if not s:
            raise KeyError(f"Unknown feature {feature_id}")
        records = self.services.ledger.records(feature_id)
        human = sum(g.get("active_minutes", 0.0) for g in s.get("gate_decisions", []))
        from .telemetry import sessions as agent_sessions
        pending = [{"interrupt_id": i.id, **i.value} for i in snap.interrupts]
        return {
            "feature_id": feature_id,
            "product_id": s.get("product_id"),
            "title": s.get("title"),
            "description": s.get("description", ""),
            "created_by": s.get("created_by", ""),
            "status": s.get("status"),
            "profile": s.get("profile", "feature"),
            "config_variant": s.get("config_variant", ""),
            "track": s.get("track", "standard"),
            "running": (running := self.is_running(feature_id)),
            "error": self.errors.get(feature_id),
            "rail": (stations := rail(s, pending, running=running)),
            # How long each step usually takes in this product, and when it should be done (vrixel_sdlc.progress).
            "path": self.path(s.get("product_id", ""), stations),
            # Each agent session: what it was for, its cost, and a link to its trace (vrixel_sdlc.telemetry).
            "agent_sessions": agent_sessions(records, self.trace_link),
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
            "flag": s.get("flag", ""),
            "gate_decisions": s.get("gate_decisions", []),
            "pending": pending,
            "cost_usd": round(float(s.get("cost_usd", 0.0)), 4),
            "budget_usd": s.get("token_budget_usd"),
            "tokens_used": s.get("tokens_used", 0),
            "human_active_minutes": human,
            "usage": summarize(records),
            "run_cost": self._run_cost(feature_id, s),
            "events": s.get("events", []),
            "diagrams": s.get("diagrams", []),
            "delivery": s.get("delivery", {}),
            "deployments": s.get("deployments", {}),
            "runs": (s.get("change_set") or {}).get("runs", {}),
            "quality": s.get("quality", {}),
            "new_repos": s.get("new_repos", []),
            "waivers": s.get("waivers", []),
            "attachments": [{k: a.get(k) for k in ("id", "name", "kind", "size", "read_by_agents")}
                            for a in s.get("attachments") or []],
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
                if (p.get("traceability") or {}).get("blocks"):  # the helper has no one to ask: say so on the record
                    d["override_reason"] = "Overridden by the auto-drive helper (demo or test run)"
            else:
                d = {"decision": "resolved", "feedback": "resolved by a human"}
            self.decide(feature_id, p["interrupt_id"], {"approver": approver, "active_minutes": 5, **d})
        return self.view(feature_id)


RAIL = [("discovery", "phase"), ("g1", "gate"), ("spec", "phase"), ("g2", "gate"), ("design", "phase"),
        ("g3", "gate"), ("architecture", "phase"), ("g4", "gate"), ("plan", "phase"), ("build", "phase"),
        ("g5", "gate"), ("release", "phase"), ("g6", "gate"), ("operate", "phase")]


WAIT_STATION = {"ci": "build", "build": "build", "dev": "build", "staging": "release", "production": "operate",
                "rollout": "operate", "flag_step": "operate", "release_train": "g6"}
EXPEDITED = ("discovery", "g1", "spec", "g2", "design", "g3", "architecture", "g4")


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
    from .profiles import get as _profile

    prof = _profile(s.get("profile"))
    skipped = set(prof.skips) | set(prof.skips_gates)
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
        if phase_status.get("design") == "skipped" and key in ("design", "g3"):
            st = "skipped"  # nothing people see changes (or G2 said so)
        if s.get("track") == "hotfix" and key in EXPEDITED:
            st = "skipped"  # an incident's hotfix starts at planning
        elif key in skipped:
            st = "skipped"  # the workflow profile leaves it out
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

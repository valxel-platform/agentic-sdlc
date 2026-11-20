"""FeatureEngine: start features, list the gate inbox, record human decisions, resume threads."""

from __future__ import annotations

import logging

import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor


from langgraph.types import Command

from .cluster import Busy, Cluster, LeaseLost, MemoryCluster, Notes
from .events import EventBus
from .graph import build_pipeline
from .graph.pipeline import normalize
from .gateway import summarize
from .services import Services
from .waits import WaitChecker
from .worksync import InMemoryLinkStore, WorkSync
from .engine_parts.base import (  # noqa: F401 - importable from valxel_sdlc.engine as before
    KINDS,
    kind_of,
    FeatureIndex,
    InMemoryIndex,
    PostgresIndex,
    openchoreo_auth,
    RAIL,
    WAIT_STATION,
    EXPEDITED,
    rail,
)
from .engine_parts.watching import WatchingMixin
from .engine_parts.roadmap import RoadmapMixin
from .engine_parts.operations import OperationsMixin
from .engine_parts.costs import CostsMixin
from .engine_parts.recovery import RecoveryMixin

log = logging.getLogger(__name__)


class FeatureEngine(WatchingMixin, RoadmapMixin, OperationsMixin, CostsMixin, RecoveryMixin):
    def __init__(self, services: Services, checkpointer, index: FeatureIndex | None = None,
                 bus: EventBus | None = None, workers: int = 4, work: WorkSync | None = None,
                 cluster: Cluster | None = None, queue=None) -> None:
        self.services = services
        # With a run queue (run_queue.py), work is queued here and run by `valxel worker`.
        self.queue = queue
        # Several replicas: thread locks, the watcher's leadership and shared notes (cluster.py).
        self.cluster: Cluster = cluster or MemoryCluster()
        from .ops.incident import build_incident_graph
        from .ops.signals import InMemorySignalStore
        from .inception.graph import build_inception_graph
        from .outcomes.review import build_review_graph
        from .roadmap.graphs import build_initiative_graph, build_release_graph

        from .tenancy import thread_bound

        # Writes LangGraph commits from its own worker threads carry no scope: they belong to the
        # thread's own workspace (valxel_sdlc.tenancy). Looked up once per thread.
        self._thread_ws: dict[str, str] = {}
        checkpointer = thread_bound(checkpointer, self._cached_workspace) if checkpointer is not None else None
        self.checkpointer = checkpointer
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
        if getattr(services, "gate_outcomes", None) is None:
            from .confidence import InMemoryOutcomeStore

            services.gate_outcomes = InMemoryOutcomeStore()
        if getattr(services, "provision_queue", None) is None:
            from .provision_queue import InMemoryProvisionQueue

            services.provision_queue = InMemoryProvisionQueue()
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
        self.cost_review_days = tuple(float(d) for d in (os.environ.get("VALXEL_COST_REVIEW_DAYS") or "7,30").split(",")
                                      if d.strip())
        self.cost_tolerance = float(os.environ.get("VALXEL_COST_TOLERANCE") or 0.2)
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
        # Scan upkeep: security findings on products' default branches (valxel_sdlc.upkeep).
        from .upkeep.service import Upkeep

        self.upkeep = Upkeep(self)
        if getattr(services, "agents", None) is None:
            from .integrations.openchoreo_agents import from_env as agents_from_env

            services.agents = agents_from_env(services.settings, openchoreo_auth())
        services.remediation_record = self.remediation_record
        services.shadow_record = self.shadow_record
        services.feature_info = self.feature_info
        # Asked once, across replicas and repeated steps (cluster.py): what a graph step asks another
        # system to start, so a step run again after a crash doesn't ask twice.
        services.ask_once, services.unask = self.cluster.claim, self.cluster.unclaim
        if getattr(services, "cohort_logs", None) is None:
            from .ops.cohorts import from_env as cohorts_from_env

            services.cohort_logs = cohorts_from_env(openchoreo_auth())
        if getattr(services, "pager", None) is None:
            from .ops.paging import from_env

            services.pager = from_env(os.environ.get("VALXEL_PUBLIC_URL") or "http://localhost:8080")
        self.bus = bus or EventBus()
        self._pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="feature")
        self.running: set[str] = set()  # queued or running on this replica
        self.errors = Notes(self.cluster, "error")  # a thread's last error, whichever replica ran it
        from .watcher_jobs import Jobs

        self.jobs = Jobs(self.cluster)  # the watcher's housekeeping and its health (watcher_jobs.py)
        from .checkpoint_archive import Archive

        # Regulated lanes' checkpoint history, archived before it's pruned (checkpoint_archive.py).
        self.checkpoint_archive = Archive.from_env()
        # Crash recovery (engine_parts/recovery.py): work queued on some replica and not yet started,
        # and how often the reaper has restarted a thread from the same checkpoint.
        self.queued = Notes(self.cluster, "queued")
        self.recoveries = Notes(self.cluster, "recovery")
        self.work = work or WorkSync(services, InMemoryLinkStore(),
                                     os.environ.get("VALXEL_PUBLIC_URL") or "http://localhost:8080")
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
        """Running (or queued) here, queued for a worker, or running on another replica."""
        return (thread_id in self.running or self.cluster.busy(thread_id)
                or (self.queue is not None and bool(self.queue.queued_among([thread_id]))))

    def busy_among(self, threads: list[str]) -> set[str]:
        out = (self.running & set(threads)) | self.cluster.busy_among(threads)
        return out | (self.queue.queued_among(threads) if self.queue is not None else set())

    def run_job(self, thread_id: str, payload) -> None:
        """A worker's run of a queued job (run_queue.Worker)."""
        self._execute(thread_id, payload)

    def _run(self, thread_id: str, payload) -> None:
        """Run it now, here, or with a run queue, queue it for a worker: an API process runs no agents."""
        if self.queue is not None:
            self._submit(thread_id, payload)
        else:
            self._execute(thread_id, payload)

    def _execute(self, feature_id: str, payload) -> None:
        """Run the thread until it finishes or waits on a human, publishing each node as it ends.
        Only one replica runs a thread at a time: the one holding its lock. It runs in its own
        workspace's scope, whoever started it (valxel_sdlc.tenancy); a thread of the platform's own,
        such as the product council's inception, in the platform's."""
        from .tenancy import PLATFORM, bound

        with bound(self.workspace_of(feature_id) or PLATFORM):
            return self._execute_scoped(feature_id, payload)

    def _execute_scoped(self, feature_id: str, payload) -> None:
        lease = self.cluster.lock(feature_id)
        self.queued.pop(feature_id, None)  # started, or refused: either way no longer waiting here
        if lease is None:
            self.running.discard(feature_id)
            raise Busy(f"{feature_id} is running on another replica")
        try:
            self.running.add(feature_id)
            self.errors.pop(feature_id, None)
            self.bus.publish({"type": "feature.running", "feature_id": feature_id})
            try:
                first = True  # None as the first payload: carry on from the checkpoint (a resume)
                while first or payload is not None:
                    first = False
                    for chunk in self._g(feature_id).stream(payload, self._config(feature_id), stream_mode="updates"):
                        for node in chunk:
                            if node != "__interrupt__":
                                self.bus.publish({"type": "feature.node", "feature_id": feature_id, "node": node})
                        if not lease.alive():  # another replica may carry it on from the last checkpoint
                            raise LeaseLost(f"{feature_id}: this replica lost the thread's lock")
                    payload = self._ready_wait(feature_id)  # CI or a deploy already done: carry on
                self.recoveries.pop(feature_id, None)  # ran through: a later restart counts from one
                self._prune(feature_id)
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

    def _prune(self, thread_id: str) -> None:
        """Keep only the thread's latest checkpoint. The tool never goes back to an earlier one, and
        without this every step's copy of the state stays in Postgres: about 95% of what a finished
        feature stored. Runs while this replica holds the thread's lock, so no run of it is under
        way; a run that ended in an error keeps its history until the next one succeeds."""
        from langgraph.checkpoint.memory import InMemorySaver

        prune = getattr(self.checkpointer, "prune", None)
        if prune is None or isinstance(self.checkpointer, InMemorySaver):
            return  # in memory: nothing piles up past the process
        if not self._archive_before_prune(thread_id):
            return
        try:
            prune([thread_id], strategy="keep_latest")
        except NotImplementedError:  # the in-memory saver: nothing piles up past the process
            return
        except Exception as exc:  # noqa: BLE001 - pruning is housekeeping: the next run tries again
            log.warning("could not prune %s's checkpoints: %s", thread_id, exc)

    def _archive_before_prune(self, thread_id: str) -> bool:
        """A regulated lane's history goes to the archive before it's pruned, or isn't pruned
        (checkpoint_archive.py). Other lanes prune without one."""
        from . import checkpoint_archive

        try:
            product_id = self._state(thread_id).get("product_id") or thread_id.split("-", 1)[0]
        except Exception:  # noqa: BLE001 - no state to read: treat as regulated below
            product_id = thread_id.split("-", 1)[0]
        if not checkpoint_archive.regulated(self.services, product_id):
            return True
        if self.checkpoint_archive is None:
            log.warning("%s is regulated and VALXEL_CHECKPOINT_ARCHIVE_BUCKET isn't set: its checkpoints "
                        "are kept, not pruned", thread_id)
            return False
        try:
            entry = self.services.settings.registry.get(product_id)
            self.checkpoint_archive.put(self.checkpointer, product_id, thread_id,
                                        workspace=getattr(entry, "workspace", "") or "")
            return True
        except checkpoint_archive.NotArchived as exc:
            log.error("%s's checkpoints not archived, so not pruned: %s", thread_id, exc)
            return False

    def _flag_on(self, s: dict) -> bool | None:
        """Whether the feature is visible to users: its flag as the flag service has it now."""
        rel = s.get("release") or {}
        if not rel.get("flag"):
            return None
        on = self.services.flags.get(s["product_id"], rel["flag"])
        return rel.get("flag_on") if on is None else on

    def trace_link(self, trace_id: str) -> str:
        tel = getattr(self.services, "telemetry", None)
        return tel.cfg.link(trace_id) if tel is not None else ""

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
        from .cluster import stamp

        if self.queue is not None:  # a worker runs it; the queue's row says it's running meanwhile
            if isinstance(payload, dict) and not self._g(feature_id).get_state(self._config(feature_id)).values:
                # A new thread: its first state is its first checkpoint, so its page shows it while it
                # waits, and the worker carries on from there.
                self._g(feature_id).update_state(self._config(feature_id), payload, as_node="__start__")
                payload = None
            self.queue.enqueue(feature_id, payload, workspace=self.workspace_of(feature_id) or "")
            self.queued[feature_id] = stamp()
            self.bus.publish({"type": "feature.running", "feature_id": feature_id})
            return
        # Running from the moment the work is queued, not when a worker picks it up: otherwise a
        # caller (or a second decision) can see the old state between the two.
        self.running.add(feature_id)
        self.queued[feature_id] = stamp()  # other replicas see it's queued (engine_parts/recovery.py)

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
        busy = self.busy_among([r["feature_id"] for r in rows])
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

    def _rows(self, product_id: str, kind: str) -> list[dict]:
        return [r for r in self.index.all() if r["product_id"] == product_id and r.get("kind", "feature") == kind]

    def _new_id(self, product_id: str, kind: str, title: str, workspace: str | None = None) -> str:
        with self._create_lock:
            return self._new_id_locked(product_id, kind, title, workspace)

    def _new_id_locked(self, product_id: str, kind: str, title: str, workspace: str | None = None) -> str:
        tag = {v: k for k, v in KINDS.items()}[kind]
        seq = self.index.next_number(product_id, kind)
        thread_id = f"{product_id}-{tag}-{seq}"
        self.index.add(thread_id, product_id, title, seq, kind, self.workspace_of_product(product_id)
                       if workspace is None else workspace)
        return thread_id

    def workspace_of_product(self, product_id: str) -> str:
        entry = self.services.settings.registry.get(product_id)
        return getattr(entry, "workspace", "") or ""

    def _cached_workspace(self, thread_id: str) -> str:
        ws = self._thread_ws.get(thread_id)
        if ws is None:
            ws = self._thread_ws[thread_id] = self.workspace_of(thread_id)
        return ws

    def workspace_of(self, thread_id: str) -> str:
        """The workspace a thread belongs to, from the index ('' for the platform's own threads)."""
        from .tenancy import PLATFORM, bound

        with bound(PLATFORM):
            row = next((r for r in self.index.all() if r["feature_id"] == thread_id), None)
        if row is None:
            return self.workspace_of_product(thread_id.rsplit("-", 2)[0])
        return row.get("workspace") or self.workspace_of_product(row["product_id"])

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
            self.index.add(feature_id, product_id, title, seq, workspace=self.workspace_of_product(product_id))
        inp = {"feature_id": feature_id, "product_id": product_id, "title": title, "description": description,
               "created_by": created_by, "simulate_faults": dict(simulate_faults or {}), "governance": snap.pins,
               "related_features": similar, "not_duplicate_reason": not_duplicate_reason.strip(),
               "initiative_id": initiative_id, "track": track, "hotfix": dict(hotfix or {}), "pilot": bool(pilot),
               "profile": profile,
               # Whether it changes what people see (UX design and G3 run only then); G2 can change it.
               **self._ui(product_id, title, description, ui),
               # A share of new features runs on the candidate config, when one is set (VALXEL_CANARY_*).
               "config_variant": self.services.canary_variant(feature_id, snap.product.data_class, track)}
        if budget_usd:
            inp["token_budget_usd"] = float(budget_usd)
        if attachments and attachments[1]:  # (intake conversation, files): moved under the feature first
            inp["attachments"] = self.services.attachments.to_feature(attachments[0], feature_id, attachments[1])
        self.bus.publish({"type": "feature.created", "feature_id": feature_id, "product_id": product_id})
        if background:
            self._submit(feature_id, inp)
        else:
            self._run(feature_id, inp)
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
        try:  # tell the people who are needed, and the watchers when it reaches staging (valxel_sdlc.notify)
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
        self._run(feature_id, cmd)
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
            # How long each step usually takes in this product, and when it should be done (valxel_sdlc.progress).
            "path": self.path(s.get("product_id", ""), stations),
            # Each agent session: what it was for, its cost, and a link to its trace (valxel_sdlc.telemetry).
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

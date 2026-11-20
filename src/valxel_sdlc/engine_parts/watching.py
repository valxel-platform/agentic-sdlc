"""Waiting on the outside world: the watcher, external waits, release waits and connections."""

from __future__ import annotations

import logging
import threading

from datetime import datetime, timedelta, timezone

from langgraph.types import Command

from .base import kind_of

log = logging.getLogger(__name__)


class WatchingMixin:
    """FeatureEngine's watching methods (valxel_sdlc.engine)."""

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
                    self._run(other, cmd)
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

    def check_connections(self) -> int:
        """Each workspace's connections, one cheap read each (valxel_sdlc.connections)."""
        conns = getattr(self.services, "connections", None)
        return conns.check_all() if conns is not None else 0

    def expire_transcripts(self) -> int:
        """Delete agent session transcripts older than a day (valxel_sdlc.sessions): resuming needs
        them only that long; the handoffs and session records stay."""
        from ..sessions import TranscriptStore

        gone = TranscriptStore(self.services.settings.artifact_dir).expire()
        if gone:
            log.info("Deleted %d session transcripts older than a day", gone)
        return gone

    def remove_drafts(self) -> int:
        """Finished features' working copies of their documents, once the product's repo holds the
        record and the retention time has passed (valxel_sdlc.drafts)."""
        from .. import drafts

        n = 0
        for row in self.index.all():
            try:
                state = self._state(row["feature_id"])
                if drafts.remove(self.services, self.cluster, {**state, "feature_id": row["feature_id"]}):
                    n += 1
            except Exception:
                log.exception("drafts of %s not removed", row.get("feature_id"))
        return n

    def catch_up_receipts(self) -> int:
        """ServiceNow receipts that never went (valxel_sdlc.ops.servicenow)."""
        catch = getattr(getattr(self.services, "changes", None), "catch_up", None)
        sent = catch() if catch is not None else 0
        if sent:
            log.info("Queued %d ServiceNow receipts that hadn't gone", sent)
        return sent

    def start_watcher(self, interval_s: float = 30.0) -> None:
        """Poll features that wait on CI or OpenChoreo. Every replica starts it; only the one that
        holds the watcher's lock polls (cluster.py)."""
        if self._watcher is not None:
            return

        def loop():
            from ..tenancy import PLATFORM, bound

            with bound(PLATFORM):  # the platform's own work, across workspaces (valxel_sdlc.tenancy)
                watch()

        def watch():
            while not self._stop.wait(interval_s):
                if not self.cluster.leader("watcher"):
                    continue  # another replica watches; this one takes over if that one goes
                for fid in self.waiting_on_external():
                    self.poke(fid)
                self.jobs.tick(self.watcher_jobs())

        self._watcher = threading.Thread(target=loop, name="valxel-watcher", daemon=True)
        self._watcher.start()

    def watcher_jobs(self) -> list:
        """The watcher's housekeeping, and how often each runs (watcher_jobs.py records how it goes)."""
        from ..watcher_jobs import DAILY, Job

        return [
            Job("recover_stranded", self.recover_stranded, 60),  # threads a replica dropped (recovery.py)
            Job("snapshot_inventories", self.snapshot_inventories, DAILY),  # one inventory snapshot a day
            Job("collect_outcomes", self.collect_outcomes, DAILY),  # readouts and goal trends move daily
            Job("sweep_change_set_envs", self.sweep_change_set_envs, 900),  # environments left behind
            Job("upkeep_sweep", self.upkeep.sweep, DAILY),  # every repo's alerts, for missed webhooks
            Job("upkeep_tick", self.upkeep.tick, 900),  # deadlines, fixes, Dependabot updates
            Job("prune_cluster_notes", lambda: self.cluster.prune(30), 3600),
            Job("collect_costs", self.collect_costs, 3600),  # cost data moves daily; hourly is plenty
            Job("review_costs", self.review_costs, 3600),
            Job("expire_transcripts", self.expire_transcripts, 3600),
            Job("remove_drafts", self.remove_drafts, DAILY),  # finished features' drafts (drafts.py)
            Job("check_connections", self.check_connections, 3600),
            Job("catch_up_receipts", self.catch_up_receipts, 3600),
            Job("tool_health", self.tool_health, 3600),  # the tool's own agents against their baseline
            Job("drift_evals", self.drift_evals, DAILY),  # replays weekly (drift.py decides when it's due)
        ]

    def drift_evals(self) -> dict | None:
        """The live config replayed on real cases, weekly, with no config change (valxel_sdlc.drift)."""
        from .. import drift

        if self.services.evals is None:
            return None
        return drift.run(self.services, self.cluster)

    def tool_health(self) -> dict:
        """The tool's own health against its baseline, with alerts (valxel_sdlc.health)."""
        from .. import health

        return health.run(self)

    def sweep_change_set_envs(self) -> list[str]:
        """Delete change-set environments whose feature stopped, finished or outlived the product's
        time to live (valxel_sdlc.changeset_env). The merge train deletes them in the normal case."""
        from .. import changeset_env

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

    def poke_now(self, thread_id: str) -> None:
        if self.is_running(thread_id):
            return
        try:
            cmd = self._ready_wait(thread_id)
        except Exception:
            return
        if cmd is not None:
            try:
                self._run(thread_id, cmd)
            except Exception:
                pass

    def _release_waits(self, packet: dict) -> dict | None:
        if packet["wait"] == "flag_step":
            from ..ops import rollout as ro

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
                from ..ops.cohorts import describe

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
        if packet["wait"] == "corroboration":  # a second independent sender (ops/remediation.py min_sources)
            from ..ops.remediation import sources

            seen = sources(self.services.incident_signals(packet["feature_id"]))
            if len(seen) >= int(packet.get("need") or 2):
                return {"status": "success", "sources": seen}
            started = datetime.fromisoformat(str(packet["since"]).replace("Z", "+00:00"))
            if datetime.now(timezone.utc) - started > timedelta(minutes=float(packet.get("minutes") or 10)):
                return {"status": "failure", "sources": seen}
            return None
        if packet["wait"] == "incident_recovery":
            from ..ops.incident import recovery

            comp, pid = packet.get("component") or "", packet.get("product_id") or ""
            return recovery(self.services.signals.for_incident(packet["feature_id"]), packet["since"],
                            int(packet.get("minutes") or 240), quiet_minutes=int(packet.get("quiet") or 0),
                            errors=(lambda: self.error_lines(pid, comp)) if comp else None)
        if packet["wait"] == "fix_window":  # a held security fix before G6 (valxel_sdlc.upkeep)
            return self.upkeep.fix_window(packet)
        if packet["wait"] == "release_train":  # a feature waiting for its train's approval
            s = self._state(packet["release_id"])
            if packet["feature_id"] not in (s.get("features") or []) and s.get("status") not in ("", "planned"):
                # A security fix that joined but wasn't ready when the release closed its list: it
                # wasn't approved with it, so it goes to its own G6.
                self.services.findings.unjoin(packet["feature_id"])
                return {"status": "failure", "reason": f"it wasn't ready when {packet['release_id']} closed its list"}
            if s.get("status") in ("approved", "shipped"):
                return {"status": "success", "approver": s.get("approved_by", ""), "release_id": packet["release_id"]}
            if s.get("status") == "cancelled":
                return {"status": "failure", "reason": f"{packet['release_id']} was cancelled"}
            # A security fix riding along leaves a release that's late for its deadline: its own G6.
            late = self.upkeep.past_release_by(packet["feature_id"])
            if late:
                return {"status": "failure", "reason": late}
            return None
        # release_ready: every member is ready (launch: in production, flag off; train: waiting at
        # its train after staging) or was stopped.
        ready, dropped, lanes, flags, evidence = [], [], set(), {}, []
        # Security fixes that joined after the release was planned ride along when they're ready;
        # one that isn't yet doesn't hold the release up (valxel_sdlc.upkeep).
        joined = [f for f in (self.services.findings.joined(packet["release_id"]) if packet.get("release_id") else [])
                  if f not in packet["features"]]
        riding = [f for f in joined if any(p.get("wait") == "release_train" and p.get("release_id") == packet["release_id"]
                                          for p in self.pending(f))]
        for fid in [*packet["features"], *riding]:
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
                "evidence": "\n".join(evidence), "joined": [f for f in riding if f in ready]}

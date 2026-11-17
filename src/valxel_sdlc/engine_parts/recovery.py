"""Picking up threads a replica dropped: crash recovery without a workflow engine.

A thread's state is checkpointed in Postgres after every step, and the replica moving it holds its
advisory lock, which Postgres lets go of when the replica dies. So a replica killed mid-step (a
crash, an out-of-memory kill, a deploy) leaves the thread at its last checkpoint with a next step
to run, and nobody running it. Work queued on that replica's in-memory pool and not yet started is
left the same way. Nothing woke such a thread until now: it sat "in progress" for good.

    stranded   the checkpoint has a next step; no person or outside system is being waited on; no
               replica holds its lock; it isn't queued on any replica (the `queued` note, written
               when work is queued and cleared when it starts, so other replicas can see it, and
               counted dead after VALXEL_RUN_QUEUE_STALE_S); and it has no recorded error
    reaper     the watcher's leader resumes stranded threads from their checkpoint
               (`recover_stranded`), a thread at most VALXEL_RUN_RECOVERY_LIMIT times (3) without
               progress: a step that kills the process each time is stopped with an error instead
               of restarted for ever
    resume     a thread stopped on an error stays stopped until a person resumes it
               (POST /api/features/{id}/resume), since an error that repeats would only repeat

A step that was cut off runs again from its start, as after a retry from an error. Opening a pull
request reuses the branch's open one, merging skips one already merged, and post-deploy, acceptance
and rollout runs are claimed once (cluster.py), so those don't repeat; an agent session that was cut
off does run again, and its tokens count twice.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone

log = logging.getLogger(__name__)

DONE = ("done", "aborted")


def _queue_stale_s() -> float:
    return float(os.environ.get("VALXEL_RUN_QUEUE_STALE_S", "900"))


def _limit() -> int:
    return int(os.environ.get("VALXEL_RUN_RECOVERY_LIMIT", "3"))


def _age_s(when: str) -> float:
    try:
        then = datetime.fromisoformat(when)
    except (TypeError, ValueError):
        return float("inf")
    return (datetime.now(timezone.utc) - then).total_seconds()


class RecoveryMixin:
    """FeatureEngine's crash recovery (valxel_sdlc.engine)."""

    def _checkpoint_id(self, thread_id: str) -> str:
        snap = self._g(thread_id).get_state(self._config(thread_id))
        return str(((snap.config or {}).get("configurable") or {}).get("checkpoint_id") or "")

    def stranded_reason(self, thread_id: str) -> str:
        """Why the thread isn't stranded ('' when it is): what it's doing or waiting for instead."""
        if self.is_running(thread_id):
            return "running"
        queued = self.queued.get(thread_id)
        if queued and _age_s(queued) < _queue_stale_s():
            return "queued"
        snap = self._g(thread_id).get_state(self._config(thread_id))
        s = snap.values or {}
        if not s or s.get("status") in DONE:
            return "finished"
        if snap.interrupts:
            return "waiting"
        if not snap.next:
            return "finished"
        if self.errors.get(thread_id):
            return "error"
        return ""

    def resume(self, thread_id: str, *, by: str = "") -> dict:
        """Carry a stopped thread on from its last checkpoint: one a replica dropped, or one that
        stopped on an error, once a person has seen the error. Raises RuntimeError when there is
        nothing to resume."""
        why = self.stranded_reason(thread_id)
        if why not in ("", "error"):
            raise RuntimeError({"running": f"{thread_id} is running", "queued": f"{thread_id} is about to run",
                                "waiting": f"{thread_id} is waiting for a decision or an outside system, "
                                           "not stopped", "finished": f"{thread_id} has nothing left to run"}[why])
        log.info("resuming %s from its checkpoint%s%s", thread_id, f" for {by}" if by else "",
                 " after an error" if why == "error" else "")
        self.errors.pop(thread_id, None)
        self.recoveries.pop(thread_id, None)  # a person's resume starts the count again
        self.bus.publish({"type": "feature.resumed", "feature_id": thread_id, "by": by or "the tool"})
        self._submit(thread_id, None)
        return {"feature_id": thread_id, "accepted": True}

    def recover_stranded(self) -> list[str]:
        """The reaper: resume every thread a replica dropped. Run by the watcher's leader."""
        out = []
        for row in self.index.all():
            tid = row["feature_id"]
            try:
                if self.stranded_reason(tid):
                    continue
                at = self._checkpoint_id(tid)
            except Exception as exc:  # noqa: BLE001 - one unreadable thread doesn't stop the sweep
                log.warning("couldn't check %s for recovery: %s", tid, exc)
                continue
            seen = self.recoveries.get(tid) or {}
            tries = int(seen.get("tries", 0)) + 1 if seen.get("checkpoint") == at else 1
            if tries > _limit():
                self.errors[tid] = (f"Stopped: restarted {tries - 1} times from the same step, and the replica "
                                    "running it went away each time. Resume once the cause is fixed.")
                self.recoveries.pop(tid, None)
                self.bus.publish({"type": "feature.updated", "feature_id": tid})
                log.error("%s: gave up after %d restarts from checkpoint %s", tid, tries - 1, at)
                continue
            self.recoveries[tid] = {"checkpoint": at, "tries": tries}
            log.warning("%s was dropped mid-step; resuming from its checkpoint (restart %d)", tid, tries)
            self.bus.publish({"type": "feature.resumed", "feature_id": tid, "by": "the tool"})
            self._submit(tid, None)
            out.append(tid)
        return out

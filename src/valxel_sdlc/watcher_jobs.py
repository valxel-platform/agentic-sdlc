"""The watcher's housekeeping jobs, and whether each one still works.

The watcher (engine_parts/watching.py) runs these on the replica that leads it. Each job used to
be wrapped in `except Exception: pass`, so a job could fail every day and nobody would know. Now:

- every failure is logged with its traceback ("watcher job <name> failed");
- each job's health is kept in the cluster's shared notes (`watcher-job:<name>`): when it last
  succeeded and failed, consecutive and total failures, and the last error's type (never its text,
  which may hold data);
- a daily job that fails is retried hourly instead of waiting for tomorrow;
- a job without a success for STALE_HOURS (26: a daily job, plus a retry or two) is stale. The
  watcher logs "watcher job <name> stale" at error level and posts it to VALXEL_NOTIFY_WEBHOOK, once,
  then daily while it stays stale; /api/healthz lists stale jobs. A job that recovers says so.

Health is shared, so it survives restarts and leadership moving to another replica. A job that has
never succeeded counts from when the leader first tried it.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

log = logging.getLogger("valxel_sdlc.watcher")
STALE_HOURS = 26
DAILY = "daily"
RETRY_S = 3600.0  # a failed daily job runs again after this long
REALERT_S = 24 * 3600.0


@dataclass
class Job:
    name: str
    run: Callable[[], Any]
    every: float | str  # seconds, or DAILY (once per UTC day, once it succeeds)


class Jobs:
    def __init__(self, cluster: Any, *, alert: Callable[[str], None] | None = None,
                 clock: Callable[[], float] = time.time) -> None:
        self.cluster, self.clock = cluster, clock
        self.alert = alert or _webhook_alert
        self.tried: dict[str, float] = {}  # this replica's last attempt per job
        self.done_day: dict[str, str] = {}  # daily jobs this replica saw succeed today (saves a read)

    def health(self, name: str) -> dict:
        return dict(self.cluster.get(f"watcher-job:{name}") or {})

    def _put(self, name: str, h: dict) -> None:
        self.cluster.put(f"watcher-job:{name}", h)

    def due(self, job: Job) -> bool:
        now, last = self.clock(), self.tried.get(job.name, 0.0)
        if job.every == DAILY:
            today = time.strftime("%Y-%m-%d", time.gmtime(now))
            if self.done_day.get(job.name) == today or (last and now - last < RETRY_S):
                return False
            done = time.strftime("%Y-%m-%d", time.gmtime(self.health(job.name).get("last_success") or 0))
            if done == today:  # another replica ran it today
                self.done_day[job.name] = today
                return False
            return True
        return now - last >= float(job.every)

    def run(self, job: Job) -> bool:
        """Run one job now, record how it went. Never raises."""
        now = self.clock()
        self.tried[job.name] = now
        h = self.health(job.name)
        h.setdefault("first_tried", now)
        try:
            job.run()
        except Exception as exc:  # a job's failure is recorded, not the watcher's end
            log.exception("watcher job %s failed", job.name)
            h.update(last_failure=now, last_error=type(exc).__name__,
                     failures=int(h.get("failures", 0)) + 1, total_failures=int(h.get("total_failures", 0)) + 1)
            self._put(job.name, h)
            return False
        if h.get("stale_alerted_at"):
            log.warning("watcher job %s recovered", job.name)
            self._say(f"Valxel watcher job {job.name} works again.")
        h.update(last_success=now, failures=0)
        self.done_day[job.name] = time.strftime("%Y-%m-%d", time.gmtime(now))
        h.pop("stale_alerted_at", None)
        self._put(job.name, h)
        return True

    def tick(self, jobs: list[Job]) -> None:
        for job in jobs:
            if self.due(job):
                self.run(job)
        self.check(jobs)

    def stale(self, jobs: list[Job]) -> list[dict]:
        now, out = self.clock(), []
        for job in jobs:
            h = self.health(job.name)
            since = h.get("last_success") or h.get("first_tried")
            if since and now - since > STALE_HOURS * 3600:
                out.append({"job": job.name, "hours_without_success": round((now - since) / 3600, 1),
                            "failures": int(h.get("failures", 0)), "last_error": h.get("last_error", "")})
        return out

    def check(self, jobs: list[Job]) -> list[dict]:
        """Alert on stale jobs: once, then daily while they stay stale."""
        now = self.clock()
        found = self.stale(jobs)
        for s in found:
            h = self.health(s["job"])
            if now - float(h.get("stale_alerted_at") or 0) < REALERT_S:
                continue
            log.error("watcher job %s stale: no success for %s hours (%s failures in a row, last %s)",
                      s["job"], s["hours_without_success"], s["failures"], s["last_error"] or "none")
            self._say(f"Valxel watcher job {s['job']} has not succeeded for {s['hours_without_success']} hours "
                      f"({s['failures']} failures in a row, last error {s['last_error'] or 'none'}). "
                      "See the tool's log for 'watcher job " + s["job"] + " failed'.")
            h["stale_alerted_at"] = now
            self._put(s["job"], h)
        return found

    def _say(self, text: str) -> None:
        try:
            self.alert(text)
        except Exception:  # the alert's own channel down: the log line stands
            log.exception("watcher alert not delivered")


def _webhook_alert(text: str) -> None:
    from .notify import _webhook

    _webhook(text)

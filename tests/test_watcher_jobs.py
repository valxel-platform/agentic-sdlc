"""The watcher's housekeeping jobs: failures logged and counted, daily jobs retried, stale jobs alerted."""

import logging

from valxel_sdlc.cluster import MemoryCluster
from valxel_sdlc.watcher_jobs import DAILY, Job, Jobs

DAY = 24 * 3600


class Clock:
    def __init__(self, t=1_800_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


def jobs_with(clock, alerts=None):
    said = alerts if alerts is not None else []
    return Jobs(MemoryCluster(), alert=said.append, clock=clock), said


def test_a_failure_is_logged_and_counted(caplog):
    clock = Clock()
    jobs, _ = jobs_with(clock)

    def boom():
        raise RuntimeError("secret detail")

    job = Job("collect_costs", boom, 3600)
    with caplog.at_level(logging.ERROR, logger="valxel_sdlc.watcher"):
        assert jobs.run(job) is False
        assert jobs.run(job) is False
    assert any("watcher job collect_costs failed" in r.message and r.exc_info for r in caplog.records)
    h = jobs.health("collect_costs")
    assert (h["failures"], h["total_failures"], h["last_error"]) == (2, 2, "RuntimeError")
    assert "secret detail" not in str(h)  # the error's type is kept, never its text
    job.run = lambda: None
    assert jobs.run(job) and jobs.health("collect_costs")["failures"] == 0
    assert jobs.health("collect_costs")["total_failures"] == 2


def test_a_failed_daily_job_is_retried_hourly_not_tomorrow():
    clock, calls = Clock(), []

    def flaky():
        calls.append(clock.t)
        if len(calls) == 1:
            raise OSError("down")

    jobs, _ = jobs_with(clock)
    job = Job("snapshot_inventories", flaky, DAILY)
    jobs.tick([job])
    clock.t += 600
    jobs.tick([job])  # too soon to retry
    clock.t += 3000
    jobs.tick([job])  # an hour after the failure: again, and it works
    clock.t += 3600
    jobs.tick([job])  # done for today
    assert len(calls) == 2


def test_interval_jobs_run_on_their_interval():
    clock, calls = Clock(), []
    jobs, _ = jobs_with(clock)
    job = Job("upkeep_tick", lambda: calls.append(1), 900)
    for _ in range(4):
        jobs.tick([job])
        clock.t += 300
    assert len(calls) == 2  # at 0 and 900 s


def test_a_job_without_success_for_26_hours_alerts_once_then_daily_and_on_recovery(caplog):
    clock = Clock()
    jobs, said = jobs_with(clock)
    broken = {"on": True}

    def job_fn():
        if broken["on"]:
            raise ConnectionError("x")

    job = Job("catch_up_receipts", job_fn, 3600)
    with caplog.at_level(logging.ERROR, logger="valxel_sdlc.watcher"):
        for _ in range(26):
            jobs.tick([job])
            clock.t += 3600
        assert said == []  # 25 hours without success: not yet
        jobs.tick([job])
        clock.t += 3600
        jobs.tick([job])
    assert len(said) == 1 and "catch_up_receipts" in said[0] and "ConnectionError" in said[0]
    assert any("watcher job catch_up_receipts stale" in r.message for r in caplog.records)
    [stale] = jobs.stale([job])
    assert stale["job"] == "catch_up_receipts" and stale["failures"] >= 27
    clock.t += DAY
    jobs.tick([job])
    assert len(said) == 2  # still stale a day later: said again
    broken["on"] = False
    clock.t += 3600
    jobs.tick([job])
    assert "works again" in said[-1] and jobs.stale([job]) == []


def test_health_is_shared_across_replicas():
    clock, cluster = Clock(), MemoryCluster()
    a = Jobs(cluster, alert=lambda _: None, clock=clock)
    b = Jobs(cluster, alert=lambda _: None, clock=clock)
    calls = []
    job = Job("collect_outcomes", lambda: calls.append(1), DAILY)
    a.tick([job])
    b.tick([job])  # leadership moved: the new leader sees it ran today
    assert calls == [1] and b.health("collect_outcomes")["last_success"] == clock.t


def test_an_alert_channel_failure_never_stops_the_watcher(caplog):
    clock = Clock()

    def down(_):
        raise OSError("webhook down")

    jobs = Jobs(MemoryCluster(), alert=down, clock=clock)
    job = Job("check_connections", lambda: 1 / 0, 3600)
    jobs.run(job)
    clock.t += 27 * 3600
    assert jobs.check([job])  # stale, alert attempted, no exception
    assert any("not delivered" in r.message for r in caplog.records)


def test_the_engine_runs_every_job_through_the_health_runner(engine):
    names = [j.name for j in engine.watcher_jobs()]
    assert len(names) == len(set(names)) == 14
    assert {"snapshot_inventories", "collect_outcomes", "upkeep_sweep", "collect_costs", "tool_health", "drift_evals"} <= set(names)


def test_monitoring_sees_stale_jobs(engine):
    from fastapi.testclient import TestClient

    from valxel_sdlc.api.app import create_app
    from valxel_sdlc.web.auth import AuthConfig

    with TestClient(create_app(engine, auth=AuthConfig(mode="dev", secret="x" * 40), web_dist=None)) as c:
        assert c.get("/api/healthz/watcher").json() == {"ok": True, "stale": []}
        engine.cluster.put("watcher-job:collect_costs", {"first_tried": 1.0, "failures": 3, "last_error": "OSError"})
        body = c.get("/api/healthz/watcher").json()
    assert body["ok"] is False and body["stale"][0]["job"] == "collect_costs"

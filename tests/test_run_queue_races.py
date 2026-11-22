"""The run queue under the races that matter in production (review finding 13, T-138): a claim that
runs out while its worker is still running, two workers on one run, a worker that dies, and the
same through Postgres with real workers."""

import os
import threading
import time
import uuid

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from valxel_sdlc.engine import FeatureEngine
from valxel_sdlc.run_queue import Claim, MemoryRunQueue, Worker

DB = os.environ.get("VALXEL_TEST_DATABASE_URL")
needs_db = pytest.mark.skipif(not DB, reason="set VALXEL_TEST_DATABASE_URL to run Postgres tests")


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


@pytest.fixture
def queued(services):
    q = MemoryRunQueue(clock=Clock())
    return FeatureEngine(services, InMemorySaver(), queue=q), q


def test_a_run_whose_claim_ran_out_mid_run_is_not_run_again_by_the_second_worker(queued):
    """Worker A's claim runs out while it's still running (it stalled); worker B claims the row and
    waits on the thread's lock. A finishes: B must find the run done, not run it a second time."""
    engine, q = queued
    fid = engine.create(product_id="FARM", title="Harvest log")
    runs, a_running, a_go = [], threading.Event(), threading.Event()
    real = engine.run_job

    class Stalling(Claim):
        def still_ours(self):  # A has the thread's lock here, and stalls past its claim
            ours = super().still_ours()
            a_running.set()
            a_go.wait(5)
            return ours

    def counted(thread_id, payload, claim=None):
        runs.append(claim.me)
        if claim.me == "a":
            claim = Stalling(claim.queue, claim.me, claim.job_id)
        real(thread_id, payload, claim)

    engine.run_job = counted
    a = Worker(engine, q, name="a", lease_s=30)
    a.run_once(wait=False)
    assert a_running.wait(5)
    q.clock.t += 31  # A stalled past its lease
    b = Worker(engine, q, name="b", lease_s=30)
    assert b.run_once() == 1  # B claims it, but A has the thread: B hands it back for later
    [row] = q.rows.values()
    assert row["by"] is None
    a_go.set()
    deadline = time.time() + 10
    while a.held:
        assert time.time() < deadline
        time.sleep(0.05)
    assert not q.rows  # A took it off the queue before letting the lock go: nothing left to run twice
    q.clock.t += 60
    assert b.run_once() == 0
    assert runs == ["a", "b"] and [p["gate"] for p in engine.pending(fid)] == ["g1"]


def test_a_worker_that_dies_mid_run_is_taken_over_once_its_claim_runs_out(queued):
    engine, q = queued
    fid = engine.create(product_id="FARM", title="Harvest log")
    dead = q.claim("dead", 30)  # claimed, then the process died: no renewals, no done
    assert dead is not None
    assert Worker(engine, q, name="b", lease_s=30).run_once() == 0  # still claimed
    q.clock.t += 31
    assert Worker(engine, q, name="b", lease_s=30).run_once() == 1
    assert not q.rows and [p["gate"] for p in engine.pending(fid)] == ["g1"]
    q.done("dead", dead.id)  # the dead worker's late word changes nothing


def test_a_live_worker_keeps_its_claim_by_renewing(queued):
    engine, q = queued
    engine.create(product_id="FARM", title="Harvest log")
    job = q.claim("a", 30)
    w = Worker(engine, q, name="a", lease_s=30)
    w.held.add(job.id)
    for _ in range(4):  # two minutes of renewals every ten seconds
        q.clock.t += 10
        w.renew()
        q.clock.t += 10
        assert q.claim("b", 30) is None
    w.held.clear()


def test_the_lock_skips_a_run_that_is_no_longer_the_workers(queued):
    engine, q = queued
    fid = engine.create(product_id="FARM", title="Harvest log")
    job = q.claim("a", 30)
    q.finish(job.id)  # someone else ran it and took it off
    engine.run_job(job.thread_id, job.payload, Claim(q, "a", job.id))
    assert engine.pending(fid) == [] and fid not in engine.running  # skipped: nothing ran


def test_a_run_that_fails_is_still_taken_off_the_queue_inside_the_lock(queued, monkeypatch):
    engine, q = queued
    fid = engine.create(product_id="FARM", title="Harvest log")
    finished_while_locked = []

    class Watching(Claim):
        def finished(self):
            finished_while_locked.append(engine.cluster.lock(fid) is None)  # someone (us) still holds it
            super().finished()

    job = q.claim("a", 30)
    monkeypatch.setattr(engine, "_g", lambda _fid: (_ for _ in ()).throw(RuntimeError("model down")))
    with pytest.raises(RuntimeError):
        engine.run_job(job.thread_id, job.payload, Watching(q, "a", job.id))
    assert finished_while_locked == [True] and not q.rows and "model down" in engine.errors[fid]


# --------------------------------------------------------------------------- Postgres, real workers


@pytest.fixture
def pg(services):
    from psycopg.rows import dict_row

    from valxel_sdlc import tenancy
    from valxel_sdlc.run_queue import PostgresRunQueue

    with tenancy.make_pool(DB, max_size=8, kwargs={"autocommit": True, "prepare_threshold": 0,
                                                   "row_factory": dict_row}) as pool:
        with tenancy.bound(tenancy.PLATFORM):
            q = PostgresRunQueue(pool)
            with pool.connection() as conn:
                conn.execute("delete from valxel_run_queue")
        yield pool, q
        with tenancy.bound(tenancy.PLATFORM), pool.connection() as conn:
            conn.execute("delete from valxel_run_queue")


@needs_db
def test_two_postgres_workers_run_each_thread_once_and_in_order(services, pg):
    """Ten features through two workers polling one Postgres queue at once: each runs to G1 once."""
    _pool, q = pg
    engine = FeatureEngine(services, InMemorySaver(), queue=q)
    titles = ["Harvest log", "Soil moisture alerts", "Seed inventory", "Tractor maintenance schedule",
              "Weather forecast panel", "Irrigation planner", "Pest sighting reports", "Crop rotation advice",
              "Market price feed", "Field worker timesheets"]
    fids = [engine.create(product_id="FARM", title=t) for t in titles]
    runs: list[str] = []
    real = engine.run_job

    def counted(thread_id, payload, claim=None):
        runs.append(thread_id)
        real(thread_id, payload, claim)

    engine.run_job = counted
    workers = [Worker(engine, q, name=n, concurrency=3, lease_s=30, poll_s=0.05) for n in ("a", "b")]
    threads = [threading.Thread(target=w.serve, daemon=True) for w in workers]
    for t in threads:
        t.start()
    deadline = time.time() + 60
    while not all(engine.pending(f) for f in fids):
        assert time.time() < deadline, "the workers didn't finish"
        time.sleep(0.1)
    for w in workers:
        w.drain(5)
    assert sorted(runs) == sorted(fids)  # each once
    assert all([p["gate"] for p in engine.pending(f)] == ["g1"] for f in fids)
    assert q.depth()["queued"] == 0


@needs_db
def test_postgres_a_claim_that_ran_out_mid_run_doesnt_run_twice(services, pg):
    pool, q = pg
    from valxel_sdlc.tenancy import PLATFORM, bound

    engine = FeatureEngine(services, InMemorySaver(), queue=q)
    fid = engine.create(product_id="FARM", title=f"Harvest log {uuid.uuid4().hex[:4]}")
    with bound(PLATFORM):
        job = q.claim("a", 30)
        lease = engine.cluster.lock(fid)  # A is running it
        with pool.connection() as conn:  # and stalls past its claim
            conn.execute("update valxel_run_queue set claimed_until = now() - interval '1 second'")
        again = q.claim("b", 30)
        assert again is not None and again.id == job.id and not q.owns("a", job.id)
        lease.release()
        engine.run_job(job.thread_id, job.payload, Claim(q, "a", job.id))  # A carries on: no longer its row
        assert engine.pending(fid) == []  # A skipped it: B owns it now
        engine.run_job(again.thread_id, again.payload, Claim(q, "b", again.id))
        assert [p["gate"] for p in engine.pending(fid)] == ["g1"] and q.depth()["queued"] == 0

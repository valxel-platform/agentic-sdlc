"""The run queue: the API queues, workers run (valxel_sdlc.run_queue). Postgres tests need
VALXEL_TEST_DATABASE_URL; the in-memory ones always run."""

import os
import uuid

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from valxel_sdlc.engine import FeatureEngine
from valxel_sdlc.run_queue import MemoryRunQueue, Worker, decode, encode

DB = os.environ.get("VALXEL_TEST_DATABASE_URL")
needs_db = pytest.mark.skipif(not DB, reason="set VALXEL_TEST_DATABASE_URL to run Postgres tests")


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def test_what_a_thread_is_run_with_survives_the_queue():
    for payload in (None, {"feature_id": "F-1", "governance": {"pins": ["abc"]}}):
        assert decode(*encode(payload)) == payload
    for cmd in (Command(resume={"i-1": {"decision": "approve", "approver": "po"}}),
                Command(resume={"i-2": {"status": "merged"}}, update={"cost_usd": 1.5})):
        back = decode(*encode(cmd))
        assert isinstance(back, Command) and back.resume == cmd.resume and back.update == cmd.update


def test_a_threads_runs_go_in_order_and_a_dead_workers_claim_runs_out():
    clock = Clock()
    q = MemoryRunQueue(clock=clock)
    first, second, other = q.enqueue("F-1", None), q.enqueue("F-1", {"x": 1}), q.enqueue("F-2", None)
    a = q.claim("a", lease_s=60)
    assert (a.id, a.thread_id) == (first, "F-1")
    b = q.claim("b", lease_s=60)
    assert b.id == other  # F-1's second run waits for its first
    assert q.claim("b", lease_s=60) is None
    clock.t += 61  # worker a died: its claim runs out
    again = q.claim("b", lease_s=60)
    assert again.id == first and again.attempts == 2
    q.done("a", first)  # a's late word counts for nothing: b holds it now
    assert first in q.rows
    q.done("b", first)
    assert q.claim("b", lease_s=60).id == second
    assert q.queued_among(["F-1", "F-3"]) == {"F-1"}


def test_a_run_handed_back_waits_and_isnt_counted_as_an_attempt():
    clock = Clock()
    q = MemoryRunQueue(clock=clock)
    j = q.enqueue("F-1", None)
    q.claim("a", lease_s=60)
    q.later("a", j, 5)
    assert q.claim("a", lease_s=60) is None
    clock.t += 5
    assert q.claim("a", lease_s=60).attempts == 1


@pytest.fixture
def queued(services):
    q = MemoryRunQueue()
    return FeatureEngine(services, InMemorySaver(), queue=q), q


def test_with_a_queue_the_api_queues_and_a_worker_runs(queued):
    engine, q = queued
    fid = engine.create(product_id="FARM", title="Harvest log")  # not in the background: still only queued
    assert engine.pending(fid) == [] and engine.is_running(fid) and engine.view(fid)["running"] is True
    assert Worker(engine, q, concurrency=2).run_once() == 1
    [g1] = engine.pending(fid)
    assert g1["gate"] == "g1" and not engine.is_running(fid) and not q.rows
    v = engine.decide(fid, g1["interrupt_id"], {"decision": "approve", "approver": "po"})
    assert v["running"] is True and [p["gate"] for p in engine.pending(fid)] == ["g1"]
    with pytest.raises(RuntimeError, match="still running"):  # queued counts as running
        engine.decide(fid, g1["interrupt_id"], {"decision": "approve", "approver": "po"})
    Worker(engine, q).run_once()
    assert [p["gate"] for p in engine.pending(fid)] == ["g2"]


def test_a_thread_another_worker_runs_is_tried_again_later(queued):
    engine, q = queued
    fid = engine.create(product_id="FARM", title="Harvest log")
    lease = engine.cluster.lock(fid)  # another worker has it
    Worker(engine, q).run_once()
    [row] = q.rows.values()
    assert row["by"] is None and row["attempts"] == 0 and row["available"] > q.clock()
    lease.release()


def test_a_run_whose_worker_kept_dying_is_dropped_with_a_note(queued):
    engine, q = queued
    fid = engine.create(product_id="FARM", title="Harvest log")
    q.rows[next(iter(q.rows))]["attempts"] = 3  # claimed three times, never finished
    Worker(engine, q, max_attempts=3).run_once()
    assert not q.rows and "never finished" in engine.errors.get(fid) and engine.pending(fid) == []
    from valxel_sdlc.run_queue import DEAD_LETTERS

    [dead] = engine.cluster.get(DEAD_LETTERS)  # counted by the tool's health
    assert dead["thread_id"] == fid and dead["attempts"] == 3


def test_stopping_hands_back_what_is_still_running(queued):
    import threading

    engine, q = queued
    fid = engine.create(product_id="FARM", title="Harvest log")
    started, release = threading.Event(), threading.Event()

    def slow(thread_id, payload):
        started.set()
        release.wait(5)

    engine.run_job = slow
    w = Worker(engine, q)
    w.run_once(wait=False)
    assert started.wait(5)
    assert w.drain(timeout_s=0.3) == [next(iter(q.rows))]
    [row] = q.rows.values()
    assert row["by"] is None and row["attempts"] == 0 and row["thread_id"] == fid
    release.set()


def test_strict_mode_wants_the_queue_in_live_mode(services, monkeypatch):
    from valxel_sdlc import strict

    monkeypatch.setattr(services.settings, "mode", "live")
    monkeypatch.setattr(services.settings, "database_url", "postgresql://x/y")
    found = strict.stand_ins(services, auth_mode="github", env={"VALXEL_DELIVERY": "live"})
    assert any(f.startswith("VALXEL_RUN_QUEUE is not postgres") for f in found)
    found = strict.stand_ins(services, auth_mode="github", env={"VALXEL_DELIVERY": "live", "VALXEL_RUN_QUEUE": "postgres", "VALXEL_CHECKPOINT_ARCHIVE_BUCKET": "valxel-checkpoints", "VALXEL_SIGNALS_TOKENS": "scoped", "VALXEL_GITHUB_APP_KMS_KEY_ID": "arn:aws:kms:us-east-2:1:key/app"})
    assert not any(f.startswith("VALXEL_RUN_QUEUE") for f in found)


# --------------------------------------------------------------------------- Postgres


@pytest.fixture
def pool():
    from psycopg.rows import dict_row

    from valxel_sdlc import tenancy

    with tenancy.make_pool(DB, max_size=4, kwargs={"autocommit": True, "prepare_threshold": 0,
                                                   "row_factory": dict_row}) as p:
        yield p


@needs_db
def test_postgres_workers_never_claim_the_same_run_and_take_over_a_dead_ones(pool):
    from valxel_sdlc.run_queue import PostgresRunQueue
    from valxel_sdlc.tenancy import PLATFORM, bound

    with bound(PLATFORM):
        q = PostgresRunQueue(pool)
        tag = uuid.uuid4().hex[:8]
        with pool.connection() as conn:
            conn.execute("delete from valxel_run_queue")
        f1, f2 = f"F-{tag}-1", f"F-{tag}-2"
        first = q.enqueue(f1, Command(resume={"i": {"decision": "approve"}}), workspace="wrkspc-shared")
        q.enqueue(f1, None)
        other = q.enqueue(f2, {"feature_id": f2})
        a, b = q.claim("a", 30), q.claim("b", 30)
        assert {a.id, b.id} == {first, other} and q.claim("c", 30) is None
        mine, dead = (a, "a") if a.id == first else (b, "b")
        assert mine.workspace == "wrkspc-shared" and mine.payload.resume == {"i": {"decision": "approve"}}
        assert q.queued_among([f1, f2, "nope"]) == {f1, f2} and q.depth()["running"] == 2
        with pool.connection() as conn:  # the worker holding it died: its claim ran out
            conn.execute("update valxel_run_queue set claimed_until = now() - interval '1 second' where id = %s",
                         (first,))
        again = q.claim("c", 30)
        assert again.id == first and again.attempts == 2
        q.done(dead, first)  # the dead worker's word comes too late: ignored
        assert q.queued_among([f1]) == {f1}
        q.done("c", first)
        nxt = q.claim("c", 30)
        assert nxt.thread_id == f1 and nxt.payload is None
        q.later("c", nxt.id, 60)
        assert q.claim("c", 30) is None  # handed back for later
        with pool.connection() as conn:
            conn.execute("delete from valxel_run_queue")

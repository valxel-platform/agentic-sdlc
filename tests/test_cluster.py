"""Several replicas: one runs a thread at a time and every replica can tell; one watches; GitHub is
asked once; live updates and intake chats reach every replica. Postgres tests need
VRIXEL_TEST_DATABASE_URL (CI starts one); the in-memory ones always run."""

import asyncio
import os
import uuid

import pytest

from vrixel_sdlc.cluster import MemoryCluster, Notes

DB = os.environ.get("VRIXEL_TEST_DATABASE_URL")
needs_db = pytest.mark.skipif(not DB, reason="set VRIXEL_TEST_DATABASE_URL to run Postgres tests")


def test_in_one_process_a_thread_runs_once_and_notes_are_kept():
    c = MemoryCluster()
    lease = c.lock("F-1")
    assert lease and c.lock("F-1") is None and c.busy("F-1") and c.busy_among(["F-1", "F-2"]) == {"F-1"}
    lease.release()
    lease.release()
    assert not c.busy("F-1") and c.leader("watcher")
    assert c.claim("k") and not c.claim("k")
    c.unclaim("k")
    assert c.claim("k") and c.prune() == 0
    errors = Notes(c, "error")
    errors["F-1"] = "boom"
    assert errors.get("F-1") == "boom" and errors.pop("F-1") == "boom" and errors.get("F-2") is None
    with pytest.raises(KeyError):
        del errors["F-1"]
    errors["F-1"] = "x"
    del errors["F-1"]
    assert list(errors) == [] and len(errors) == 0


def test_a_decision_waits_while_the_thread_runs_elsewhere(engine):
    fid = engine.start(product_id="FARM", title="Harvest log")["feature_id"]
    lease = engine.cluster.lock(fid)  # as if another replica were running it
    assert engine.view(fid)["running"] is True
    [p] = engine.pending(fid)
    with pytest.raises(RuntimeError, match="still running"):
        engine.decide(fid, p["interrupt_id"], {"decision": "approve", "approver": "po"})
    assert engine.poke(fid) is False
    lease.release()
    v = engine.decide(fid, p["interrupt_id"], {"decision": "approve", "approver": "po"})
    assert [x["gate"] for x in v["pending"]] == ["g2"] and v["running"] is False


# --------------------------------------------------------------------------- Postgres


@pytest.fixture
def pool():
    from psycopg.rows import dict_row
    from psycopg_pool import ConnectionPool

    with ConnectionPool(DB, open=True, kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row}) as p:
        yield p


@needs_db
def test_one_replica_holds_a_thread_and_a_dead_one_lets_go(pool):
    import psycopg

    from vrixel_sdlc.cluster import PostgresCluster

    a, b = PostgresCluster(pool, DB), PostgresCluster(pool, DB)
    fid = f"T-{uuid.uuid4().hex[:8]}"
    lease = a.lock(fid)
    assert lease is not None and b.lock(fid) is None
    assert b.busy(fid) and b.busy_among([fid, "other"]) == {fid}
    pid = lease.conn.execute("select pg_backend_pid()").fetchone()[0]
    with psycopg.connect(DB, autocommit=True) as admin:
        admin.execute("select pg_terminate_backend(%s)", (pid,))
    assert not lease.alive() and not b.busy(fid)
    taken = b.lock(fid)
    assert taken is not None
    taken.release()
    lease.release()


@needs_db
def test_one_replica_watches_and_another_takes_over(pool):
    from vrixel_sdlc.cluster import PostgresCluster

    role = f"watcher-{uuid.uuid4().hex[:6]}"
    a, b = PostgresCluster(pool, DB), PostgresCluster(pool, DB)
    assert a.leader(role) and a.leader(role) and not b.leader(role)
    a._leads[role].release()  # replica a goes away
    assert b.leader(role) and not a.leader(role)


@needs_db
def test_claims_and_notes_are_shared(pool):
    from vrixel_sdlc.cluster import PostgresCluster

    a, b = PostgresCluster(pool, DB), PostgresCluster(pool, DB)
    key = f"dispatch:{uuid.uuid4()}"
    assert a.claim(key) and not b.claim(key)
    b.unclaim(key)
    assert a.claim(key)
    a.put(f"error:{key}", "TypeError: x")
    assert Notes(b, "error").get(key) == "TypeError: x"
    b.put(f"error:{key}", None)
    assert a.get(f"error:{key}") is None and a.prune(30) >= 0


@needs_db
def test_github_is_asked_once_whichever_replica_checks(pool, services):
    from vrixel_sdlc.cluster import PostgresCluster
    from vrixel_sdlc.waits import WaitChecker

    since = str(uuid.uuid4())
    packet = {"feature_id": "F", "wait": "staging", "since": since, "quality": {"targets": [
        {"repo": "o/svc", "sha": "1", "perf": "none"}]}}
    for _ in range(2):
        WaitChecker(services, PostgresCluster(pool, DB))._start_post_deploy(packet, [{"repo": "o/svc", "url": "u"}])
    sent = [d for d in services.github.dispatches if d["event_type"] == "vrixel-deployed"]
    assert len(sent) == 1


@needs_db
def test_live_updates_reach_every_replica(pool):
    from vrixel_sdlc.events import PostgresBus

    here, there = PostgresBus(pool, DB), PostgresBus(pool, DB)

    async def follow():
        q = there.subscribe()
        await asyncio.sleep(0.5)  # the listeners are connected
        fid = f"F-{uuid.uuid4().hex[:6]}"
        await asyncio.to_thread(here.publish, {"type": "feature.updated", "feature_id": fid, "big": "x" * 9000})
        while True:
            e = await asyncio.wait_for(q.get(), 10)
            if e.get("feature_id") == fid:
                return e

    e = asyncio.run(follow())
    assert e["type"] == "feature.updated" and "big" not in e  # too big for NOTIFY: the essentials


@needs_db
def test_an_intake_chat_continues_on_another_replica(pool):
    from vrixel_sdlc.intake import PostgresIntakeStore

    a, b = PostgresIntakeStore(pool), PostgresIntakeStore(pool)
    owner = f"u-{uuid.uuid4().hex[:6]}"
    s = a.create(owner, ["FARM"])
    s.messages.append({"role": "agent", "text": "What would you like to build?", "at": s.created_at})
    s.draft.problem = "Farmers lose harvest notes offline"
    a.save(s)
    got = b.get(s.id, owner)
    assert got.messages[0]["text"].startswith("What") and got.draft.problem.startswith("Farmers")
    assert got.draft.missing() == ["product_id", "users", "success_metric"]
    assert [x.id for x in b.list(owner)] == [s.id]
    with pytest.raises(KeyError):
        b.get(s.id, "someone-else")

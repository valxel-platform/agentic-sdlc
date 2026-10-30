"""Durability against real Postgres: a thread waits at a gate, the process 'restarts' (a new
engine on the same database), and the decision resumes the thread. Skipped without a DB."""

import os
import uuid

import pytest

DB = os.environ.get("VRIXEL_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not DB, reason="set VRIXEL_TEST_DATABASE_URL to run Postgres tests")


def _engine(settings, pool):
    from langgraph.checkpoint.postgres import PostgresSaver

    from vrixel_sdlc.engine import FeatureEngine, PostgresIndex
    from vrixel_sdlc.gateway import PostgresLedger
    from vrixel_sdlc.services import build_services

    saver = PostgresSaver(pool)
    saver.setup()
    return FeatureEngine(build_services(settings, ledger=PostgresLedger(pool)), saver, PostgresIndex(pool))


def test_threads_survive_a_restart_and_usage_is_persisted(settings):
    from psycopg.rows import dict_row
    from psycopg_pool import ConnectionPool

    with ConnectionPool(DB, kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row}) as pool:
        with pool.connection() as conn:
            conn.execute("DROP TABLE IF EXISTS vrixel_features")
        title = f"Durable feature {uuid.uuid4().hex[:6]}"
        first = _engine(settings, pool)
        v = first.start(product_id="EMR", title=title)
        fid = v["feature_id"]
        assert [p["gate"] for p in v["pending"]] == ["g1"]

        restarted = _engine(settings, pool)  # same database, fresh process state
        [item] = [i for i in restarted.inbox() if i["feature_id"] == fid]
        v = restarted.decide(fid, item["interrupt_id"], {"decision": "approve", "approver": "po"})
        assert [p["gate"] for p in v["pending"]] == ["g2"]

        v = restarted.auto_drive(fid)
        assert v["status"] == "done"
        records = restarted.services.ledger.records(fid)
        assert len(records) == v["usage"]["calls"] > 10
        assert records[0].prefix_hashes and records[0].product_id == "EMR"

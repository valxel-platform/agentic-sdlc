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

    from vrixel_sdlc.integrations.catalog import PostgresCatalog

    saver = PostgresSaver(pool)
    saver.setup()
    return FeatureEngine(build_services(settings, ledger=PostgresLedger(pool), catalog=PostgresCatalog(pool)),
                         saver, PostgresIndex(pool))


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


def test_released_features_and_the_duplicate_check_survive_a_restart(settings):
    from psycopg.rows import dict_row
    from psycopg_pool import ConnectionPool

    from vrixel_sdlc.duplicates import DuplicateIntent

    with ConnectionPool(DB, kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row}) as pool:
        with pool.connection() as conn:
            conn.execute("DROP TABLE IF EXISTS vrixel_features")
            conn.execute("DROP TABLE IF EXISTS vrixel_capabilities")
        first = _engine(settings, pool)
        fid = first.start(product_id="EMR", title="Medication reconciliation at discharge")["feature_id"]
        assert first.auto_drive(fid)["status"] == "done"

        restarted = _engine(settings, pool)  # same database, fresh process state
        [cap] = [c for c in restarted.services.catalog.lookup("EMR") if c.get("feature") == fid]
        assert cap["status"] == "live"
        assert [f["status"] for f in restarted.feature_summaries("EMR") if f["feature_id"] == fid] == ["released"]
        with pytest.raises(DuplicateIntent, match="already released"):
            restarted.create(product_id="EMR", title="Medication reconciliation at discharge")


def test_initiatives_and_releases_share_the_index_and_survive_a_restart(settings):
    from psycopg.rows import dict_row
    from psycopg_pool import ConnectionPool

    with ConnectionPool(DB, kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row}) as pool:
        with pool.connection() as conn:
            conn.execute("DROP TABLE IF EXISTS vrixel_features")
            # An index created before initiatives existed: no kind column yet.
            conn.execute("""CREATE TABLE vrixel_features (feature_id TEXT PRIMARY KEY, product_id TEXT NOT NULL,
                            title TEXT NOT NULL, seq INT NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT now())""")
            conn.execute("INSERT INTO vrixel_features VALUES ('EMR-FEAT-1', 'EMR', 'Old feature', 1, now())")
        first = _engine(settings, pool)
        iid = first.create_initiative(product_id="EMR", title="Walk-in intake",
                                      request="Register walk-in patients; Capture consent at registration")
        assert iid == "EMR-INI-1"
        fid = first.create(product_id="EMR", title="Lab results viewer")
        assert fid == "EMR-FEAT-2"  # numbering per kind; the old row counts as a feature

        restarted = _engine(settings, pool)
        assert restarted.view(iid)["status"] == "awaiting_approval"
        assert [i["kind"] for i in restarted.inbox("EMR") if i["feature_id"] == iid] == ["initiative"]

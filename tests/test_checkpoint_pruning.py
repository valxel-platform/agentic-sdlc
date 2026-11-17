"""A thread keeps only its latest checkpoint in Postgres; runs on VALXEL_TEST_DATABASE_URL."""

import os

import pytest

DB = os.environ.get("VALXEL_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not DB, reason="set VALXEL_TEST_DATABASE_URL to run Postgres tests")


@pytest.fixture
def pg_engine(tmp_path):
    from langgraph.checkpoint.postgres import PostgresSaver
    from psycopg.rows import dict_row
    from psycopg_pool import ConnectionPool

    from valxel_sdlc.config import load_settings
    from valxel_sdlc.engine import FeatureEngine
    from valxel_sdlc.services import build_services

    with ConnectionPool(DB, open=True, kwargs={"autocommit": True, "prepare_threshold": 0,
                                               "row_factory": dict_row}) as pool:
        saver = PostgresSaver(pool)
        saver.setup()
        settings = load_settings(mode="stub", database_url="", artifact_dir=tmp_path / "a", governance_source="local")
        yield FeatureEngine(build_services(settings), saver), pool


def test_a_finished_feature_keeps_one_checkpoint_per_namespace_and_its_whole_state(pg_engine):
    engine, pool = pg_engine
    v = engine.auto_drive(engine.start(product_id="FARM", title="Offline crop log")["feature_id"])
    fid = v["feature_id"]
    with pool.connection() as conn:
        rows = conn.execute("select checkpoint_ns, count(*) as n from checkpoints where thread_id = %s"
                            " group by checkpoint_ns", (fid,)).fetchall()
        writes = conn.execute("select count(*) as n from checkpoint_writes where thread_id = %s", (fid,)).fetchone()
    assert rows and all(r["n"] == 1 for r in rows) and writes["n"] == 0
    after = engine.view(fid)  # read back from what was kept
    assert after["status"] == "done" and len(after["events"]) == len(v["events"]) > 10
    assert after["gate_decisions"] == v["gate_decisions"]


def test_a_gate_waits_and_is_decided_after_pruning(pg_engine):
    from conftest import approve

    engine, _ = pg_engine
    fid = engine.start(product_id="CLN", title="Consent check on intake")["feature_id"]
    for gate in ("g1", "g2", "g3", "g4", "g5"):
        approve(engine, fid, gate)  # each run ends in a pruned checkpoint waiting on the next gate
    assert approve(engine, fid, "g6")["status"] == "done"

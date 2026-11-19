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


class FakeS3:
    def __init__(self, fail=False):
        self.objects, self.fail = {}, fail

    def put_object(self, **kw):
        if self.fail:
            raise ConnectionError("s3 unreachable")
        self.objects[kw["Key"]] = kw


def counts(pool, fid):
    with pool.connection() as conn:
        return conn.execute("select count(*) as n from checkpoints where thread_id = %s", (fid,)).fetchone()["n"]


def test_a_regulated_lanes_history_is_archived_before_it_is_pruned(pg_engine):
    from conftest import pending_of

    from valxel_sdlc.checkpoint_archive import Archive, read

    engine, pool = pg_engine
    s3 = FakeS3()
    engine.checkpoint_archive = Archive("valxel-checkpoints", 2190, s3=s3)
    fid = engine.start(product_id="CLN", title="Consent check on intake")["feature_id"]  # PHI
    [key] = s3.objects
    obj = s3.objects[key]
    ws = engine.services.settings.registry["CLN"].workspace
    assert key.startswith(f"checkpoints/{ws}/CLN/{fid}/") and obj["ObjectLockMode"] == "COMPLIANCE"
    assert obj["ServerSideEncryption"] == "aws:kms" and obj["Metadata"]["sha256"][:16] in key
    # Its workspace and product: as tags (the bucket's read rule) and in the data key's context.
    assert obj["Tagging"] == f"workspace={ws}&product=CLN" and obj["BucketKeyEnabled"] is False
    import base64
    import json

    assert json.loads(base64.b64decode(obj["SSEKMSEncryptionContext"])) == {
        "valxel:workspace": ws, "valxel:product": "CLN"}
    records = read(obj["Body"], engine.checkpointer.serde)
    assert len(records) == int(obj["Metadata"]["checkpoints"]) > 1  # every step's state, not only the last
    assert any(r["checkpoint"]["channel_values"].get("title") == "Consent check on intake" for r in records)
    assert counts(pool, fid) <= len({r["checkpoint_ns"] for r in records})  # then pruned to the latest
    assert pending_of(engine, fid, "g1")


def test_without_an_archive_a_regulated_lane_keeps_its_history(pg_engine):
    from valxel_sdlc.checkpoint_archive import Archive

    engine, pool = pg_engine
    engine.checkpoint_archive = None
    fid = engine.start(product_id="CLN", title="Allergy list")["feature_id"]
    kept = counts(pool, fid)
    assert kept > 2  # not pruned: nowhere to archive it
    engine.checkpoint_archive = Archive("valxel-checkpoints", s3=FakeS3(fail=True))
    engine._prune(fid)
    assert counts(pool, fid) == kept  # the upload failed: still not pruned


def test_other_lanes_prune_without_an_archive(pg_engine):
    from valxel_sdlc.checkpoint_archive import Archive

    engine, pool = pg_engine
    s3 = FakeS3()
    engine.checkpoint_archive = Archive("valxel-checkpoints", s3=s3)
    fid = engine.start(product_id="FARM", title="Yield chart")["feature_id"]  # general
    assert s3.objects == {} and counts(pool, fid) <= 3


def test_a_name_that_could_escape_its_folder_is_refused():
    import pytest as _pytest

    from valxel_sdlc.checkpoint_archive import Archive, NotArchived

    class Saver:
        def list(self, _):
            return iter(())

    with _pytest.raises(NotArchived):
        Archive("b", s3=FakeS3()).put(Saver(), "../CLN", "t", workspace="clinic")

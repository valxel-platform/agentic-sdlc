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
        v = first.start(product_id="CLN", title=title)
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
        assert records[0].prefix_hashes and records[0].product_id == "CLN"


def test_released_features_and_the_duplicate_check_survive_a_restart(settings):
    from psycopg.rows import dict_row
    from psycopg_pool import ConnectionPool

    from vrixel_sdlc.duplicates import DuplicateIntent

    with ConnectionPool(DB, kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row}) as pool:
        with pool.connection() as conn:
            conn.execute("DROP TABLE IF EXISTS vrixel_features")
            conn.execute("DROP TABLE IF EXISTS vrixel_capabilities")
        first = _engine(settings, pool)
        fid = first.start(product_id="CLN", title="Medication reconciliation at discharge")["feature_id"]
        assert first.auto_drive(fid)["status"] == "done"

        restarted = _engine(settings, pool)  # same database, fresh process state
        [cap] = [c for c in restarted.services.catalog.lookup("CLN") if c.get("feature") == fid]
        assert cap["status"] == "live"
        assert [f["status"] for f in restarted.feature_summaries("CLN") if f["feature_id"] == fid] == ["released"]
        with pytest.raises(DuplicateIntent, match="already released"):
            restarted.create(product_id="CLN", title="Medication reconciliation at discharge")


def test_initiatives_and_releases_share_the_index_and_survive_a_restart(settings):
    from psycopg.rows import dict_row
    from psycopg_pool import ConnectionPool

    with ConnectionPool(DB, kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row}) as pool:
        with pool.connection() as conn:
            conn.execute("DROP TABLE IF EXISTS vrixel_features")
            # An index created before initiatives existed: no kind column yet.
            conn.execute("""CREATE TABLE vrixel_features (feature_id TEXT PRIMARY KEY, product_id TEXT NOT NULL,
                            title TEXT NOT NULL, seq INT NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT now())""")
            conn.execute("INSERT INTO vrixel_features VALUES ('CLN-FEAT-1', 'CLN', 'Old feature', 1, now())")
        first = _engine(settings, pool)
        iid = first.create_initiative(product_id="CLN", title="Walk-in intake",
                                      request="Register walk-in patients; Capture consent at registration")
        assert iid == "CLN-INI-1"
        fid = first.create(product_id="CLN", title="Lab results viewer")
        assert fid == "CLN-FEAT-2"  # numbering per kind; the old row counts as a feature

        restarted = _engine(settings, pool)
        assert restarted.view(iid)["status"] == "awaiting_approval"
        assert [i["kind"] for i in restarted.inbox("CLN") if i["feature_id"] == iid] == ["initiative"]


def test_incidents_and_their_signals_survive_a_restart(settings):
    from psycopg.rows import dict_row
    from psycopg_pool import ConnectionPool

    from vrixel_sdlc.ops.signals import PostgresSignalStore, from_alertmanager

    alert = {"alerts": [{"status": "firing", "fingerprint": "pg1", "startsAt": "2026-11-03T21:00:00Z",
                         "labels": {"alertname": "HighErrorRate", "severity": "critical", "component": "cln-intake-api"},
                         "annotations": {"summary": "5xx"}}]}
    with ConnectionPool(DB, kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row}) as pool:
        with pool.connection() as conn:
            conn.execute("DROP TABLE IF EXISTS vrixel_signals")
            conn.execute("DROP TABLE IF EXISTS vrixel_unmatched_signals")
            conn.execute("DROP TABLE IF EXISTS vrixel_features")
        first = _engine(settings, pool)
        first.services.signals = PostgresSignalStore(pool)
        [iid] = first.ingest(from_alertmanager(alert))

        restarted = _engine(settings, pool)
        restarted.services.signals = PostgresSignalStore(pool)
        assert restarted.ingest(from_alertmanager(alert)) == [iid]  # joins the open incident
        assert len(restarted.services.signals.for_incident(iid)) == 2
        assert [p["gate"] for p in restarted.pending(iid)] == ["incident"]

        # A signal for no product is kept, with why, across the restart.
        stray = {"alerts": [{**alert["alerts"][0], "fingerprint": "pg-stray",
                             "labels": {**alert["alerts"][0]["labels"], "component": "billing-cron"}}]}
        assert restarted.ingest(from_alertmanager(stray)) == []
        [got] = [s for s in PostgresSignalStore(pool).unmatched() if s["fingerprint"] == "prometheus:pg-stray"]
        assert got["reason"].startswith("component billing-cron matches no product") and got["received_at"]


def test_cost_records_and_reviews_persist():
    from psycopg.rows import dict_row
    from psycopg_pool import ConnectionPool

    from vrixel_sdlc.finops.ledger import CostRecord, PostgresCostLedger
    from vrixel_sdlc.ops.cost import PostgresCostReviews

    pid = f"P{uuid.uuid4().hex[:6]}"
    with ConnectionPool(DB, kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row}) as pool:
        ledger = PostgresCostLedger(pool)
        r = dict(product_id=pid, stage="build", category="ci", period_start="2026-11-02T10:00:00+00:00",
                 period_end="2026-11-02T10:05:00+00:00", feature_id=f"{pid}-FEAT-1")
        ledger.upsert([CostRecord(key=f"ci:{pid}:1", usd=1.0, **r), CostRecord(key=f"ci:{pid}:2", usd=2.0, **r)])
        ledger.upsert([CostRecord(key=f"ci:{pid}:1", usd=1.5, **r)])
        assert sorted(x.usd for x in PostgresCostLedger(pool).query(product_id=pid)) == [1.5, 2.0]
        assert ledger.query(product_id=pid, start="2026-11-03T00:00:00+00:00") == []

        reviews = PostgresCostReviews(pool)
        reviews.add({"feature_id": f"{pid}-FEAT-1", "product_id": pid, "at": "2026-11-09T00:00:00+00:00",
                     "review_day": 7, "findings": []})
        assert [x["review_day"] for x in PostgresCostReviews(pool).for_product(pid)] == [7]


def test_alarm_states_persist():
    from psycopg.rows import dict_row
    from psycopg_pool import ConnectionPool

    from vrixel_sdlc.ops.alarms import PostgresAlarmBoard

    pid = f"P{uuid.uuid4().hex[:6]}"
    sig = {"fingerprint": f"cloudwatch:1:{pid}", "product_id": pid, "status": "firing", "env": "staging",
           "at": "2026-11-06T10:00:00Z", "title": "t", "severity": "sev3"}
    with ConnectionPool(DB, kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row}) as pool:
        board = PostgresAlarmBoard(pool)
        board.record(sig)
        board.record({**sig, "at": "2026-11-06T11:00:00Z"})
        [s] = PostgresAlarmBoard(pool).firing(pid)
        assert s["since"] == "2026-11-06T10:00:00Z"
        board.record({**sig, "status": "resolved", "at": "2026-11-06T12:00:00Z"})
        board.record({**sig, "at": "2026-11-06T11:30:00Z"})  # late
        assert board.firing(pid) == []


def test_incident_signals_move_between_incidents():
    from psycopg.rows import dict_row
    from psycopg_pool import ConnectionPool

    from vrixel_sdlc.ops.signals import PostgresSignalStore

    pid = f"P{uuid.uuid4().hex[:6]}"
    sig = {"fingerprint": "f1", "component": "web", "title": "t", "status": "firing"}
    with ConnectionPool(DB, kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row}) as pool:
        store = PostgresSignalStore(pool)
        store.add(f"{pid}-INC-1", pid, sig)
        store.add(f"{pid}-INC-2", pid, {**sig, "fingerprint": "f2"})
        store.add(f"{pid}-INC-2", pid, {**sig, "fingerprint": "f3"})
        assert store.open_incidents(pid) == [f"{pid}-INC-1", f"{pid}-INC-2"]
        assert store.move(f"{pid}-INC-2", f"{pid}-INC-1", "f2") == 1
        assert store.move(f"{pid}-INC-2", f"{pid}-INC-1") == 1
        assert [s["fingerprint"] for s in store.for_incident(f"{pid}-INC-1")] == ["f1", "f2", "f3"]
        assert store.open_incidents(pid) == [f"{pid}-INC-1"]


def test_the_change_log_chains_in_postgres_and_shows_tampering():
    from psycopg.rows import dict_row
    from psycopg_pool import ConnectionPool

    from vrixel_sdlc.ops import changes
    from vrixel_sdlc.ops.inventory import PostgresSnapshots

    with ConnectionPool(DB, kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row}) as pool:
        with pool.connection() as conn:
            conn.execute("DROP TABLE IF EXISTS vrixel_changes")
            conn.execute("DROP TABLE IF EXISTS vrixel_change_incidents")
            conn.execute("DROP TABLE IF EXISTS vrixel_inventory_snapshots")
        log = changes.PostgresChangeLog(pool)

        def rec(key, product="CLN"):
            return changes.new(key=key, product_id=product, kind="release", type="standard", action="deploy",
                               summary=key, targets=[{"kind": "component", "name": "api"}],
                               approvals=[{"gate": "g6", "approver": "rm", "at": "2026-11-06T10:00:00+00:00"}])

        a, b = log.record(rec("a")), log.record(rec("b", "FARM"))
        assert (a["id"], b["id"]) == ("CHG-1", "CHG-2") and b["prev_hash"] == a["hash"]
        assert log.record(rec("a"))["id"] == "CHG-1"
        assert [r["id"] for r in log.list("FARM")] == ["CHG-2"]
        assert changes.verify(changes.PostgresChangeLog(pool).all())["ok"]  # a restart reads the same chain
        log.link("CHG-1", "CLN-INC-1", "undone by CHG-3")
        log.link("CHG-1", "CLN-INC-1", "again")
        assert [(x["change_id"], x["incident_id"], x["product_id"]) for x in log.links("CLN")] == [
            ("CHG-1", "CLN-INC-1", "CLN")]
        with pool.connection() as conn:
            conn.execute("UPDATE vrixel_changes SET data = jsonb_set(data, '{summary}', '\"edited\"') WHERE id = 'CHG-1'")
        assert changes.verify(log.all()) == {"ok": False, "records": 2, "broken_at": "CHG-1"}

        snaps = PostgresSnapshots(pool)
        snaps.put({"product_id": "FARM", "day": "2026-11-05", "per_env": {}, "unmanaged": 0})
        snaps.put({"product_id": "FARM", "day": "2026-11-06", "per_env": {}, "unmanaged": 1})
        snaps.put({"product_id": "FARM", "day": "2026-11-06", "per_env": {}, "unmanaged": 2})
        assert [(h["day"], h["unmanaged"]) for h in snaps.history("FARM")] == [("2026-11-05", 0), ("2026-11-06", 2)]


def test_servicenow_receipts_are_kept_in_postgres():
    from psycopg.rows import dict_row
    from psycopg_pool import ConnectionPool

    from vrixel_sdlc.ops.servicenow import PostgresReceipts

    with ConnectionPool(DB, kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row}) as pool:
        with pool.connection() as conn:
            conn.execute("DROP TABLE IF EXISTS vrixel_change_receipts")
        store = PostgresReceipts(pool)
        store.put("CHG-1", {"status": "queued"})
        store.put("CHG-1", {"status": "closed", "number": "CHG0030001"})
        got = store.get(["CHG-1", "CHG-2"])
        assert list(got) == ["CHG-1"] and got["CHG-1"]["number"] == "CHG0030001" and store.get([]) == {}


def test_pilot_effort_and_the_ledgers_new_columns_persist():
    from psycopg.rows import dict_row
    from psycopg_pool import ConnectionPool

    from vrixel_sdlc.gateway import PostgresLedger
    from vrixel_sdlc.gateway.ledger import UsageRecord
    from vrixel_sdlc.pilot import PostgresEffortStore

    fid = f"FARM-FEAT-{uuid.uuid4().hex[:8]}"
    with ConnectionPool(DB, kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row}) as pool:
        with pool.connection() as conn:  # an older ledger table, without the new columns
            conn.execute("ALTER TABLE IF EXISTS vrixel_usage DROP COLUMN IF EXISTS config_version")
            conn.execute("ALTER TABLE IF EXISTS vrixel_usage DROP COLUMN IF EXISTS duration_s")
        ledger = PostgresLedger(pool)
        ledger.record(UsageRecord(ts="2026-11-09T10:00:00+00:00", product_id="FARM", feature_id=fid, phase="spec",
                                  role="spec", runtime="generic", provider="anthropic", model="m", workspace="w",
                                  input_tokens=1, cache_creation_input_tokens=0, cache_read_input_tokens=0,
                                  output_tokens=1, cache_ttl="1h", prompt_version="v1", config_version="abc123",
                                  duration_s=4.5))
        [row] = ledger.records(fid)
        assert row.config_version == "abc123" and row.duration_s == 4.5
        store = PostgresEffortStore(pool)
        store.add({"feature_id": fid, "product_id": "FARM", "kind": "asked", "ref": "i1"})
        store.add({"feature_id": fid, "product_id": "FARM", "kind": "asked", "ref": "i1"})  # first time only
        store.add({"feature_id": fid, "product_id": "FARM", "kind": "pr_review", "who": "maria", "minutes": 10,
                   "detail": {"state": "approved"}})
        got = PostgresEffortStore(pool).events(fid)
        assert [e["kind"] for e in got] == ["asked", "pr_review"] and got[1]["minutes"] == 10.0
        assert got[1]["detail"] == {"state": "approved"} and PostgresEffortStore(pool).asked_at(fid, "i1")

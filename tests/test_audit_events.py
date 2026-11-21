"""T-113: the tool's own audit trail, in the audit sink's contract, through an outbox."""

import json
import os
import uuid
from pathlib import Path

import pytest

from valxel_sdlc import audit_events as ae

ROOT = Path(__file__).resolve().parents[1]
DB = os.environ.get("VALXEL_TEST_DATABASE_URL")


def test_the_packaged_schema_is_the_contract_copy_byte_for_byte():
    assert ae.SCHEMA_FILE.read_bytes() == (ROOT / "contracts/audit/valxel.audit.v1.schema.json").read_bytes()


def test_a_record_meets_the_sink_contract_and_carries_ids_not_values():
    a = ae.Auditor(ae.InMemoryOutbox())
    ev = a.record("gate.decide", actor="ann@acme.example", resource=("feature", ["CLN-F-1"]), workspace="acme",
                  product="CLN", details={"decision": "approve", "nested": {"no": "dicts"}, "notes": "x" * 999,
                                          "budget_usd": None})
    assert ae.problems(ev) == []
    assert ev["type"] == "valxel.audit.v1" and ev["subject"] == "feature/CLN-F-1"
    d = ev["data"]["details"]
    assert d == {"workspace": "acme", "decision": "approve", "notes": "x" * 200}
    with pytest.raises(ValueError):
        a.record("x", actor="a", resource=("r", ["1"]), outcome="maybe")


def test_workspace_entries_are_mirrored_without_names_or_emails():
    a = ae.Auditor(ae.InMemoryOutbox())
    a.from_workspace("acme", "ann", "member.add", {"member": "m-1", "name": "Bob Smith", "email": "bob@x.example",
                                                   "role": "approver"})
    ev = a.outbox.events()[0]
    assert ev["data"]["resource"] == {"type": "member", "ids": ["m-1"]}
    assert "Bob" not in json.dumps(ev) and "bob@" not in json.dumps(ev)
    assert ev["data"]["details"] == {"workspace": "acme", "member": "m-1", "role": "approver"}


def test_the_relay_sends_in_order_and_moves_only_past_what_the_sink_took():
    sink = ae.MemorySink()
    a = ae.Auditor(ae.InMemoryOutbox(), sink)
    for i in range(5):
        a.record("api.post", actor="ann", resource=("features", [str(i)]))
    sink.fail = True
    with pytest.raises(ae.AuditUnavailable):
        a.relay(batch=2)
    assert a.backlog() == 5
    sink.fail = False
    assert a.relay(batch=2) == 5 and a.backlog() == 0 and a.relay() == 0
    assert [e["data"]["resource"]["ids"][0] for e in sink.events] == ["0", "1", "2", "3", "4"]


def test_kafka_sink_reports_undelivered_records():
    class Producer:
        def __init__(self):
            self.cb = []

        def produce(self, topic, key, value, headers, on_delivery):
            self.cb.append(on_delivery)

        def poll(self, _):
            pass

        def flush(self, _):
            for cb in self.cb:
                cb("broker down", None)
            return 0

    k = ae.KafkaSink("b:9092", "audit.v1", producer=Producer())
    k.publish("acme", ae.event("x", actor="a", resource=("r", ["1"])))
    with pytest.raises(ae.AuditUnavailable):
        k.flush()


def test_sink_from_env():
    assert ae.sink_from_env({"VALXEL_AUDIT_SINK": "off"}) is None
    assert isinstance(ae.sink_from_env({}), ae.LogSink)
    with pytest.raises(RuntimeError, match="BOOTSTRAP"):
        ae.sink_from_env({"VALXEL_AUDIT_SINK": "kafka"})


def test_gate_decisions_workspace_changes_and_requests_are_recorded(engine):
    from fastapi.testclient import TestClient

    from valxel_sdlc.api.app import create_app
    from valxel_sdlc.web.auth import AuthConfig

    out = engine.services.audit.outbox
    with TestClient(create_app(engine, auth=AuthConfig(mode="dev", secret="x" * 40), web_dist=None)) as c:
        c.post("/api/auth/dev-login", json={"login": "founder", "email": "f@t113.example", "orgs": ["x"]})
        assert c.post("/api/workspaces", json={"id": "t113-ws", "name": "T113", "plan": "lite"}).status_code == 201
        c.post("/api/auth/dev-login", json={"login": "stranger", "email": "s@else.example", "orgs": ["x"]})
        c.post("/api/workspaces/t113-ws/members", json={"name": "x", "email": "x@x.example", "role": "member"})
        c.post("/api/auth/logout")
    acts = [(e["data"]["action"], e["data"]["actor"]["id"], e["data"]["outcome"]) for e in out.events()]
    assert ("signin", "founder", "success") in acts and ("signout", "stranger", "success") in acts
    assert ("workspace.create", "founder", "success") in acts
    assert ("api.post", "founder", "success") in acts
    assert any(a == "api.post" and who == "stranger" and o == "denied" for a, who, o in acts)
    assert all(ae.problems(e) == [] for e in out.events())


def test_a_gate_decision_is_recorded_before_it_takes_effect(engine, monkeypatch):
    from conftest import approve

    fid = engine.start(product_id="FARM", title="Audit me", description="x")["feature_id"]
    gate = engine.pending(fid)[0]["gate"]
    approve(engine, fid, gate)
    ev = [e for e in engine.services.audit.outbox.events() if e["data"]["action"] == "gate.decide"]
    assert ev and ev[0]["data"]["resource"]["ids"] == [fid]
    assert ev[0]["data"]["details"]["decision"] == "approve" and ev[0]["data"]["actor"]["id"] == f"{gate}-owner"

    def down(*a, **k):
        raise ae.AuditUnavailable("down")

    nxt = engine.pending(fid)[0]
    monkeypatch.setattr(engine.services.audit, "record", down)
    with pytest.raises(ae.AuditUnavailable):
        engine.decide(fid, nxt["interrupt_id"], {"decision": "approve", "approver": "ann"})
    assert engine.pending(fid)[0]["interrupt_id"] == nxt["interrupt_id"]  # unanswered: it didn't happen


def test_a_new_production_change_is_recorded_once(engine):
    from valxel_sdlc.ops import changes

    rec = changes.new(key="t113-chg", product_id="CLN", kind="flag", type="standard", action="off", summary="off",
                      targets=[], requested_by="ann", feature_id="CLN-F-9")
    engine.services.changes.record(rec)
    engine.services.changes.record(dict(rec, at="later"))  # the same key: the earlier record, not a new one
    ev = [e for e in engine.services.audit.outbox.events() if e["data"]["action"] == "change.recorded"]
    assert len(ev) == 1 and "CLN-F-9" in ev[0]["data"]["resource"]["ids"]


@pytest.mark.skipif(not DB, reason="set VALXEL_TEST_DATABASE_URL to run Postgres tests")
def test_the_postgres_outbox_is_append_only_and_the_cursor_moves():
    import psycopg
    from psycopg.rows import dict_row
    from psycopg_pool import ConnectionPool

    with ConnectionPool(DB, kwargs={"autocommit": True, "row_factory": dict_row}, open=True) as pool:
        box = ae.PostgresOutbox(pool)
        ae.PostgresOutbox(pool)
        name = f"t113-{uuid.uuid4().hex[:6]}"
        sink = ae.MemorySink()
        a = ae.Auditor(box, sink)
        a.RELAY = name
        start = box.cursor(name)
        a.record("api.post", actor="ann", resource=("features", ["1"]), workspace="acme")
        assert a.relay() >= 1 and box.cursor(name) > start
        with pool.connection() as conn, pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("DELETE FROM valxel_audit_outbox")

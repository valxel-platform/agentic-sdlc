"""CloudWatch alarms on AWS resources (the platform's standard alarms, through EventBridge): production
ones open and resolve incidents, the rest show at G6."""

import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import InMemorySaver

from vrixel_sdlc.api.app import create_app
from vrixel_sdlc.config import load_settings
from vrixel_sdlc.engine import FeatureEngine
from vrixel_sdlc.ops.alarms import InMemoryAlarmBoard, for_feature
from vrixel_sdlc.ops.signals import from_cloudwatch
from vrixel_sdlc.services import build_services
from vrixel_sdlc.web.auth import AuthConfig

EXAMPLES = Path(__file__).resolve().parents[1] / "examples" / "governance"


def event(state="ALARM", env="production", feature="AG-FEAT-1", resource="field-state", alarm="write-throttled",
          consumers="agtech-field-api", at="2026-11-06T18:00:00.000+0000", **over):
    account = {"production": "333333333333", "staging": "222222222222"}.get(env, "111111111111")
    desc = (f"vrixel product=AG feature={feature} resource={resource} env={env} consumers={consumers} "
            f"alarm={alarm} severity=sev3\nWrites are throttled: the table needs more capacity, or one key is too hot.")
    return {"version": "0", "id": "e1", "detail-type": "CloudWatch Alarm State Change", "source": "aws.cloudwatch",
            "account": account, "time": at, "region": "us-east-2",
            "resources": ["arn:aws:cloudwatch:us-east-2:333333333333:alarm:vx-field-state-1a2b3c4d-write-throttled"],
            "detail": {"alarmName": f"vx-{resource}-1a2b3c4d-{alarm}",
                       "state": {"value": state, "reason": "Threshold Crossed: 2 datapoints were greater than 0.0",
                                 "timestamp": at},
                       "previousState": {"value": "OK"},
                       "configuration": {"description": desc}}, **over}


def test_a_vrixel_alarm_says_whose_it_is():
    [s] = from_cloudwatch(event())
    assert (s["source"], s["status"], s["product_id"], s["env"], s["severity"]) == (
        "cloudwatch", "firing", "AG", "production", "sev3")
    assert s["component"] == "agtech-field-api" and s["title"] == "field-state: write throttled"
    assert s["detail"].startswith("Writes are throttled") and "Threshold Crossed" in s["detail"]
    assert s["labels"]["feature_id"] == "AG-FEAT-1" and s["fingerprint"].startswith("cloudwatch:333333333333:vx-")
    assert s["url"].startswith("https://us-east-2.console.aws.amazon.com/cloudwatch/") and s.get("resolves", True)
    [ok] = from_cloudwatch(event("OK"))
    assert ok["status"] == "resolved" and ok["fingerprint"] == s["fingerprint"]
    assert from_cloudwatch(event("INSUFFICIENT_DATA")) == []
    other = event()
    other["detail"]["configuration"]["description"] = "Someone else's alarm"
    assert from_cloudwatch(other) == []
    assert from_cloudwatch({**event(), "detail-type": "EC2 Instance State-change Notification"}) == []


def test_the_board_keeps_when_an_alarm_started_and_ignores_late_news():
    board = InMemoryAlarmBoard()
    first = {**from_cloudwatch(event(env="staging", at="2026-11-06T10:00:00Z"))[0], "product_id": "AG"}
    later = {**first, "at": "2026-11-06T11:00:00Z"}
    board.record(first)
    board.record(later)
    assert board.firing("AG")[0]["since"] == "2026-11-06T10:00:00Z"
    board.record({**first, "status": "resolved", "at": "2026-11-06T12:00:00Z"})
    board.record({**first, "at": "2026-11-06T11:30:00Z"})  # arrived late: already resolved
    assert board.firing("AG") == []

    board.record({**first, "at": "2026-11-06T13:00:00Z"})
    state = {"feature_id": "AG-FEAT-9", "product_id": "AG", "resources": [{"name": "other", "type": "sqs-queue"}],
             "deployments": {"vrixel-agtech/agtech-field-api": {"component": "agtech-field-api"}}}
    [shown] = for_feature(board, state)  # not its resource, but a component it changes uses it
    assert shown["env"] == "staging" and shown["alarm"] == "write-throttled"
    assert for_feature(board, {**state, "deployments": {}}) == []
    assert for_feature(board, {**state, "deployments": {}, "feature_id": "AG-FEAT-1"})  # its own resource


@pytest.fixture
def engine(tmp_path):
    root = tmp_path / "governance"
    shutil.copytree(EXAMPLES, root)
    s = load_settings(mode="stub", database_url="", artifact_dir=tmp_path / "artifacts",
                      governance_source="local", governance_root=root)
    return FeatureEngine(build_services(s), InMemorySaver())


def start(engine):
    return engine.start(product_id="AG", title="Field log",
                        description="Keep each field's latest state in DynamoDB")["feature_id"]


def test_g4_shows_the_alarms_and_g6_shows_what_fires_before_production(engine):
    seen = {}
    real = engine.decide

    def capture(fid, iid, decision):
        for p in engine.pending(fid):
            if p.get("interrupt_id") == iid and p.get("gate"):
                seen[p["gate"]] = p
        return real(fid, iid, decision)

    engine.decide = capture
    engine.alarm_changes(from_cloudwatch(event(env="staging")))  # already firing in staging
    fid = start(engine)
    assert fid == "AG-FEAT-1"
    assert engine.auto_drive(fid)["status"] == "done"
    s = engine._state(fid)
    [row] = seen["g4"]["aws_resources"]["resources"]
    assert {a["name"] for a in row["alarms"]} == {"read-throttled", "write-throttled"}
    [res] = [r for r in s["resources"] if r["type"] == "dynamodb-table"]
    assert res["parameters"]["tags"]["vrixel:resource"] == res["name"]
    [firing] = seen["g6"]["alarms_firing"]
    assert firing["env"] == "staging" and firing["severity"] == "sev3" and firing["says"].startswith("Writes")


def test_production_alarms_open_and_resolve_incidents_and_others_never_do(engine):
    out = engine.alarm_changes(from_cloudwatch(event(env="staging")))
    assert out == {"recorded": 1, "incidents": []}
    out = engine.alarm_changes(from_cloudwatch(event()))
    [iid] = out["incidents"]
    inc = engine._state(iid)
    assert inc["severity"] == "sev3" and inc["title"] == "field-state: write throttled"
    assert inc["component"].endswith("agtech-field-api")  # the repo of the component using the table
    assert engine.alarm_changes(from_cloudwatch(event("OK", at="2026-11-06T18:20:00.000+0000")))["incidents"] == [iid]
    assert [x["status"] for x in engine.services.signals.for_incident(iid)] == ["firing", "resolved"]
    assert engine.services.alarms.firing("AG")[0]["env"] == "staging"  # production cleared, staging still firing


def test_the_endpoint_wants_the_signals_token(engine, monkeypatch):
    client = TestClient(create_app(engine, auth=AuthConfig(mode="dev", secret="x" * 40), web_dist=None))
    monkeypatch.setenv("VRIXEL_SIGNALS_TOKEN", "t0k")
    assert client.post("/api/signals/cloudwatch", json=event()).status_code == 401
    r = client.post("/api/signals/cloudwatch", json=event(), headers={"authorization": "Bearer t0k"})
    assert r.status_code == 202 and r.json()["recorded"] == 1 and len(r.json()["incidents"]) == 1

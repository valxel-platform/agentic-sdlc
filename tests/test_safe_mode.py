"""Safe mode (valxel_sdlc.safe_mode): entered automatically when a product's health or the drift
evals trip; every gate to a person, read in full; left after two normal checks in a row and an
owner's confirmation, with the approvals made in the bad window listed. Scoped replays (T-87)."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from conftest import approve
from fastapi.testclient import TestClient
from test_api import SECRET, sign_in
from test_drift import ENV, Notes, Replay, case

from valxel_sdlc import drift, health, safe_mode
from valxel_sdlc.api.app import create_app
from valxel_sdlc.decisions import batch_ready, low_risk
from valxel_sdlc.evals import InMemoryEvalStore
from valxel_sdlc.graph.routing import after_ci
from valxel_sdlc.web.auth import AuthConfig

T0 = datetime(2026, 11, 1, 12, tzinfo=UTC)
CRITIC = {"source": "health", "rule": "critic_false_pass", "scope": "spec",
          "says": "The spec critic passed 50% of documents people then sent back"}


def test_in_on_a_cause_out_after_two_normal_checks_and_an_owner():
    notes = Notes()
    assert safe_mode.enter(notes, ["FARM"], [CRITIC], at="2026-11-10T00:00:00+00:00",
                           bad_since="2026-11-03T00:00:00+00:00") == ["FARM"]
    s = safe_mode.get(notes, "FARM")
    assert s["phases"] == ["spec"] and s["normal_checks"] == 0
    assert safe_mode.enter(notes, ["FARM"], [CRITIC]) == []  # the same cause again changes nothing
    with pytest.raises(ValueError, match="needs 2 normal checks in a row"):
        safe_mode.leave(notes, "FARM", by="maya")

    safe_mode.record_check(notes, "FARM", source="health", normal=True, says="ok")
    safe_mode.record_check(notes, "FARM", source="health", normal=None, says="not enough work yet")  # doesn't count
    safe_mode.record_check(notes, "FARM", source="replay", normal=False, says="spec: pass rate fell")
    assert safe_mode.get(notes, "FARM")["normal_checks"] == 0  # a bad check starts the count again
    safe_mode.mark_fixed(notes, "FARM", by="sam", note="Withdrew lesson L-007")
    safe_mode.record_check(notes, "FARM", source="replay", normal=True, says="back at the baseline")
    safe_mode.record_check(notes, "FARM", source="health", normal=True, says="back to its baseline")
    assert safe_mode.get(notes, "FARM")["ready"]

    left = safe_mode.leave(notes, "FARM", by="maya", approvals=[{"feature_id": "FARM-FEAT-3", "gate": "g2"}])
    assert safe_mode.get(notes, "FARM") is None and left["approvals_to_review"][0]["gate"] == "g2"
    assert notes[safe_mode.history_key("FARM")][0]["fix"]["note"] == "Withdrew lesson L-007"


def test_a_health_check_cant_end_a_model_drift():
    notes = Notes()
    safe_mode.enter(notes, ["FARM"], [{"source": "drift", "rule": "drift", "scope": "", "says": "Drift evals"}])
    assert safe_mode.get(notes, "FARM")["phases"] == []  # no phase named: every phase gets the second critic
    for _ in range(3):
        safe_mode.record_check(notes, "FARM", source="health", normal=True, says="real work looks fine")
    s = safe_mode.get(notes, "FARM")
    assert s["normal_checks"] == 0 and "replay the cases" in s["checks"][-1]["says"]


def test_a_gate_cause_names_its_phase():
    notes = Notes()
    safe_mode.enter(notes, ["FARM"], [{"source": "health", "rule": "first_time_approval", "scope": "g4", "says": "x"}])
    assert safe_mode.get(notes, "FARM")["phases"] == ["architecture"]


def test_approvals_in_the_bad_window():
    rows = [{"feature_id": "F1", "gate": "g2", "decision": "approve", "at": "2026-11-05T00:00:00+00:00"},
            {"feature_id": "F2", "gate": "g2", "decision": "revise", "at": "2026-11-06T00:00:00+00:00"},
            {"feature_id": "F3", "gate": "g1", "decision": "approve", "at": "2026-10-01T00:00:00+00:00"}]
    services = SimpleNamespace(gate_outcomes=SimpleNamespace(rows=lambda pid: rows))
    got = safe_mode.approvals_in_window(services, "FARM", "2026-11-01T00:00:00+00:00", "2026-11-10T00:00:00+00:00")
    assert got == [{"feature_id": "F1", "gate": "g2", "at": "2026-11-05T00:00:00+00:00"}]


def test_every_change_goes_through_g5_and_nothing_is_batched():
    notes = Notes()
    services = SimpleNamespace(cluster=notes)
    state = {"product_id": "FARM", "risk_tier": "low"}
    assert after_ci(state, services) == "merge_train"
    safe_mode.enter(notes, ["FARM"], [CRITIC])
    assert after_ci(state, services) == "g5"
    item = {"type": "gate", "gate": "g2", "profile": "express", "safe_mode": safe_mode.get(notes, "FARM")}
    assert low_risk(item, "general") == (False, "safe mode: every gate is read in full")
    assert batch_ready(item, 0)[0] is False  # express doesn't skip the read either


def test_health_puts_a_product_in_and_checks_the_work_since_the_fix(monkeypatch):
    notes = Notes()
    engine = SimpleNamespace(cluster=notes, services=SimpleNamespace(settings=SimpleNamespace(registry={"FARM": 1, "CLN": 1})))
    trip = [{"key": "critic_false_pass:spec", "rule": "critic_false_pass", "scope": "spec", "says": CRITIC["says"]}]
    calls = {"n": 0}

    def check(current, baseline, rules, **kw):
        calls["n"] += 1
        return trip if current.get("pid") == "FARM" and current.get("bad") else []

    def measure(states, records, *, since, until):
        pid = states[0]["product_id"] if states else ""
        bad = any(s.get("bad") and since <= s["at"] < until for s in states)
        return {"pid": pid, "bad": bad, "critic": {"spec": {"passed": sum(1 for s in states if since <= s["at"] < until)}}}

    monkeypatch.setattr(health, "check", check)
    monkeypatch.setattr(health, "measure", measure)
    rules = health.Rules(7, 28, {"critic_false_pass": {"min_samples": 2}})
    now = T0
    states = [{"product_id": "FARM", "at": (now - timedelta(days=1)).isoformat(), "bad": True}]
    said = []
    out = health.by_product(engine, states, [], rules, now=now, since=now - timedelta(days=7),
                            b_since=now - timedelta(days=35), alert=said.append)
    assert list(out) == ["FARM"] and out["FARM"]["phases"] == ["spec"]
    assert said[0].startswith("Valxel safe mode on for FARM")

    later = now + timedelta(hours=2)
    safe_mode.mark_fixed(notes, "FARM", by="sam", note="Pinned the critic's previous model", at=now.isoformat())
    states.append({"product_id": "FARM", "at": (now + timedelta(hours=1)).isoformat()})
    health.by_product(engine, states, [], rules, now=later, since=later - timedelta(days=7),
                      b_since=later - timedelta(days=35))
    assert safe_mode.get(notes, "FARM")["checks"][-1]["normal"] is None  # one decision since the fix: too few
    states.append({"product_id": "FARM", "at": (now + timedelta(hours=1, minutes=30)).isoformat()})
    health.by_product(engine, states, [], rules, now=later, since=later - timedelta(days=7),
                      b_since=later - timedelta(days=35))
    s = safe_mode.get(notes, "FARM")
    assert s["checks"][-1]["normal"] is True and s["normal_checks"] == 1
    health.by_product(engine, states, [], rules, now=later, since=later - timedelta(days=7),
                      b_since=later - timedelta(days=35))
    assert safe_mode.get(notes, "FARM")["normal_checks"] == 1  # nothing new: the same evidence doesn't count twice

    safe_mode.record_check(notes, "FARM", source="replay", normal=True, says="ok")
    safe_mode.leave(notes, "FARM", by="maya")
    health.by_product(engine, states, [], rules, now=later, since=later - timedelta(days=7),
                      b_since=later - timedelta(days=35))
    assert safe_mode.get(notes, "FARM") is None  # the same bad week doesn't put it back in


@pytest.fixture
def drifting(monkeypatch):
    store = InMemoryEvalStore()
    for i in range(6):
        store.capture(case(i))
        store.capture(case(i, phase="architecture"))
    services = SimpleNamespace(evals=store, settings=SimpleNamespace(roles={}, registry={"FARM": 1, "CLN": 1}))
    monkeypatch.setattr("valxel_sdlc.pilot.config_version", lambda settings: "cfg-A")
    return services, store, Notes()


def test_drift_puts_every_product_in_and_a_scoped_replay_is_a_check(drifting):
    services, _, notes = drifting
    said = []
    drift.run(services, notes, now=T0, replay=Replay(1.0), alert=said.append, env=ENV)
    drift.run(services, notes, now=T0 + timedelta(days=7), replay=Replay(0.34), alert=said.append, env=ENV)
    assert safe_mode.get(notes, "FARM") and safe_mode.get(notes, "CLN")
    assert any(m.startswith("Valxel safe mode on for FARM, CLN") for m in said)

    replay = Replay(1.0)
    r = drift.replay_scoped(services, notes, "FARM", ["spec"], now=T0 + timedelta(days=8), replay=replay, env=ENV)
    assert r["normal"] is True and r["cases"] >= 1
    assert all(":spec:" in i for call in replay.calls for i in call)  # only the phase asked for
    assert safe_mode.get(notes, "FARM")["normal_checks"] == 1 and safe_mode.get(notes, "CLN")["normal_checks"] == 0
    bad = drift.replay_scoped(services, notes, "FARM", ["spec"], now=T0 + timedelta(days=8), replay=Replay(0.0), env=ENV)
    assert bad["normal"] is False and safe_mode.get(notes, "FARM")["normal_checks"] == 0
    assert notes[drift.REPLAYS][0]["normal"] is False

    drift.run(services, notes, now=T0 + timedelta(days=14), replay=Replay(1.0), env=ENV)  # the weekly run: back
    assert safe_mode.get(notes, "CLN")["normal_checks"] == 1


def test_drift_names_the_phases_that_moved():
    assert drift.phases_in(["Same cases, same config: spec: pass rate fell from 100% to 34%",
                            "This week's work: all: pass rate fell", "Same cases, same config: build: x"]) == ["spec", "build"]


def test_the_pipeline_in_safe_mode(engine):
    safe_mode.enter(engine.cluster, ["FARM"], [{"source": "manual", "rule": "manual", "scope": "", "says": "sam: checking"}])
    fid = engine.start(product_id="FARM", title="Rename the harvest button", profile="express")["feature_id"]
    view = engine.view(fid)
    assert view["profile"] == "feature" and "Safe mode" in view["profile_note"]
    assert view["safe_mode"]["says"].startswith("Safe mode since")
    [g1] = [p for p in engine.pending(fid) if p["type"] == "gate"]
    assert g1["gate"] == "g1" and g1["safe_mode"]["causes"][0]["says"] == "sam: checking"
    assert g1["second_critic"] and g1["second_critic"]["model"]  # the shadow critic read the brief too
    approve(engine, fid, "g1")
    [g2] = [p for p in engine.pending(fid) if p["type"] == "gate"]
    if g2.get("confidence"):
        assert g2["confidence"]["quick_read"] is False


@pytest.fixture
def client(engine, monkeypatch):
    monkeypatch.setenv("VALXEL_EVAL_APPROVERS", "maya")
    with TestClient(create_app(engine, auth=AuthConfig(mode="dev", secret=SECRET), web_dist=None)) as c:
        yield c


def test_support_and_owners_through_the_api(client, engine):
    sign_in(client, "someone-else")
    assert client.get("/api/safe-mode").status_code == 403
    sign_in(client, "maya")
    assert client.post("/api/safe-mode/FARM/enter", json={"note": ""}).status_code == 422
    r = client.post("/api/safe-mode/FARM/enter", json={"note": "Spec critic looks off", "phases": ["spec"]})
    assert r.status_code == 200 and r.json()["phases"] == ["spec"]
    listed = client.get("/api/safe-mode").json()
    assert listed["products"]["FARM"]["needed"] == 2 and listed["products"]["FARM"]["hours"] >= 0
    assert client.post("/api/safe-mode/FARM/fixed", json={"note": "Pinned the previous model"}).json()["fix"]["by"] == "maya"
    assert client.post("/api/safe-mode/FARM/leave", json={}).status_code == 409  # no normal checks yet
    assert client.post("/api/safe-mode/FARM/replay", json={}).status_code == 202
    for _ in range(2):
        safe_mode.record_check(engine.cluster, "FARM", source="replay", normal=True, says="ok")
    left = client.post("/api/safe-mode/FARM/leave", json={})
    assert left.status_code == 200 and left.json()["left_by"] == "maya"
    assert client.get("/api/safe-mode").json()["products"] == {}
    assert client.post("/api/safe-mode/NOPE/enter", json={"note": "x"}).status_code == 404


def test_an_approval_in_safe_mode_needs_the_decision_read(client, engine):
    from test_api import wait_idle

    sign_in(client, "priya", teams=["acme-farm/product-owners"], orgs=["acme-farm"])
    fid = client.post("/api/features", json={"product_id": "FARM", "title": "Field notes", "description": "x"}).json()["feature_id"]
    wait_idle(engine, fid)
    safe_mode.enter(engine.cluster, ["FARM"], [CRITIC])
    [i] = [i for i in client.get("/api/inbox?mine=true").json() if i["feature_id"] == fid]
    assert i["safe_mode"]["says"].startswith("Safe mode since") and i["batch_ready"] is False
    url = f"/api/features/{fid}/decisions/{i['interrupt_id']}"
    r = client.post(url, json={"decision": "approve"})
    assert r.status_code == 409 and "read it in full" in r.json()["detail"]
    engine.cluster.put(f"read:{i['interrupt_id']}:priya", {"seconds": 25})
    assert client.post(url, json={"decision": "approve"}).status_code == 202

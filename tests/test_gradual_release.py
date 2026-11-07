"""Gradual releases: G6 approves a plan, the flag goes on for 5% of users, and the tool steps up
after each quiet bake time. An incident holds it; the flag going off stops it and the release
managers decide. Each step is a change record; Flipt gets a threshold rollout."""

import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from vrixel_sdlc.config import load_settings
from vrixel_sdlc.engine import FeatureEngine
from vrixel_sdlc.integrations.flags import FliptFlags
from vrixel_sdlc.ops import rollout
from vrixel_sdlc.ops.signals import from_alertmanager
from vrixel_sdlc.services import build_services

EXAMPLES = Path(__file__).resolve().parents[1] / "examples" / "governance"


@pytest.fixture
def real_time(monkeypatch):
    """Bake times count, on a clock the test moves."""
    monkeypatch.setattr(rollout, "COMPRESS_IN_STUB", False)
    clock = {"t": datetime.now(timezone.utc)}
    monkeypatch.setattr(rollout, "_clock", lambda: clock["t"])
    return clock


@pytest.fixture
def eng(tmp_path):
    root = tmp_path / "governance"
    shutil.copytree(EXAMPLES, root)
    s = load_settings(mode="stub", database_url="", artifact_dir=tmp_path / "a", governance_source="local",
                      governance_root=root)
    return FeatureEngine(build_services(s), InMemorySaver())


def waiting(eng, fid):
    return [p for p in eng.pending(fid) if p.get("wait") == "flag_step"]


def test_the_plan_g6_approves_steps_up_while_quiet(eng, real_time):
    fid = eng.create(product_id="EMR", title="Consent capture at intake")
    eng.auto_drive(fid)
    v = eng.view(fid)
    key, plan = v["release"]["flag"], v["release"]["rollout_plan"]
    assert plan["steps"] == [5.0, 25.0, 50.0, 100.0] and plan["bake_minutes"] == {"low": 30, "high": 60, "regulated": 120}[plan["risk_tier"]]
    flags = eng.services.flags
    assert flags.percent("EMR", key) == 5.0 and v["status"] != "done"
    [w] = waiting(eng, fid)
    assert w["percent"] == 5.0 and w["label"].startswith("Gradual release: on for 5%")
    eng.poke_now(fid)
    assert flags.percent("EMR", key) == 5.0  # not yet: the bake time hasn't passed
    real_time["t"] += timedelta(minutes=plan["bake_minutes"] + 1)
    eng.poke_now(fid)
    assert flags.percent("EMR", key) == 25.0
    for _ in range(2):
        real_time["t"] += timedelta(minutes=plan["bake_minutes"] + 1)
        eng.poke_now(fid)
    assert flags.percent("EMR", key) == 100.0 and eng.view(fid)["status"] == "done"
    steps = [r for r in eng.services.changes.list("EMR") if r["action"] == "rollout_step"]
    assert [r["targets"][0]["percent"] for r in steps] == [25.0, 50.0, 100.0]


def test_an_incident_holds_it_and_a_flag_off_stops_it(eng, real_time):
    fid = eng.create(product_id="EMR", title="Consent capture at intake")
    eng.auto_drive(fid)
    key, plan = eng.view(fid)["release"]["flag"], eng.view(fid)["release"]["rollout_plan"]
    gov = eng.services.snapshot("EMR")
    assert gov.remediation["gradual_release"]
    # An alert on its component opens an incident; the tool turns the flag off (it agrees with
    # OpenChoreo's analysis that the new release is the cause).
    [iid] = eng.ingest(from_alertmanager({"alerts": [{"status": "firing", "fingerprint": "f",
                                                      "startsAt": datetime.now(timezone.utc).isoformat(),
                                                      "labels": {"alertname": "Errors", "severity": "error",
                                                                 "component": "emr-intake-api"}}]}))
    assert eng.services.flags.percent("EMR", key) == 0.0
    real_time["t"] += timedelta(minutes=plan["bake_minutes"] + 1)
    eng.poke_now(fid)
    esc = next(p for p in eng.pending(fid) if p["type"] == "escalation")
    assert esc["wait"] == "flag_rollout" and "stopped at 5%" in esc["reason"] and "turned off" in esc["reason"]
    # The release managers start it again from the first step, once the fix is in.
    eng.decide(fid, esc["interrupt_id"], {"decision": "retry", "approver": "rm-1"})
    assert eng.services.flags.percent("EMR", key) == 5.0
    restart = [r for r in eng.services.changes.list("EMR") if r["action"] == "rollout_step"][-1]
    assert restart["approved_by"] == ["rm-1"] and restart["targets"][0]["percent"] == 5.0
    # While an incident on its components is open, it holds.
    assert waiting(eng, fid)


def test_the_check():
    since = datetime.now(timezone.utc) - timedelta(minutes=61)
    ok = rollout.check(on=True, percent=5, since=since.isoformat(), bake_minutes=60, incidents=[])
    assert ok["status"] == "success"
    assert rollout.check(on=True, percent=5, since=since.isoformat(), bake_minutes=60,
                         incidents=[{"status": "monitoring"}]) is None
    just = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
    assert rollout.check(on=True, percent=5, since=since.isoformat(), bake_minutes=60,
                         incidents=[{"status": "closed", "events": [{"kind": "recovered", "at": just}]}]) is None
    assert rollout.check(on=False, percent=0, since=since.isoformat(), bake_minutes=60, incidents=[])["status"] == "failure"
    assert rollout.plan_for({"gradual_release": None}, "high") is None
    p = rollout.plan_for({"gradual_release": {"steps": [10.0, 100.0], "bake_minutes": {"low": 5, "high": 10, "regulated": 20}}},
                         "regulated")
    assert p["bake_minutes"] == 20 and p["steps"] == [10.0, 100.0]


def test_a_product_can_release_at_once_or_choose_its_steps(tmp_path):
    root = tmp_path / "g"
    shutil.copytree(EXAMPLES, root)
    gov = root / "vrixel-emr" / "emr-governance" / "product.yaml"
    gov.write_text(gov.read_text().replace("runtime:\n", "runtime:\n  remediation: {gradual_release: false}\n", 1))
    ag = root / "vrixel-agtech" / "ag-governance" / "product.yaml"
    ag.write_text(ag.read_text().replace("runtime:\n", "runtime:\n  remediation: {gradual_release: {steps: [10, 100]}}\n", 1))
    s = load_settings(mode="stub", database_url="", artifact_dir=tmp_path / "a", governance_source="local", governance_root=root)
    eng = FeatureEngine(build_services(s), InMemorySaver())
    fid = eng.create(product_id="EMR", title="Consent capture at intake")
    v = eng.auto_drive(fid)
    assert "rollout_plan" not in v["release"] and eng.services.flags.percent("EMR", v["release"]["flag"]) == 100.0
    assert eng.services.snapshot("AG").remediation["gradual_release"]["steps"] == [10.0, 100.0]


class FakeFlipt:
    def __init__(self):
        self.flag = {"key": "f", "type": "BOOLEAN_FLAG_TYPE", "enabled": False, "rollouts": []}
        self.puts = []

    def __call__(self, method, path, body):
        if method == "GET":
            return 200, {"resource": {"payload": dict(self.flag)}, "revision": "r1"}
        self.puts.append(body["payload"])
        self.flag = {k: v for k, v in body["payload"].items() if k != "@type"}
        return 200, {}


def test_flipt_gets_a_threshold_rollout_and_off_means_off_for_everyone():
    fake = FakeFlipt()
    f = FliptFlags(fake, namespace="emr", environments={"production": "production"})
    f.set_percent("f", 5, actor="rm")
    assert fake.flag["enabled"] is False and fake.flag["rollouts"] == [
        {"type": "THRESHOLD_ROLLOUT_TYPE", "description": "vrixel gradual release",
         "threshold": {"percentage": 5.0, "value": True}}]
    assert f.percent("f") == 5.0 and f.get("f") is True
    f.set("f", False, actor="agent:remediate")  # an incident's flag off
    assert fake.flag["enabled"] is False and fake.flag["rollouts"] == [] and f.get("f") is False
    f.set_percent("f", 100, actor="agent:rollout")
    assert fake.flag["enabled"] is True and fake.flag["rollouts"] == [] and f.percent("f") == 100.0
    n = len(fake.puts)
    f.set("f", True, actor="x")
    assert len(fake.puts) == n  # nothing to change

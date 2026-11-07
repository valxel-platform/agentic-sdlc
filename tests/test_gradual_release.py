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


def launch(eng):
    a = eng.create(product_id="AG", title="Offline crop log")
    b = eng.create(product_id="AG", title="Soil sensor import")
    rid = eng.create_release(product_id="AG", title="Spring season", model="launch", features=[a, b])
    eng.auto_drive(a)
    eng.auto_drive(b)
    g = next(p for p in eng.pending(rid) if p.get("gate") == "release")
    eng.decide(rid, g["interrupt_id"], {"decision": "approve", "approver": "rm"})
    keys = [eng.view(f)["release"]["flag"] for f in (a, b)]
    return rid, a, b, keys


def test_a_launch_widens_every_flag_together(eng, real_time):
    rid, a, b, keys = launch(eng)
    flags = eng.services.flags
    assert [flags.percent("AG", k) for k in keys] == [5.0, 5.0] and eng.view(rid)["status"] == "approved"
    [w] = [p for p in eng.pending(rid) if p.get("wait") == "flag_step"]
    assert w["flags"] == sorted(keys) and w["label"].startswith("Gradual launch: on for 5%")
    bake = eng._state(rid)["rollout_plan"]["bake_minutes"]
    for _ in range(3):
        real_time["t"] += timedelta(minutes=bake + 1)
        eng.poke_now(rid)
    assert [flags.percent("AG", k) for k in keys] == [100.0, 100.0] and eng.view(rid)["status"] == "shipped"
    steps = [r for r in eng.services.changes.list("AG") if r["action"] == "rollout_step" and r["release_id"] == rid]
    assert [r["targets"][0]["percent"] for r in steps] == [25.0, 50.0, 100.0] and len(steps[0]["targets"]) == 2
    launch_rec = next(r for r in eng.services.changes.list("AG") if r["action"] == "flag_on")
    assert all(t["percent"] == 5.0 for t in launch_rec["targets"])


def test_a_flag_turned_off_stops_the_launch_for_the_release_managers(eng, real_time):
    rid, a, b, keys = launch(eng)
    eng.services.flags.set("AG", keys[1], False, actor="oncall-1")  # an incident turned one off
    real_time["t"] += timedelta(minutes=200)
    eng.poke_now(rid)
    esc = next(p for p in eng.pending(rid) if p["type"] == "escalation")
    assert esc["wait"] == "launch_rollout" and keys[1] in esc["reason"]
    eng.decide(rid, esc["interrupt_id"], {"decision": "abort", "approver": "rm"})
    assert eng.view(rid)["status"] == "cancelled"
    assert [eng.services.flags.percent("AG", k) for k in keys] == [0.0, 0.0]  # cancelled: off for everyone
    off = next(r for r in eng.services.changes.list("AG") if r["action"] == "flag_off" and r["release_id"] == rid)
    assert off["approved_by"] == ["rm"] and len(off["targets"]) == 2


# --------------------------------------------------------------------------- comparing on against off

from vrixel_sdlc.ops import cohorts  # noqa: E402
from vrixel_sdlc.ops import paging  # noqa: E402


def test_the_summary_lines_add_up_and_the_test_is_conservative():
    f = "emr.f"
    lines = [cohorts.line(f, (100, 1), (900, 4)), cohorts.line(f, (120, 0), (880, 5)), cohorts.line("other", (5, 5), (5, 0)),
             '{"message": "vrixel.flag_cohorts {\\"flag\\": \\"emr.f\\", \\"on\\": {\\"requests\\": 10, \\"errors\\": 0}, '
             '\\"off\\": {\\"requests\\": 10, \\"errors\\": 0}}"}', "unrelated", "vrixel.flag_cohorts {not json"]
    got = cohorts.parse(lines, [f])[f]
    assert got == {"on": {"requests": 230, "errors": 1}, "off": {"requests": 1790, "errors": 9}, "lines": 3}
    assert cohorts.compare(got)["verdict"] == "ok"
    bad = {"on": {"requests": 400, "errors": 24}, "off": {"requests": 4000, "errors": 20}}
    r = cohorts.compare(bad)
    assert r["verdict"] == "regression" and r["on_rate"] == 0.06 and r["off_rate"] == 0.005 and r["z"] > 3
    assert "6.00% of requests failed with it on (24/400)" in cohorts.describe(f, r)
    few = {"on": {"requests": 50, "errors": 10}, "off": {"requests": 4000, "errors": 20}}
    assert cohorts.compare(few)["verdict"] == "too_few"  # 20% is alarming, but 50 requests isn't enough to say
    tiny = {"on": {"requests": 4000, "errors": 21}, "off": {"requests": 40000, "errors": 160}}
    assert cohorts.compare(tiny)["verdict"] == "ok"  # 0.5% against 0.4%: not worth stopping a release for
    assert cohorts.compare({"on": {"requests": 300, "errors": 6}, "off": {"requests": 3000, "errors": 0}})["verdict"] == "regression"


@pytest.fixture
def watched(eng, real_time):
    eng.services.cohort_logs = cohorts.StubLogs()
    eng.services.pager = paging.RecordingPager({"EMR": "k", "AG": "k"}, "https://sdlc")
    fid = eng.create(product_id="EMR", title="Consent capture at intake")
    eng.auto_drive(fid)
    return eng, fid, eng.view(fid)["release"]["flag"]


def test_users_with_it_on_failing_more_stops_it_and_the_tool_turns_it_off(watched):
    eng, fid, key = watched
    eng.services.cohort_logs.by_component["emr-intake-api"] = [cohorts.line(key, (400, 24), (4000, 20))]
    eng.poke_now(fid)  # within the bake time: the comparison doesn't wait for it
    assert eng.services.flags.percent("EMR", key) == 0.0
    esc = next(p for p in eng.pending(fid) if p["type"] == "escalation")
    assert "users with it on fail more" in esc["reason"] and "The tool turned the flag off" in esc["reason"]
    off = next(r for r in eng.services.changes.list("EMR") if r["action"] == "flag_off")
    assert off["type"] == "automatic" and off["evidence"]["cohorts"]["verdict"] == "regression"
    assert eng.services.pager.sent == []  # nothing left on: no page
    eng.decide(fid, esc["interrupt_id"], {"decision": "resolved", "approver": "rm"})
    v = eng.view(fid)
    assert v["release"]["flag_on"] is False and v["release"]["rollout_plan"]["stopped"]


def test_without_flag_off_autonomy_it_pages_instead(tmp_path, real_time):
    root = tmp_path / "g"
    shutil.copytree(EXAMPLES, root)
    gov = root / "vrixel-emr" / "emr-governance" / "product.yaml"
    gov.write_text(gov.read_text().replace("runtime:\n", "runtime:\n  remediation: {autonomous: [rollback]}\n", 1))
    s = load_settings(mode="stub", database_url="", artifact_dir=tmp_path / "a", governance_source="local", governance_root=root)
    eng = FeatureEngine(build_services(s), InMemorySaver())
    eng.services.cohort_logs = cohorts.StubLogs()
    eng.services.pager = paging.RecordingPager({"EMR": "k"}, "https://sdlc")
    fid = eng.create(product_id="EMR", title="Consent capture at intake")
    eng.auto_drive(fid)
    key = eng.view(fid)["release"]["flag"]
    eng.services.cohort_logs.by_component["emr-intake-api"] = [cohorts.line(key, (400, 24), (4000, 20))]
    eng.poke_now(fid)
    assert eng.services.flags.percent("EMR", key) == 5.0  # left on: a person decides
    [page] = [x for x in eng.services.pager.sent if x["dedup_key"] == f"{fid}:urgent"]
    assert "its users fail more" in page["payload"]["summary"]


def test_a_healthy_comparison_is_kept_with_each_step_and_no_lines_means_time_alone(watched, real_time):
    eng, fid, key = watched
    eng.services.cohort_logs.by_component["emr-intake-api"] = [cohorts.line(key, (400, 1), (4000, 12))]
    bake = eng.view(fid)["release"]["rollout_plan"]["bake_minutes"]
    real_time["t"] += timedelta(minutes=bake + 1)
    eng.poke_now(fid)
    [h] = eng.view(fid)["release"]["rollout_plan"]["history"]
    assert h["percent"] == 5.0 and "0.25% of requests failed with it on" in h["cohorts"]
    eng.services.cohort_logs.by_component.clear()
    real_time["t"] += timedelta(minutes=bake + 1)
    eng.poke_now(fid)
    assert eng.view(fid)["release"]["rollout_plan"]["history"][-1]["cohorts"].startswith("no comparison")


def test_a_launch_turns_off_only_the_flag_whose_users_fail_more(eng, real_time):
    eng.services.cohort_logs = cohorts.StubLogs()
    rid, a, b, keys = launch(eng)
    comps = eng.feature_info(a)["components"] + eng.feature_info(b)["components"]
    eng.services.cohort_logs.by_component[comps[0].rsplit("/", 1)[-1]] = [
        cohorts.line(keys[0], (400, 1), (4000, 10)), cohorts.line(keys[1], (400, 30), (4000, 10))]
    eng.poke_now(rid)
    assert eng.services.flags.percent("AG", keys[0]) == 5.0 and eng.services.flags.percent("AG", keys[1]) == 0.0
    esc = next(p for p in eng.pending(rid) if p["type"] == "escalation")
    assert keys[1] in esc["reason"] and keys[0] not in esc["reason"].split("users with it on fail more:")[1]


def test_each_component_and_app_is_compared_on_its_own_across_the_whole_project(watched):
    """The web app (relayed by the BFF as source web) regresses while the busy API is fine: pooled
    together the API's volume would hide it. The web app isn't one of the feature's components;
    the project-wide query finds it anyway."""
    eng, fid, key = watched
    lines = [cohorts.line(key, (4000, 20), (40000, 200))]
    eng.services.cohort_logs.by_component["emr-intake-api"] = lines
    eng.services.cohort_logs.by_component["emr-bff"] = [cohorts.line(key, (300, 15), (3000, 3), source="web")]
    pooled = cohorts.parse([*lines, eng.services.cohort_logs.by_component["emr-bff"][0]], [key])[key]
    assert cohorts.compare(pooled)["verdict"] == "ok"  # what a single pooled comparison would have said
    eng.poke_now(fid)
    assert eng.services.flags.percent("EMR", key) == 0.0
    esc = next(p for p in eng.pending(fid) if p["type"] == "escalation")
    assert f"{key} in web: 5.00% of requests failed with it on" in esc["reason"]
    off = next(r for r in eng.services.changes.list("EMR") if r["action"] == "flag_off")
    assert off["evidence"]["cohorts"]["group"] == "web"


def test_a_step_keeps_every_components_comparison(watched, real_time):
    eng, fid, key = watched
    eng.services.cohort_logs.by_component["emr-intake-api"] = [cohorts.line(key, (400, 1), (4000, 12))]
    eng.services.cohort_logs.by_component["emr-bff"] = [cohorts.line(key, (250, 0), (2500, 2), source="mobile")]
    real_time["t"] += timedelta(minutes=eng.view(fid)["release"]["rollout_plan"]["bake_minutes"] + 1)
    eng.poke_now(fid)
    [h] = eng.view(fid)["release"]["rollout_plan"]["history"]
    assert f"{key} in emr-intake-api: 0.25%" in h["cohorts"] and f"{key} in mobile: 0.00%" in h["cohorts"]


@pytest.fixture
def ledger(eng, real_time):
    """EMR builds on APX; EMR's web app calls APX's ledger, which the feature doesn't change."""
    fid = eng.create(product_id="EMR", title="Consent capture at intake")
    eng.auto_drive(fid)
    assert "vrixel-apx/apx-ledger" not in eng.feature_info(fid)["components"]
    return eng, fid, eng.view(fid)["release"]["flag"]


def test_a_feature_still_rolling_out_is_a_suspect_for_the_components_around_it(ledger):
    eng, fid, key = ledger
    assert eng.services.flags.percent("EMR", key) == 5.0
    [s] = [x for x in eng.suspects("APX", "vrixel-apx/apx-ledger") if x["feature_id"] == fid]
    assert s["flag"] == key and s["flag_on"] and "rolling out to 5%" in s["via"] and s["product_id"] == "EMR"
    assert "via" not in [x for x in eng.suspects("EMR", "vrixel-emr/emr-intake-api") if x["feature_id"] == fid][0]
    assert not [x for x in eng.suspects("EMR", "vrixel-emr/emr-unrelated") if x["feature_id"] == fid]
    # Once it's on for everyone it's an ordinary release: not a suspect for components it didn't change.
    st = eng._state(fid)
    st["release"]["flag_percent"] = 100.0
    real = eng._state
    eng._state = lambda f: st if f == fid else real(f)
    assert not [x for x in eng.suspects("APX", "vrixel-apx/apx-ledger") if x["feature_id"] == fid]


def test_an_incident_in_a_component_it_calls_holds_the_rollout_and_can_turn_its_flag_off(ledger):
    eng, fid, key = ledger
    comps = eng.feature_info(fid)["components"]
    assert eng._touching("EMR", comps) == []
    [iid] = eng.ingest(from_alertmanager({"alerts": [{"status": "firing", "fingerprint": "l",
                                                      "startsAt": datetime.now(timezone.utc).isoformat(),
                                                      "labels": {"alertname": "Errors", "severity": "error",
                                                                 "component": "apx-ledger"}}]}))
    assert iid.startswith("APX-")
    [t] = eng._touching("EMR", comps)  # it holds the rollout too
    assert t["incident_id"] == iid and t["touch"] == "calls or is called by it"
    # The ledger's incident is APX's, but EMR's rolling-out feature is a suspect and its flag goes off.
    assert eng.services.flags.percent("EMR", key) == 0.0

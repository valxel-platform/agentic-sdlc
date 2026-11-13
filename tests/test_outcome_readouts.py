"""Outcomes, step 3: readouts after release, goal trends and initiative status, and numbers typed in
when a hand-entered goal's readout is due."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from test_api import CLN_TEAMS, SECRET, sign_in
from valxel_sdlc.api.app import create_app
from valxel_sdlc.outcomes import Metric, Reading
from valxel_sdlc.outcomes import read as rd
from valxel_sdlc.outcomes import readouts as ro
from valxel_sdlc.web.auth import AuthConfig


def released_feature(engine):
    fid = engine.create(product_id="CLN", title="Kiosk check-in")
    for _ in range(25):
        p = [x for x in engine.pending(fid) if x["type"] == "gate"]
        if not p:
            break
        engine.decide(fid, p[0]["interrupt_id"], {"decision": "approve", "approver": "lead"})
    state = engine._state(fid)
    assert state.get("status") == "done"
    return fid, ro.released_at(state)


class Timed:
    """A metric source whose value depends on the window: before release 6.0 minutes, after 4.5."""
    provider = "sql"

    def __init__(self, released):
        self.released = released
        self.calls = 0

    def check(self, metric, data_class):
        return []

    def read(self, cfg, metric, start, end, *, product_id):
        self.calls += 1
        before = datetime.fromisoformat(end) <= self.released
        return Reading(product_id, metric.goal_id, "sql", start, end, "ok", value=6.0 if before else 4.5, n=200)


def test_readouts_land_after_each_window(engine, services):
    fid, released = released_feature(engine)
    rd.sources(services)
    services.metric_sources["sql"] = src = Timed(released)
    assert engine.collect_outcomes("CLN") == []  # nothing due on release day
    taken = ro.run(engine, "CLN", now=released + timedelta(days=31))
    assert sorted(r["window_days"] for r in taken) == [7, 30]
    r7 = next(r for r in taken if r["window_days"] == 7)
    assert (r7["before"]["value"], r7["after"]["value"], r7["change"], r7["toward"]) == (6.0, 4.5, -1.5, True)
    assert r7["feature_id"] == fid and r7["goal_id"] == "GOAL-CLN-1" and "Before and after" in r7["caveat"]
    calls = src.calls
    assert ro.run(engine, "CLN", now=released + timedelta(days=32)) == []  # taken readouts aren't retaken
    assert [r["window_days"] for r in ro.run(engine, "CLN", now=released + timedelta(days=91))] == [90]
    assert src.calls > calls
    trend = [h for h in services.readings.history("CLN", "GOAL-CLN-1") if h["extra"].get("kind") == "trend"]
    assert len(trend) == 3  # a reading a week at most: days 0, 31 and 91, not 32


def metric(**kw):
    base = dict(goal_id="G", source="sql", direction="down", baseline=6.5, target=3.0)
    return Metric(**{**base, **kw})


@pytest.mark.parametrize("value, by, days_in, expect", [
    (5.5, "", 10, "on_track"),          # 29% of the gap: well past 5%
    (6.4, "", 10, "flat"),              # 3%
    (7.0, "", 10, "off_track"),         # moved away
    (5.9, "2027-01-01", 300, "flat"),   # 17% of the gap with most of the time gone: under a quarter of it
    (5.9, "2027-01-01", 30, "on_track"),
    (None, "", 10, "no_data"),
])
def test_initiative_status(value, by, days_in, expect):
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    m = metric(by=by)
    assert ro.status(m, value, start, start + timedelta(days=days_in))["status"] == expect


def test_status_needs_a_baseline_and_works_upward():
    assert ro.status(metric(baseline=None), 5.0, None)["why"].startswith("no baseline")
    up = metric(direction="up", baseline=100.0, target=200.0)
    assert ro.status(up, 150.0, None)["status"] == "on_track" and ro.progress(up, 150.0) == 0.5
    assert ro.progress(metric(target=None, direction="up", baseline=10.0), 12.0) == pytest.approx(0.2)


def test_a_hand_entered_goal_asks_for_its_number_then_reads_it(services):
    released = datetime(2026, 9, 1, tzinfo=timezone.utc)
    ms = ro.metrics(services, "CLN")
    rd.sources(services)
    r = ro.take(services, "CLN", "CLN-FEAT-1", ms["GOAL-CLN-2"], 30, released)
    assert r["before"]["value"] == 5.0 and "baseline" in r["before"]["note"]  # no reading before: the baseline
    assert r["after"]["status"] == "no_data"
    services.readings.put_readout(r)
    engine = SimpleNamespace(services=services)
    out = ro.product_outcomes(engine, "CLN")
    assert out["wanted"] == [{"goal_id": "GOAL-CLN-2", "feature_id": "CLN-FEAT-1", "window_days": 30,
                              "start": r["after"]["start"], "end": r["after"]["end"], "unit": "% of arrivals"}]
    with pytest.raises(ValueError, match="rate"):  # PHI: the count on top too
        rd.record_manual(services, "CLN", "GOAL-CLN-2", value=3.2, n=900, start=r["after"]["start"],
                         end=r["after"]["end"], entered_by="sunil")
    rd.record_manual(services, "CLN", "GOAL-CLN-2", value=3.2, n=900, k=29, start=r["after"]["start"],
                     end=r["after"]["end"], entered_by="sunil")
    [new] = ro.retake_waiting(engine, "CLN", "GOAL-CLN-2")
    assert (new["after"]["value"], new["change"], new["toward"]) == (3.2, -1.8, True)
    out = ro.product_outcomes(engine, "CLN")
    assert out["wanted"] == [] and out["goals"]["GOAL-CLN-2"]["latest"] == 3.2
    assert out["goals"]["GOAL-CLN-2"]["status"] == "on_track"  # 5 -> 3.2 of 5 -> 2 is 60% of the gap


@pytest.fixture
def client(engine):
    app = create_app(engine, auth=AuthConfig(mode="dev", secret=SECRET), web_dist=None)
    with TestClient(app) as c:
        yield c


def test_the_roadmap_shows_outcomes_and_owners_enter_numbers(client, engine):
    services = engine.services
    released = datetime(2026, 9, 1, tzinfo=timezone.utc)
    rd.sources(services)
    r = ro.take(services, "CLN", "CLN-FEAT-1", ro.metrics(services, "CLN")["GOAL-CLN-2"], 7, released)
    services.readings.put_readout(r)
    sign_in(client, "maria", teams=CLN_TEAMS)
    road = client.get("/api/roadmap/CLN").json()
    g1 = next(g for g in road["goals"] if g["id"] == "GOAL-CLN-1")
    assert g1["outcome"]["metric"]["source"] == "sql" and road["outcomes"]["windows"] == [7, 30, 90]
    assert road["outcomes"]["wanted"][0]["goal_id"] == "GOAL-CLN-2"
    body = {"value": 4.1, "n": 700, "k": 29, "start": r["after"]["start"], "end": r["after"]["end"]}
    sign_in(client, "dev1", teams=["acme-clinic/developers"], orgs=["acme-clinic"])
    assert client.post("/api/products/CLN/goals/GOAL-CLN-2/readings", json=body).status_code == 403
    sign_in(client, "maria", teams=CLN_TEAMS)
    assert client.post("/api/products/CLN/goals/GOAL-CLN-1/readings", json=body).status_code == 422  # read from sql
    got = client.post("/api/products/CLN/goals/GOAL-CLN-2/readings", json=body)
    assert got.status_code == 201 and got.json()["readouts"][0]["after"]["value"] == 4.1
    assert client.get("/api/roadmap/CLN").json()["outcomes"]["wanted"] == []


# ------------------------------------------------------------------ step 4: flag comparisons

def test_goal_events_are_compared_on_against_off():
    from valxel_sdlc.ops import cohorts

    def side(req, n):
        return {"requests": req, "errors": 0, "slow": 0, "events": {"checkin_completed": n}}

    groups = {"api": {"on": side(2000, 300), "off": side(20000, 2000), "lines": 3, "timed": 0},
              "bff": {"on": side(500, 70), "off": side(5000, 500), "lines": 3, "timed": 0}}
    r = cohorts.goal_events(groups, ["checkin_completed", "never_sent"])
    c = r["checkin_completed"]
    assert c["verdict"] == "higher" and c["on_rate"] == 0.148 and c["off_rate"] == 0.1 and c["z"] > 2
    assert r["never_sent"]["verdict"] == "too_few"
    assert "more often" in cohorts.describe_goal({"checkin_completed": c})
    even = {"api": {"on": side(2000, 200), "off": side(20000, 2010), "lines": 1, "timed": 0}}
    assert cohorts.goal_events(even, ["checkin_completed"])["checkin_completed"]["verdict"] == "no_difference"


def test_each_rollout_step_reports_the_goal_events_and_never_stops_for_them(tmp_path, monkeypatch):
    import json
    import shutil
    from pathlib import Path

    from langgraph.checkpoint.memory import InMemorySaver

    from valxel_sdlc.config import load_settings
    from valxel_sdlc.engine import FeatureEngine
    from valxel_sdlc.ops import cohorts, rollout
    from valxel_sdlc.services import build_services

    monkeypatch.setattr(rollout, "COMPRESS_IN_STUB", False)
    clock = {"t": datetime.now(timezone.utc)}
    monkeypatch.setattr(rollout, "_clock", lambda: clock["t"])
    root = tmp_path / "g"
    shutil.copytree(Path(__file__).resolve().parents[1] / "examples" / "governance", root)
    s = load_settings(mode="stub", database_url="", artifact_dir=tmp_path / "a", governance_source="local",
                      governance_root=root)
    eng = FeatureEngine(build_services(s), InMemorySaver())
    eng.services.cohort_logs = cohorts.StubLogs()
    fid = eng.create(product_id="CLN", title="Kiosk check-in")
    eng.auto_drive(fid)
    key = eng.view(fid)["release"]["flag"]
    event = "kiosk_check_in_completed"
    rec = {"flag": key, "on": {"requests": 2000, "errors": 0, "events": {event: 120}},
           "off": {"requests": 20000, "errors": 3, "events": {event: 2000}}}
    eng.services.cohort_logs.by_component["cln-intake-api"] = [f"{cohorts.MARKER} {json.dumps(rec)}"]
    clock["t"] += timedelta(minutes=eng.view(fid)["release"]["rollout_plan"]["bake_minutes"] + 1)
    eng.poke_now(fid)
    [h] = eng.view(fid)["release"]["rollout_plan"]["history"]
    assert h["goal_cohorts"][event]["verdict"] == "lower" and "less often" in h["goal"]
    assert eng.services.flags.percent("CLN", key) > 5.0  # fewer goal events is reported, not a reason to stop
    out = ro.feature_outcome(eng, fid)
    assert out["rollout"][0]["percent"] == 5.0 and event in out["rollout"][0]["goal"]

"""Weekly drift evals (valxel_sdlc.drift): the same pinned cases under the same config, week over
week, plus the last week's cases against that baseline; alerts on a drop and on recovery."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from valxel_sdlc import drift
from valxel_sdlc.evals import EvalCase, InMemoryEvalStore, summarize

T0 = datetime(2026, 11, 1, 12, tzinfo=UTC)
ENV = {"VALXEL_DRIFT_EVERY_DAYS": "7", "VALXEL_DRIFT_CASES_PER_PHASE": "3"}


def case(i, phase="spec", at=T0, data_class="general"):
    return EvalCase(id=f"F{i}:{phase}:1", product_id="FARM", feature_id=f"F{i}", phase=phase, data_class=data_class,
                    at=(at - timedelta(days=1)).isoformat(), config_version="c", ctx={}, output="doc", status="passed",
                    label={"decision": "approve", "first_time": True})


class Notes(dict):
    def put(self, k, v):
        self[k] = v


class Replay:
    """Scores the cases it's given; `passing` is the share that pass (the model's 'quality')."""

    def __init__(self, passing=1.0):
        self.passing, self.calls = passing, []

    def __call__(self, cases):
        self.calls.append([c.id for c in cases])
        n = len(cases)
        results = [{"case_id": c.id, "phase": c.phase, "passed": i < round(n * self.passing), "must_met": 1.0,
                    "critic_agrees": True, "pairwise": None, "cost_usd": 0.01, "duration_s": 1.0}
                   for i, c in enumerate(cases)]
        return {"results": results, "summary": summarize(results)}


@pytest.fixture
def setup(monkeypatch):
    store = InMemoryEvalStore()
    for i in range(6):
        store.capture(case(i))
    store.capture(case(99, data_class="phi"))  # never sent out by drift evals (general only by default)
    services = SimpleNamespace(evals=store, settings=SimpleNamespace(roles={}))
    monkeypatch.setattr("valxel_sdlc.pilot.config_version", lambda settings: "cfg-A")
    return services, store, Notes(), []


def test_the_first_run_pins_the_anchor_and_its_baseline(setup):
    services, _, notes, said = setup
    r = drift.run(services, notes, now=T0, replay=Replay(), alert=said.append, env=ENV)
    assert r["first_run_for_config"] and r["anchor_cases"] == 3 and r["regressions"] == []
    assert notes[drift.ANCHOR]["config"] == "cfg-A" and len(notes[drift.ANCHOR]["case_ids"]) == 3
    assert all("F99" not in i for i in notes[drift.ANCHOR]["case_ids"])
    assert said == []


def test_it_waits_a_week_between_runs(setup):
    services, _, notes, said = setup
    drift.run(services, notes, now=T0, replay=Replay(), alert=said.append, env=ENV)
    assert drift.run(services, notes, now=T0 + timedelta(days=3), replay=Replay(), env=ENV) is None
    assert drift.run(services, notes, now=T0 + timedelta(days=7), replay=Replay(), env=ENV) is not None


def test_same_cases_same_config_worse_results_is_drift(setup):
    services, store, notes, said = setup
    drift.run(services, notes, now=T0, replay=Replay(1.0), alert=said.append, env=ENV)
    week2 = T0 + timedelta(days=7)
    for i in range(10, 13):
        store.capture(case(i, at=week2))  # this week's work
    worse = Replay(0.34)
    r = drift.run(services, notes, now=week2, replay=worse, alert=said.append, env=ENV)
    assert worse.calls[0] == notes[drift.ANCHOR]["case_ids"]  # the same pinned cases
    assert any(x.startswith("Same cases, same config: all: pass rate fell") for x in r["regressions"])
    assert r["recent_cases"] == 3
    assert len(said) == 1 and said[0].startswith("Valxel drift evals: the agents did worse this week")
    r = drift.run(services, notes, now=week2 + timedelta(days=7), replay=Replay(1.0), alert=said.append, env=ENV)
    assert r["regressions"] == [] and said[-1] == "Valxel drift evals: back to the baseline."
    assert [x["regressions"] == [] for x in notes[drift.RUNS]] == [True, False, True]


def test_a_new_config_pins_a_new_anchor(setup, monkeypatch):
    services, _, notes, said = setup
    drift.run(services, notes, now=T0, replay=Replay(1.0), alert=said.append, env=ENV)
    monkeypatch.setattr("valxel_sdlc.pilot.config_version", lambda settings: "cfg-B")
    r = drift.run(services, notes, now=T0 + timedelta(days=7), replay=Replay(0.34), alert=said.append, env=ENV)
    assert r["first_run_for_config"] and r["regressions"] == []  # the change gate judged the new config
    assert notes[drift.ANCHOR]["config"] == "cfg-B"


def test_only_the_tools_owners_see_or_start_it(engine, monkeypatch):
    from fastapi.testclient import TestClient
    from test_api import SECRET, sign_in

    from valxel_sdlc.api.app import create_app
    from valxel_sdlc.web.auth import AuthConfig

    monkeypatch.setenv("VALXEL_EVAL_APPROVERS", "maya")
    started = []
    monkeypatch.setattr("valxel_sdlc.drift.run", lambda *a, **kw: started.append(kw))
    with TestClient(create_app(engine, auth=AuthConfig(mode="dev", secret=SECRET), web_dist=None)) as c:
        sign_in(c, "maya")
        assert c.get("/api/evals/drift").json() == {"latest": None, "runs": [], "anchor": {}}
        assert c.post("/api/evals/drift", json={}).status_code == 202
        sign_in(c, "someone")
        assert c.get("/api/evals/drift").status_code == 403


def test_a_real_replay_on_cases_the_pipeline_left(engine, monkeypatch):
    from conftest import approve

    from valxel_sdlc.evals import InMemoryEvalStore

    if engine.services.evals is None:
        engine.services.evals = InMemoryEvalStore()
    fid = engine.start(product_id="FARM", title="Soil sensor import")["feature_id"]
    approve(engine, fid, "g1")
    approve(engine, fid, "g2")
    assert engine.services.evals.cases(data_classes=("general",))  # discovery and spec, labelled at G1 and G2
    said = []
    r = drift.run(engine.services, engine.cluster, force=True, alert=said.append)
    assert r["anchor_cases"] >= 2 and r["first_run_for_config"]
    assert r["anchor"]["all"]["errors"] == 0 and r["anchor"]["all"]["pass_rate"] is not None
    assert engine.services.evals.cases(data_classes=("general",))[0].product_id == "FARM"

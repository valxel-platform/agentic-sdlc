"""Gate confidence (valxel_sdlc.confidence): learned per product from its own gate decisions,
shown at G1-G4, kept with the decision, and missed when build or the fit check undoes an approval."""

import pytest

from valxel_sdlc import confidence
from valxel_sdlc.confidence import InMemoryOutcomeStore, Settings, lower_bound, score
from valxel_sdlc.pilot import config_version

CFG = Settings(min_samples=20, high=0.95, medium=0.8)
KIND = "feature/general/own-lane"


def rows(approved, sent_back, missed=0, gate="g2", kind=KIND, config="c1"):
    out = [{"gate": gate, "kind": kind, "config": config, "decision": "approve", "missed": ""}
           for _ in range(approved)]
    for r in out[:missed]:
        r["missed"] = "reopened from build: spec"
    out += [{"gate": gate, "kind": kind, "config": config, "decision": "revise", "missed": ""}
            for _ in range(sent_back)]
    return out


def test_the_lower_bound_is_cautious_with_few_samples():
    assert lower_bound(38, 40) == pytest.approx(0.8597, abs=0.001)  # by hand: Wilson, z = 1.645
    assert lower_bound(5, 5) < 0.7 < lower_bound(200, 200)
    assert lower_bound(0, 0) == 0.0


def test_levels_need_enough_decisions_and_a_high_lower_bound():
    assert score(rows(10, 0), "g2", KIND, "c1", CFG)["level"] == "unknown"  # too few
    s = score(rows(120, 1), "g2", KIND, "c1", CFG)
    assert s["level"] == "high" and s["decisions"] == 121 and s["held"] == 120
    assert score(rows(38, 2), "g2", KIND, "c1", CFG)["level"] == "medium"
    assert score(rows(20, 10), "g2", KIND, "c1", CFG)["level"] == "low"


def test_an_approval_undone_later_is_a_miss():
    assert score(rows(120, 0, missed=12), "g2", KIND, "c1", CFG)["level"] == "medium"


def test_other_configs_kinds_and_gates_dont_count():
    history = rows(120, 0, config="old") + rows(120, 0, kind="feature/phi/own-lane") + rows(120, 0, gate="g3")
    assert score(history, "g2", KIND, "c1", CFG)["decisions"] == 0


def test_the_store_keeps_each_gate_s_first_decision_and_marks_misses():
    st = InMemoryOutcomeStore()
    base = {"product_id": "FARM", "feature_id": "F1", "kind": KIND, "config": "c1"}
    st.add({**base, "gate": "g2", "decision": "revise"})
    st.add({**base, "gate": "g2", "decision": "approve"})  # the second round: not the first decision
    st.add({**base, "gate": "g4", "decision": "approve"})
    st.missed("F1", ("g2", "g3", "g4"), "reopened from build: spec")
    got = {r["gate"]: r for r in st.rows("FARM")}
    assert got["g2"]["decision"] == "revise" and got["g2"]["missed"] == ""  # not approved, so not a miss
    assert got["g4"]["missed"] == "reopened from build: spec"


def test_calibration_says_how_often_each_level_held():
    rs = [{"decision": "approve", "missed": "", "predicted": {"level": "high"}}] * 9 + [
        {"decision": "approve", "missed": "fit", "predicted": {"level": "high"}},
        {"decision": "revise", "missed": "", "predicted": {"level": "low"}}]
    assert confidence.calibration(rs) == {"high": {"decisions": 10, "held": 9, "rate": 0.9},
                                          "low": {"decisions": 1, "held": 0, "rate": 0.0}}


def test_this_feature_must_pass_its_own_checks():
    clean = {"loop_count": {"spec": 1}, "events": [
        {"node": "spec", "kind": "passed", "critic": {"passed": True, "items": [{"id": "a", "met": True}]}}]}
    assert confidence.feature_checks(clean, "spec") == []
    messy = {"loop_count": {"spec": 3}, "feedback": {"spec": "tighten AC-2"}, "events": [
        {"node": "spec", "kind": "passed", "critic": {"passed": True, "items": [{"id": "AC", "met": False}]}}]}
    assert confidence.feature_checks(messy, "spec") == [
        "spec needed 3 tries", "checklist items not met: AC", "people asked for changes to it before"]


def test_what_the_approver_reads():
    hi = {"level": "high", "held": 120, "decisions": 121, "lower_bound": 0.96}
    assert "can be a quick read" in confidence.says("g4", "architecture", hi, [])  # VX-1: still decided
    assert "candidate for a quick approval" in confidence.says("g2", "spec", hi, [])
    assert "this one differs: spec needed 3 tries" in confidence.says(
        "g2", "spec", {**hi, "level": "medium"}, ["spec needed 3 tries"])
    assert confidence.says("g2", "spec", {"level": "unknown", "decisions": 3}, []).startswith("Not enough history")


def _approve_to_g4(engine, title):
    from conftest import approve

    fid = engine.start(product_id="FARM", title=title)["feature_id"]
    approve(engine, fid, "g1")
    approve(engine, fid, "g2")
    return fid


def test_the_pipeline_shows_records_and_learns(engine):
    svc = engine.services
    cfg = config_version(svc.settings)
    kind = KIND
    for i in range(60):  # a history of clean G2 approvals for FARM under this config
        svc.gate_outcomes.add({"product_id": "FARM", "feature_id": f"OLD-{i}", "gate": "g2", "kind": kind,
                               "config": cfg, "decision": "approve"})
    fid = engine.start(product_id="FARM", title="Soil sensor import")["feature_id"]
    from conftest import approve

    approve(engine, fid, "g1")
    [g2] = [p for p in engine.pending(fid) if p.get("gate") == "g2"]
    c = g2["confidence"]
    assert c["decisions"] == 60 and c["category_level"] == "high"
    assert c["level"] in ("high", "medium") and c["skippable"] is False
    approve(engine, fid, "g2")
    mine = {r["gate"]: r for r in svc.gate_outcomes.rows("FARM") if r["feature_id"] == fid}
    assert mine["g1"]["decision"] == "approve" and mine["g2"]["predicted"]["decisions"] == 60
    decided = [d for d in engine.view(fid)["gate_decisions"] if d["gate"] == "g2"]
    assert decided[0]["confidence"]["decisions"] == 60

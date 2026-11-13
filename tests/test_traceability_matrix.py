"""The requirements-to-tests matrix: NFRs and goal links in the PRD, test cases in the plan, tests
verified on the branch, a matrix that must be complete at G6 (or overridden, with a reason, by a
named person), frozen into the release record, mirrored to the work tracker and reconciled with it."""

import csv
import io
import json

import pytest
from fastapi.testclient import TestClient

from valxel_sdlc import traceability
from valxel_sdlc.agents.parse import parse_ac_goals, parse_acs, parse_cases, parse_nfrs
from valxel_sdlc.api.app import create_app
from valxel_sdlc.evaluators import checks
from valxel_sdlc.ops.changes import verify
from valxel_sdlc.web.auth import AuthConfig

PRD = """# PRD: Crop log
## Goals
- G1: Log crops offline (measured by M1)
## Non-goals
- Sync conflicts
## Acceptance criteria
- AC-1 (G1): Given a farmer offline, when they log a crop, then it is saved on the device
- AC-2 (G1): Given a bad date, when they save, then an error names the field
## Non-functional requirements
- NFR-1 (performance, G1): saving answers within 200 ms on a mid-range phone
- NFR-2 (accessibility): meets WCAG 2.1 AA
## Constitution articles
- VX-5
"""


def to_g6(engine, fid):
    for _ in range(30):
        ps = [p for p in engine.pending(fid) if p["type"] != "external"]
        if not ps or ps[0].get("gate") == "g6":
            return ps[0] if ps else None
        engine.decide(fid, ps[0]["interrupt_id"], {"decision": "approve", "approver": "x"})
    raise AssertionError("never reached G6")


# ---------------------------------------------------------------- the documents

def test_criteria_link_to_goals_and_nfrs_are_parsed():
    assert parse_acs(PRD)["AC-1"].startswith("Given a farmer offline")
    assert parse_ac_goals(PRD) == {"AC-1": ["G1"], "AC-2": ["G1"]}
    assert parse_nfrs(PRD) == {"NFR-1": {"kind": "performance", "goal": "G1", "text": "saving answers within 200 ms on a mid-range phone"},
                               "NFR-2": {"kind": "accessibility", "goal": "", "text": "meets WCAG 2.1 AA"}}
    assert checks.run_checks("spec", PRD, {"articles": ["VX-5"]}) == []


def test_the_spec_needs_goal_links_and_measurable_nfrs():
    bad = (PRD.replace("- AC-2 (G1):", "- AC-2:").replace("- AC-1 (G1):", "- AC-1 (G7):")
              .replace("within 200 ms on a mid-range phone", "is fast").replace("(accessibility)", "(vibes)"))
    found = checks.run_checks("spec", bad, {"articles": ["VX-5"]})
    assert "AC-2 names no goal: write '- AC-2 (G1): ...'" in found
    assert "AC-1 serves G7, which isn't one of the PRD's goals" in found
    assert "NFR-1 is not measurable: give a number, a percentile, a limit or a named standard" in found
    assert any(f.startswith("NFR-2's kind 'vibes'") for f in found)
    none = PRD.split("## Non-functional")[0] + "## Non-functional requirements\n- None: the product's budgets cover it\n## Constitution articles\n- VX-5\n"
    assert checks.run_checks("spec", none, {"articles": ["VX-5"]}) == []
    missing = PRD.split("## Non-functional")[0] + "## Constitution articles\n- VX-5\n"
    assert "PRD has no Non-functional requirements section (list them, or '- None: <why>')" in checks.run_checks(
        "spec", missing, {"articles": ["VX-5"]})


PLAN = """# Plan
## Tasks
```json
{"tasks": [{"id": "TASK-1", "repo": "o/app", "role": "mobile", "acs": ["AC-1", "AC-2"]}]}
```
## Traceability
- AC-1 -> TASK-1
- AC-2 -> TASK-1
- NFR-1 -> TASK-1
- NFR-2 -> deferred: the design system's audit covers it next sprint
## Test cases
- TC-1 (AC-1, happy): saves offline -> TASK-1 tests/crop.test.ts::saves_offline
- TC-2 (AC-2, error): names the bad field -> acceptance tests/crop.spec.ts "bad date"
- TC-3 (NFR-1, performance): save under 200 ms -> suite perf-smoke
- TC-4 (AC-1, edge): storage full -> manual: needs a full device
"""


def test_test_cases_name_a_requirement_a_kind_and_a_checkable_test():
    cases = {c["id"]: c for c in parse_cases(PLAN)}
    assert (cases["TC-1"]["via"], cases["TC-1"]["task"], cases["TC-1"]["file"], cases["TC-1"]["test"]) == (
        "test", "TASK-1", "tests/crop.test.ts", "saves_offline")
    assert (cases["TC-2"]["via"], cases["TC-2"]["test"]) == ("acceptance", "bad date")
    assert (cases["TC-3"]["via"], cases["TC-3"]["suite"]) == ("suite", "perf-smoke")
    assert cases["TC-4"]["via"] == "manual"
    assert checks.run_checks("plan", PLAN, {"parent_texts": {"PRD": PRD}}) == []
    bad = (PLAN.replace("- TC-3 (NFR-1, performance): save under 200 ms -> suite perf-smoke\n", "")
               .replace("-> TASK-1 tests/crop.test.ts::saves_offline", "-> TASK-9 tests/crop.test.ts::saves_offline")
               .replace("-> manual: needs a full device", "-> somewhere"))
    found = checks.run_checks("plan", bad + "- TC-5 (AC-7, happy): x -> suite e2e\n", {"parent_texts": {"PRD": PRD}})
    assert "NFR-1 has no test case" in found and "TC-1 names TASK-9, which isn't a task in the plan" in found
    assert any(f.startswith("TC-4's test 'somewhere' is not") for f in found)
    assert "TC-5 tests AC-7, which the PRD doesn't have" in found


# ---------------------------------------------------------------- tests on the branch

def test_a_planned_test_missing_from_the_branch_goes_back_to_its_coder(engine, monkeypatch):
    from valxel_sdlc.agents import templates
    from valxel_sdlc.graph import phase

    real, calls = templates.stub_tests, {"n": 0}

    def forgetful(cases):
        calls["n"] += 1
        files = real(cases)
        if calls["n"] == 1:  # the first coder leaves out its first test
            first = cases[0]
            files[first["file"]] = files[first["file"]].replace(first["test"], "something_else")
        return files

    monkeypatch.setattr(phase.templates, "stub_tests", forgetful)
    fid = engine.start(product_id="FARM", title="Offline crop log", description="x")["feature_id"]
    v = engine.auto_drive(fid)
    assert v["status"] == "done" and calls["n"] >= 3  # one task redone
    t = engine.trace(fid)
    assert t["complete"] and all(c["status"] == "passed" for r in t["criteria"] for c in r["cases"])


# ---------------------------------------------------------------- the matrix

def test_a_stub_feature_reaches_g6_with_a_complete_matrix_and_exports_it(engine):
    fid = engine.start(product_id="FARM", title="Offline crop log", description="x")["feature_id"]
    g6 = to_g6(engine, fid)
    assert g6["traceability"]["complete"] and not g6["traceability"]["blocks"]
    t = engine.trace(fid)
    assert {r["id"] for r in t["nfrs"]} == {"NFR-1", "NFR-2"} and t["goals"][0]["requirements"]
    rows = list(csv.DictReader(io.StringIO(traceability.to_csv(t))))
    assert len(rows) == sum(len(r["cases"]) for r in t["criteria"] + t["nfrs"])
    assert set(rows[0]) == set(traceability.COLUMNS) and {r["status"] for r in rows} == {"passed"}


def test_deferred_requirements_are_listed_not_gaps(engine):
    state = {"feature_id": "F", "tasks": [{"id": "TASK-1", "repo": "o/app", "acs": ["AC-1", "AC-2"]}],
             "cases": parse_cases(PLAN), "pull_requests": {}, "quality": {}}
    t = traceability.build(state, lambda k: {"PRD": PRD, "PLAN": PLAN}.get(k, ""))
    assert t["deferred"] == [{"id": "NFR-2", "reason": "the design system's audit covers it next sprint"}]
    assert not any("NFR-2" in g for g in t["gaps"])


# ---------------------------------------------------------------- G6

@pytest.fixture
def blocked(engine, monkeypatch):
    """A feature at G6 whose first test case is manual: the matrix can't be complete."""
    real = traceability.case_status
    monkeypatch.setattr(traceability, "case_status",
                        lambda case, *a: "manual" if case["id"] == "TC-1" else real(case, *a))
    fid = engine.start(product_id="FARM", title="Offline crop log", description="x")["feature_id"]
    g6 = to_g6(engine, fid)
    assert g6["traceability"]["blocks"] and any(g.startswith("TC-1 (AC-1, happy)") for g in g6["traceability"]["gaps"])
    assert g6["traceability"]["override_approvers"] == ["acme-farm/product-owners", "acme-farm/qa"]
    return fid, g6


def test_g6_wont_release_an_incomplete_matrix_without_an_override(engine, blocked):
    fid, g6 = blocked
    engine.decide(fid, g6["interrupt_id"], {"decision": "approve", "approver": "rm"})
    [again] = [p for p in engine.pending(fid) if p.get("gate") == "g6"]
    assert "isn't complete" in again["refused"] and engine.view(fid)["status"] != "done"
    engine.decide(fid, again["interrupt_id"], {"decision": "approve", "approver": "po",
                                               "override_reason": "TC-1 checked by hand on three devices"})
    v = engine.auto_drive(fid)
    assert v["status"] == "done"
    over = [e for e in v["events"] if e["kind"] == "traceability_overridden"]
    assert over and over[0]["detail"] == "TC-1 checked by hand on three devices" and over[0]["approver"] == "po"
    rec = next(r for r in engine.services.changes.list("FARM") if r["kind"] == "release")
    tr = rec["evidence"]["traceability"]
    assert tr["override"] == {"by": "po", "reason": "TC-1 checked by hand on three devices"} and not tr["complete"]
    assert any(c["status"] == "manual" for r in tr["requirements"] for c in r["cases"])
    assert verify(engine.services.changes.all())["ok"]


def test_only_traceability_approvers_can_override_through_the_api(engine, blocked):
    fid, g6 = blocked
    with TestClient(create_app(engine, auth=AuthConfig(mode="dev", secret="x" * 40), web_dist=None)) as c:
        c.post("/api/auth/dev-login", json={"login": "rm", "teams": ["acme-farm/release-managers"]})
        url = f"/api/features/{fid}/decisions/{g6['interrupt_id']}"
        r = c.post(url, json={"decision": "approve"})
        assert r.status_code == 409 and "isn't complete" in r.json()["detail"]
        r = c.post(url, json={"decision": "approve", "override_reason": "ship it"})
        assert r.status_code == 403 and "acme-farm/qa" in r.json()["detail"]
        c.post("/api/auth/dev-login", json={"login": "qa-lead", "teams": ["acme-farm/release-managers", "acme-farm/qa"]})
        r = c.post(url, json={"decision": "approve", "override_reason": "checked by hand"})
        assert r.status_code == 202
        csv_text = c.get(f"/api/features/{fid}/trace.csv").text
        assert csv_text.startswith("feature,requirement,type,") and "manual" in csv_text


def test_a_hotfix_is_shown_its_gaps_but_not_held(engine, monkeypatch):
    from valxel_sdlc.graph.pipeline import release_traceability

    monkeypatch.setattr(traceability, "case_status", lambda *a: "pending")
    fid = engine.start(product_id="FARM", title="Offline crop log", description="x")["feature_id"]
    to_g6(engine, fid)
    state = engine._state(fid)
    lane = engine.services.feature_snapshot(state).product
    assert release_traceability(engine.services, state, lane)["blocks"]
    assert not release_traceability(engine.services, {**state, "track": "hotfix"}, lane)["blocks"]


# ---------------------------------------------------------------- the tracker

def test_the_tracker_is_reconciled_and_drift_warns_or_blocks(engine):
    from valxel_sdlc.graph.pipeline import release_traceability

    fid = engine.start(product_id="FARM", title="Offline crop log", description="x")["feature_id"]
    to_g6(engine, fid)
    state = engine._state(fid)
    jira = engine.services.trackers["jira"]
    assert engine.work.reconcile(state) == {"drift": [], "block": False}
    items = {it["summary"].split("]")[0].split("/")[-1]: it for it in jira.items.values() if "/" in it["summary"]}
    items["TC-1"]["summary"] = "renamed by someone"
    items["NFR-2"]["status"] = "done"  # closed by hand while its test case hasn't run
    got = engine.work.reconcile(state)
    assert any("TC-1 (subtask)" in d and "renamed in the tracker" in d for d in got["drift"])
    lane = engine.services.feature_snapshot(state).product
    warn = release_traceability(engine.services, state, lane)
    assert warn["drift"] and warn["complete"] == (not warn["gaps"]) and warn["blocks"] == bool(warn["gaps"])
    cfg, _ = engine.work.config(state)
    cfg["block_on_drift"] = True  # a product that holds releases for drift
    held = release_traceability(engine.services, state, lane)
    assert held["drift_blocks"] and held["blocks"]

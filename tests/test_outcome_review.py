"""Outcomes, step 5: the product council reviews an initiative on its review date, or when its
goal reads off track twice: continue, change course, or stop (retiring features that are less
effort to remove than to keep). Each decision is recorded on the initiative's file."""

from __future__ import annotations

import shutil
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from valxel_sdlc.config import load_settings
from valxel_sdlc.engine import FeatureEngine
from valxel_sdlc.outcomes import Reading, readouts as ro, review
from valxel_sdlc.roadmap import read_roadmap
from valxel_sdlc.services import build_services

ROOT = Path(__file__).resolve().parents[1]
INI = """---
id: CLN-INI-1
title: Self check-in
goal: GOAL-CLN-1
horizon: now
status: approved
budget_usd: 400
lanes: [CLN]
release_model: launch
approved_by: maria
approved_at: '{approved}'
{extra}---
# CLN-INI-1: Self check-in

## Outcome

Patients check themselves in.

## Candidate features

- Kiosk check-in: patients scan a code at a kiosk
"""
COUNCIL = "valxel-platform/product-council"


def make(tmp_path, approved="2026-08-01", extra=""):
    gov = tmp_path / "gov"
    shutil.copytree(ROOT / "examples" / "governance", gov)
    d = gov / "acme-clinic" / "cln-governance" / "roadmap" / "initiatives"
    d.mkdir(parents=True)
    (d / "CLN-INI-1.md").write_text(INI.format(approved=approved, extra=extra))
    s = load_settings(mode="stub", database_url="", artifact_dir=tmp_path / "a", governance_source="local",
                      governance_root=gov)
    svc = build_services(s)
    return FeatureEngine(svc, InMemorySaver()), svc


def settle(eng, tid, timeout=10.0):
    import time

    end = time.time() + timeout
    while time.time() < end and (eng.is_running(tid) or not eng.pending(tid)):
        time.sleep(0.02)


def gate(eng, rid):
    [p] = [p for p in eng.pending(rid) if p.get("gate") == "review"]
    return p


def written(svc, path="roadmap/initiatives/CLN-INI-1.md"):
    return next(c for (repo, branch, p), c in svc.github.files.items() if p == path and branch.startswith("valxel/cln-rev"))


def test_a_review_is_due_on_its_date_or_after_two_off_track_readings(tmp_path):
    eng, svc = make(tmp_path)
    road = read_roadmap(svc, "CLN")
    [i] = road["initiatives"]
    assert review.review_date(i) == date(2026, 10, 30)  # 90 days after approval when no review_by is set
    assert review.due(svc, "CLN", road, {"goals": {}}, today=date(2026, 10, 1)) == []
    [(got, why)] = review.due(svc, "CLN", road, {"goals": {}}, today=date(2026, 11, 1))
    assert got["id"] == "CLN-INI-1" and "review date" in why
    m = ro.metrics(svc, "CLN")["GOAL-CLN-1"]
    off = {"goals": {"GOAL-CLN-1": {"metric": m.as_dict(), "trend": [
        {"end": "2026-09-01T00:00:00+00:00", "value": 6.9}, {"end": "2026-09-08T00:00:00+00:00", "value": 7.1}]}}}
    [(_, why)] = review.due(svc, "CLN", road, off, today=date(2026, 9, 9))
    assert "off track twice" in why
    once = {"goals": {"GOAL-CLN-1": {**off["goals"]["GOAL-CLN-1"], "trend": off["goals"]["GOAL-CLN-1"]["trend"][-1:]}}}
    assert review.due(svc, "CLN", road, once, today=date(2026, 9, 9)) == []


def test_continue_sets_the_next_review_and_records_it(tmp_path):
    eng, svc = make(tmp_path)
    [rid] = eng.start_due_reviews("CLN", today=date(2026, 11, 1))
    settle(eng, rid)
    assert eng.start_due_reviews("CLN", today=date(2026, 11, 1)) == [rid]  # one open review per initiative
    p = gate(eng, rid)
    assert p["approvers"] == [COUNCIL] and p["allowed"] == ["continue", "change", "stop"]
    assert "why now: its review date, 2026-10-30, has come" in p["summary"] and "## The goal" in p["preview"]
    eng.decide(rid, p["interrupt_id"], {"decision": "continue", "review_by": "2027-01-15", "approver": "sunil",
                                        "feedback": "Give the kiosks a full season"})
    v = eng.view(rid)
    assert v["kind"] == "review" and v["status"] == "continued" and v["record_pr"]["url"]
    text = written(svc)
    assert "review_by: '2027-01-15'" in text and "## Reviews" in text
    assert "continue, next review 2027-01-15 (sunil): Give the kiosks a full season" in text
    assert "## Outcome\n\nPatients check themselves in." in text  # the rest of the file is kept


def test_change_opens_an_intent_under_the_initiative(tmp_path):
    eng, svc = make(tmp_path)
    rid = eng.create_review(product_id="CLN", initiative_id="CLN-INI-1", why="asked")
    eng.decide(rid, gate(eng, rid)["interrupt_id"], {"decision": "change", "approver": "sunil",
                                                     "feedback": "Text patients a link instead of a kiosk"})
    [fid] = eng.view(rid)["opened"]
    s = eng._state(fid)
    assert s["initiative_id"] == "CLN-INI-1" and s["title"] == "Change course: Self check-in"
    assert "Text patients a link instead of a kiosk" in s["description"]
    assert "change course, opened " + fid in written(svc)


def test_stop_keeps_features_and_retires_only_the_ticked_ones(tmp_path, monkeypatch):
    eng, svc = make(tmp_path)
    svc.features = lambda pid: [
        {"feature_id": "CLN-FEAT-7", "title": "Kiosk check-in", "status": "released", "initiative_id": "CLN-INI-1",
         "cost_usd": 120.0},
        {"feature_id": "CLN-FEAT-8", "title": "Queue screen", "status": "released", "initiative_id": "CLN-INI-1",
         "cost_usd": 80.0}]
    states = {"CLN-FEAT-7": {"flag": "cln.cln-feat-7.kiosk", "release": {"flag_on": True, "flag_percent": 25.0}},
              "CLN-FEAT-8": {"flag": "cln.cln-feat-8.queue", "release": {"flag_on": True, "flag_percent": 100.0}}}
    svc.feature_state = lambda fid: states.get(fid, {})
    opened = []
    svc.create_feature = lambda **kw: opened.append(kw) or f"CLN-FEAT-{9 + len(opened)}"
    rid = eng.create_review(product_id="CLN", initiative_id="CLN-INI-1", why="asked")
    p = gate(eng, rid)
    assert [r["feature_id"] for r in p["retire_candidates"]] == ["CLN-FEAT-7"]  # fully on: not cheaper to remove
    assert "$200.00 of $400.00" in p["preview"] and "on for 25% of users" in p["preview"]
    eng.decide(rid, p["interrupt_id"], {"decision": "stop", "approver": "sunil", "retire": ["CLN-FEAT-7", "CLN-FEAT-8"]})
    v = eng.view(rid)
    assert v["status"] == "stopped" and [o["title"] for o in opened] == ["Retire Kiosk check-in"]
    assert "cln.cln-feat-7.kiosk" in opened[0]["description"]
    text = written(svc)
    assert "status: dropped" in text and "stopped (sunil); retiring CLN-FEAT-7" in text


def test_the_roadmap_shows_review_dates_and_reviews(tmp_path):
    eng, svc = make(tmp_path, extra="review_by: '2027-02-01'\n")
    from valxel_sdlc.roadmap.views import product_roadmap

    [i] = product_roadmap(eng, "CLN")["initiatives"]
    assert i["review_by"] == "2027-02-01" and i["reviews"] == []
    rid = eng.create_review(product_id="CLN", initiative_id="CLN-INI-1", why="asked")
    [i] = product_roadmap(eng, "CLN")["initiatives"]
    assert i["reviews"][0]["id"] == rid and i["reviews"][0]["status"] == "waiting"

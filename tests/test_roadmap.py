"""Above features: goals, initiatives with their own gate (G0), roadmap fit at G1, budget roll-up,
releases as launch groups (feature flags) or release trains, and the roadmap record in Git."""

import shutil
from pathlib import Path

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from vrixel_sdlc.config import load_settings
from vrixel_sdlc.engine import FeatureEngine
from vrixel_sdlc.knowledge import render
from vrixel_sdlc.roadmap import read_roadmap
from vrixel_sdlc.roadmap.initiative import check_draft, parse_draft
from vrixel_sdlc.roadmap.record import parse_initiative, parse_release
from vrixel_sdlc.roadmap.views import product_roadmap
from vrixel_sdlc.services import build_services

ROOT = Path(__file__).resolve().parents[1]
REQUEST = ("Register walk-in patients at the front desk; Capture consent during registration; "
           "Show the waiting queue to clinicians")


@pytest.fixture
def gov_root(tmp_path):
    root = tmp_path / "governance"
    shutil.copytree(ROOT / "examples" / "governance", root)
    return root


def make(tmp_path, gov_root):
    s = load_settings(mode="stub", database_url="", artifact_dir=tmp_path / "a",
                      governance_source="local", governance_root=gov_root)
    svc = build_services(s)
    return FeatureEngine(svc, InMemorySaver()), svc


def gate(eng, tid, name):
    return next(p for p in eng.pending(tid) if p.get("gate") == name)


def approved_initiative(eng, title="Walk-in intake", request=REQUEST, product="CLN"):
    iid = eng.create_initiative(product_id=product, title=title, request=request, created_by="maria")
    for _ in range(5):
        p = [x for x in eng.pending(iid) if x.get("gate") == "g0"]
        if not p:
            break
        eng.decide(iid, p[0]["interrupt_id"], {"decision": "approve", "approver": f"po-{p[0]['lane']}"})
    return iid


# --------------------------------------------------------------------------- initiatives


def test_an_initiative_is_drafted_checked_approved_at_g0_and_recorded_in_git(tmp_path, gov_root):
    eng, svc = make(tmp_path, gov_root)
    iid = eng.create_initiative(product_id="CLN", title="Walk-in intake", request=REQUEST, created_by="maria")
    assert iid == "CLN-INI-1"
    g0 = gate(eng, iid, "g0")
    assert g0["approvers"] == ["acme-clinic/product-owners"] and g0["findings"] == []
    i = g0["initiative"]
    assert i["goal"] == "GOAL-CLN-1" and i["horizon"] == "now" and i["release_model"] == "launch"
    assert [c["title"] for c in i["candidates"]][:2] == ["Register walk-in patients at the front desk",
                                                         "Capture consent during registration"]
    assert eng.inbox("CLN")[0]["kind"] == "initiative"

    eng.decide(iid, g0["interrupt_id"], {"decision": "approve", "approver": "maria-po"})
    v = eng.view(iid)
    assert v["status"] == "approved" and v["record_pr"]["url"]
    pr = next(p for p in svc.github.prs.values() if p["branch"] == f"vrixel/{iid}")
    record = parse_initiative(svc.github.files[("acme-clinic/cln-governance", pr["branch"], f"roadmap/initiatives/{iid}.md")])
    assert record["status"] == "approved" and record["goal"] == "GOAL-CLN-1" and record["budget_usd"] > 0
    assert len(record["candidates"]) == 3

    rm = product_roadmap(eng, "CLN")
    [row] = rm["initiatives"]
    assert row["id"] == iid and row["record"] == "pull request" and row["progress"]["planned"] == 3


def test_a_cross_lane_initiative_needs_each_lanes_approval(tmp_path, gov_root):
    eng, _ = make(tmp_path, gov_root)
    iid = eng.create_initiative(product_id="CLN", title="Card payments at check-in",
                                request="Take copays by card at check-in through PAY; Receipts by email")
    first = gate(eng, iid, "g0")
    assert first["lanes"] == ["CLN", "PAY"] and first["lane"] == "CLN"
    eng.decide(iid, first["interrupt_id"], {"decision": "approve", "approver": "cln-po"})
    second = gate(eng, iid, "g0")
    assert second["lane"] == "PAY" and second["approvers"]
    eng.decide(iid, second["interrupt_id"], {"decision": "approve", "approver": "pay-po"})
    assert eng.view(iid)["status"] == "approved"


def test_revise_and_drop_at_g0(tmp_path, gov_root):
    eng, svc = make(tmp_path, gov_root)
    iid = eng.create_initiative(product_id="CLN", title="Walk-in intake", request=REQUEST)
    eng.decide(iid, gate(eng, iid, "g0")["interrupt_id"],
               {"decision": "revise", "approver": "po", "feedback": "Put the waiting queue in its own initiative"})
    v = eng.view(iid)
    assert "Put the waiting queue" in v["content"] and v["status"] == "awaiting_approval"
    eng.decide(iid, gate(eng, iid, "g0")["interrupt_id"], {"decision": "abort", "approver": "po", "feedback": "not now"})
    assert eng.view(iid)["status"] == "dropped"
    assert not [p for p in svc.github.prs.values() if p["branch"] == f"vrixel/{iid}"]


def test_initiative_checks():
    ctx = {"product_id": "CLN", "initiative_id": "CLN-INI-2", "title": "Walk-in intake",
           "goals": [{"id": "GOAL-CLN-1", "title": "Faster check-in"}], "dependency_lanes": ["SS", "PAY"],
           "max_budget_usd": 5000, "features": [{"feature_id": "CLN-FEAT-1", "title": "Capture consent during registration",
                                                 "description": "", "status": "released"}],
           "initiatives": [{"id": "CLN-INI-1", "title": "Walk-in intake", "outcome": "Walk-in patients", "status": "approved"}]}
    text = """## Goal
- GOAL-CLN-9: unknown
## Outcome
Walk-in patients
## Success measure
## Horizon
- soon
## Budget
- 9000
## Lanes
- SHOP
## Release model
- big-bang
## Candidate features
- Capture consent during registration: again
"""
    found = check_draft(parse_draft(text), ctx)
    expected = ["Goal GOAL-CLN-9 is not in", "No success measure", "Horizon must be one of", "Budget envelope 9000",
                "Lanes must include the product", "Lane SHOP is not the product", "Release model must be",
                "repeats CLN-FEAT-1", "looks like initiative CLN-INI-1"]
    for e in expected:
        assert any(e in f for f in found), (e, found)
    ok = parse_draft(text.replace("GOAL-CLN-9: unknown", "New goal: Faster walk-ins (measure: wait under 10 minutes)"))
    assert ok["new_goal"] == {"title": "Faster walk-ins", "measure": "wait under 10 minutes"}


# --------------------------------------------------------------------------- features under initiatives


def test_features_start_from_an_initiative_and_g1_shows_roadmap_fit(tmp_path, gov_root):
    eng, svc = make(tmp_path, gov_root)
    iid = approved_initiative(eng)
    fid = eng.create(product_id="CLN", title="Register walk-in patients at the front desk",
                     description="Register walk-in patients at the front desk", initiative_id=iid)
    fit = gate(eng, fid, "g1")["roadmap_fit"]
    assert fit["initiative"]["id"] == iid and fit["initiative"]["horizon"] == "now"
    assert 0 <= fit["spent_usd"] < fit["budget_usd"] and not fit["over"]

    other = eng.create(product_id="CLN", title="Lab results viewer")
    assert gate(eng, other, "g1")["roadmap_fit"] == {"unplanned": True}
    assert [f["feature_id"] for f in product_roadmap(eng, "CLN")["unplanned"]] == [other]

    v = eng.view(iid)
    assert [f["feature_id"] for f in v["features"]] == [fid] and v["candidates"][0]["started"] is True
    with pytest.raises(ValueError, match="not an approved initiative"):
        eng.create(product_id="CLN", title="Something else", initiative_id="CLN-INI-99")

    text = render(svc.knowledge.collect(svc.snapshot("CLN")))
    assert f"- {iid} (now): Walk-in intake, goal GOAL-CLN-1" in text and f"[{iid}]" in text


def test_the_tracker_puts_feature_epics_under_their_initiative(tmp_path, gov_root):
    eng, svc = make(tmp_path, gov_root)
    iid = approved_initiative(eng)
    fid = eng.create(product_id="CLN", title="Register walk-in patients at the front desk", initiative_id=iid)
    eng.decide(fid, gate(eng, fid, "g1")["interrupt_id"], {"decision": "approve", "approver": "po"})
    items = svc.trackers["openproject"].items
    [ini] = [i for i in items.values() if i["kind"] == "initiative"]
    [epic] = [i for i in items.values() if i["kind"] == "epic"]
    assert ini["marker"] == iid and ini["status"] == "in_progress" and epic["parent"] == ini["id"]


# --------------------------------------------------------------------------- releases


def test_a_launch_group_ships_dark_and_turns_on_together(tmp_path, gov_root):
    eng, svc = make(tmp_path, gov_root)
    a = eng.create(product_id="FARM", title="Offline crop log")
    b = eng.create(product_id="FARM", title="Soil sensor import")
    rid = eng.create_release(product_id="FARM", title="Spring season", model="launch", features=[a, b])
    eng.auto_drive(a)
    assert eng.view(a)["release"]["flag_on"] is False  # in production, dark
    assert eng.view(rid)["status"] == "planned"  # still waiting for b
    eng.auto_drive(b)
    g = gate(eng, rid, "release")
    assert g["features"] == [a, b] and g["approvers"] == ["acme-farm/release-managers"]
    assert "turns on the flags" in g["summary"] and f"{a}: Offline crop log" in g["preview"]
    eng.decide(rid, g["interrupt_id"], {"decision": "approve", "approver": "rm"})
    assert eng.view(rid)["status"] == "shipped"
    assert eng.view(a)["release"]["flag_on"] is True and eng.view(b)["release"]["flag_on"] is True
    files = [p for (repo, br, p) in svc.github.files if br == f"vrixel/{rid}"]
    assert files == [f"roadmap/releases/{rid}.md"]
    rec = parse_release(svc.github.files[("acme-farm/farm-governance", f"vrixel/{rid}", f"roadmap/releases/{rid}.md")])
    assert rec["status"] == "shipped" and rec["approved_by"] == "rm" and rec["features"] == [a, b]


def test_a_feature_alone_goes_live_at_g6(tmp_path, gov_root):
    eng, _ = make(tmp_path, gov_root)
    a = eng.create(product_id="FARM", title="Offline crop log")
    assert eng.auto_drive(a)["release"]["flag_on"] is True


def test_a_release_train_approves_its_features_together(tmp_path, gov_root):
    eng, _ = make(tmp_path, gov_root)
    a = eng.create(product_id="FARM", title="Offline crop log")
    b = eng.create(product_id="FARM", title="Soil sensor import")
    rid = eng.create_release(product_id="FARM", title="Certified build", model="train", features=[a, b],
                             target_date="2027-03-01")
    for f in (a, b):
        v = eng.auto_drive(f)
        assert [p.get("wait") for p in v["pending"]] == ["release_train"]  # past staging, waiting for the train
    g = gate(eng, rid, "release")
    assert "promotes to production" in g["summary"] and "22/22 suites passed" in g["evidence"]
    eng.decide(rid, g["interrupt_id"], {"decision": "approve", "approver": "rm"})
    for f in (a, b):
        v = eng.view(f)
        assert v["status"] == "done"
        assert [(g["gate"], g["approver"]) for g in v["gate_decisions"] if g["gate"] == "g6"] == [("g6", "rm")]


def test_a_cancelled_train_hands_its_features_back_to_g6(tmp_path, gov_root):
    eng, _ = make(tmp_path, gov_root)
    a = eng.create(product_id="FARM", title="Offline crop log")
    rid = eng.create_release(product_id="FARM", title="Certified build", model="train", features=[a])
    eng.auto_drive(a)
    eng.decide(rid, gate(eng, rid, "release")["interrupt_id"], {"decision": "abort", "approver": "rm", "feedback": "x"})
    assert eng.view(rid)["status"] == "cancelled"
    assert gate(eng, a, "g6")  # back to a person


def test_release_membership_rules(tmp_path, gov_root):
    eng, _ = make(tmp_path, gov_root)
    a = eng.create(product_id="FARM", title="Offline crop log")
    b = eng.create(product_id="FARM", title="Soil sensor import")
    eng.create_release(product_id="FARM", title="One", model="launch", features=[a])
    with pytest.raises(ValueError, match="already in FARM-REL-1"):
        eng.create_release(product_id="FARM", title="Two", model="train", features=[a])
    with pytest.raises(ValueError, match="not a feature of FARM"):
        eng.create_release(product_id="FARM", title="Two", model="train", features=["FARM-FEAT-99"])
    eng.auto_drive(b)
    with pytest.raises(ValueError, match="released"):
        eng.create_release(product_id="FARM", title="Two", model="train", features=[b])


def test_a_stopped_member_drops_out_of_its_release(tmp_path, gov_root):
    eng, _ = make(tmp_path, gov_root)
    a = eng.create(product_id="FARM", title="Offline crop log")
    b = eng.create(product_id="FARM", title="Soil sensor import")
    rid = eng.create_release(product_id="FARM", title="Spring", model="launch", features=[a, b])
    eng.decide(b, gate(eng, b, "g1")["interrupt_id"], {"decision": "abort", "approver": "po", "feedback": "no"})
    eng.auto_drive(a)
    g = gate(eng, rid, "release")
    assert g["features"] == [a] and g["dropped"] == [b]


# --------------------------------------------------------------------------- record in Git


def test_the_roadmap_is_read_from_the_governance_repo(tmp_path, gov_root):
    d = gov_root / "acme-clinic" / "cln-governance" / "roadmap" / "initiatives"
    d.mkdir(parents=True)
    (d / "CLN-INI-7.md").write_text("""---
id: CLN-INI-7
title: Discharge summaries
goal: GOAL-CLN-1
horizon: next
status: approved
budget_usd: 800
lanes: [CLN]
release_model: train
---
# CLN-INI-7: Discharge summaries

## Outcome

Summaries reach the GP the same day.

## Candidate features

- Draft summary: from the encounter
""")
    eng, svc = make(tmp_path, gov_root)
    rm = read_roadmap(svc, "CLN")
    assert rm["goals"][0]["id"] == "GOAL-CLN-1"
    [i] = rm["initiatives"]
    assert i["horizon"] == "next" and i["release_model"] == "train" and i["candidates"][0]["title"] == "Draft summary"
    fid = eng.create(product_id="CLN", title="Draft discharge summary", initiative_id="CLN-INI-7")
    assert gate(eng, fid, "g1")["roadmap_fit"]["initiative"]["id"] == "CLN-INI-7"
    assert product_roadmap(eng, "CLN")["initiatives"][0]["record"] == "merged"

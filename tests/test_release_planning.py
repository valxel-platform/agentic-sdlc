"""Releases planned at G0 and confirmed at G1 (T-95, T-96, T-97, T-99): an initiative's release model,
date and must-haves; its planned release gathering the features that start from it; G1's "how does
this ship?"; changing it until G6; the readiness summary; and a launch member without a flag."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from test_api import SECRET, sign_in
from test_roadmap import ROOT, approved_initiative, gate, make

from valxel_sdlc.api.app import create_app
from valxel_sdlc.graph.gates import launch_flag_warning
from valxel_sdlc.roadmap.initiative import check_draft, parse_draft
from valxel_sdlc.web.auth import AuthConfig


@pytest.fixture
def gov_root(tmp_path):
    import shutil

    root = tmp_path / "governance"
    shutil.copytree(ROOT / "examples" / "governance", root)
    return root


DRAFT = """# Initiative: Spring season
## Goal
- GOAL-FARM-1: More farmers log daily
## Success measure
- Daily logs per farmer
## Horizon
- now
## Budget
- 200
## Lanes
- FARM
## Release model
- launch
## Release date
- 2027-03-01
## Candidate features
- Offline crop log (must): log without signal
- Soil sensor import: read the sensors
"""
CTX = {"goals": [{"id": "GOAL-FARM-1", "title": "More farmers log daily"}], "max_budget_usd": 500,
       "product_id": "FARM", "today": "2026-11-20"}


def test_g0_takes_a_date_and_must_haves():
    d = parse_draft(DRAFT)
    assert d["target_date"] == "2027-03-01" and d["release_model"] == "launch"
    assert [(c["title"], c["must"]) for c in d["candidates"]] == [("Offline crop log", True),
                                                                   ("Soil sensor import", False)]
    assert check_draft(d, CTX) == []
    assert parse_draft(DRAFT.replace("- 2027-03-01", "- none"))["target_date"] == ""


@pytest.mark.parametrize(("change", "says"), [
    (("- 2027-03-01", "- March"), "must be a date"),
    (("- 2027-03-01", "- 2026-01-01"), "must be in the future"),
    (("## Release model\n- launch", "## Release model\n- each"), "A release date needs a release"),
    (("## Release model\n- launch\n## Release date\n- 2027-03-01", "## Release model\n- each\n## Release date\n- none"),
     "Must-have features need a release"),
    (("## Release model\n- launch", "## Release model\n- soon"), "Release model must be"),
])
def test_g0_checks_the_release_plan(change, says):
    old, new = change
    assert any(says in f for f in check_draft(parse_draft(DRAFT.replace(old, new)), CTX))


def test_an_initiatives_features_join_its_planned_release(tmp_path, gov_root):
    eng, _ = make(tmp_path, gov_root)
    iid = approved_initiative(eng)  # three candidates, released as a launch
    fid = eng.create(product_id="CLN", title="Register walk-in patients at the front desk",
                     description="Register walk-in patients at the front desk", initiative_id=iid)
    ship = eng.view(fid)["ship"]
    rel = eng.initiative_release(iid, create=False)
    assert ship["release_id"] == rel["id"] and rel["model"] == "launch" and rel["open"]
    assert eng.release_for(fid)["id"] == rel["id"] and eng.release_members(rel["id"]) == [fid]
    r = eng.release_readiness(rel["id"])
    assert r["counts"]["design"] == 1 and r["counts"]["not_started"] == 2 and r["total"] == 3
    assert r["says"] == "0 of 3 ready; 1 in design; 2 not started"

    # G1 asks; the approver ships it on its own instead
    g1 = gate(eng, fid, "g1")
    assert g1["ship"]["current"]["release_id"] == rel["id"] and [x["id"] for x in g1["ship"]["releases"]] == [rel["id"]]
    eng.decide(fid, g1["interrupt_id"], {"decision": "approve", "approver": "po", "ship": ""})
    assert eng.view(fid)["ship"]["own"] and eng.release_for(fid) is None and eng.release_members(rel["id"]) == []
    assert any(e["node"] == "g1" and e["kind"] == "ship" for e in eng.view(fid)["events"])
    # ...and changes it back before G6
    assert eng.set_ship(fid, rel["id"], by="po")["release_id"] == rel["id"]
    assert eng.release_members(rel["id"]) == [fid]
    with pytest.raises(ValueError, match="isn't an open release"):
        eng.set_ship(fid, "CLN-REL-99", by="po")


def test_a_feature_outside_an_initiative_ships_on_its_own_unless_it_joins(tmp_path, gov_root):
    eng, _ = make(tmp_path, gov_root)
    a = eng.create(product_id="FARM", title="Offline crop log")
    assert eng.view(a)["ship"] == {"own": True, "why": "No initiative: on its own"} and eng.release_for(a) is None
    b = eng.create(product_id="FARM", title="Soil sensor import")
    rid = eng.create_release(product_id="FARM", title="Spring season", model="launch", features=[b])
    eng.set_ship(a, rid, by="po")
    assert eng.release_for(a)["id"] == rid and set(eng.release_members(rid)) == {a, b}
    eng.set_ship(b, "", by="po")  # listed when it was made, then chose to ship on its own
    assert eng.release_members(rid) == [a] and eng.release_for(b) is None


def test_a_release_waits_for_its_initiatives_planned_features(tmp_path, gov_root):
    eng, _ = make(tmp_path, gov_root)
    a = eng.create(product_id="FARM", title="Offline crop log")
    rid = eng.create_release(product_id="FARM", title="Certified build", model="train", features=[a],
                             initiative="FARM-INI-9", candidates=[{"title": "Offline crop log"},
                                                                  {"title": "Soil sensor import", "must": True}])
    v = eng.auto_drive(a)
    assert [p.get("wait") for p in v["pending"]] == ["release_train"]
    assert eng.view(rid)["status"] == "planned"  # its planned features haven't all started
    r = eng.view(rid)["readiness"]
    assert r["ready"] == 1 and r["counts"]["not_started"] == 2 and r["must"] == {
        "total": 1, "ready": 0, "missing": ["Soil sensor import"]}
    assert r["says"].endswith("must-haves 0 of 1 ready")


def test_how_it_ships_is_settled_at_g6(tmp_path, gov_root):
    eng, _ = make(tmp_path, gov_root)
    a = eng.create(product_id="FARM", title="Offline crop log")
    eng.auto_drive(a)
    with pytest.raises(ValueError, match="reached G6"):
        eng.set_ship(a, "", by="po")


def test_g4_warns_when_a_launch_feature_wont_have_a_flag():
    rel = {"id": "FARM-REL-1", "model": "launch"}
    services = SimpleNamespace(release_for=lambda fid: rel, flags=SimpleNamespace(available=lambda pid: False))
    state = {"feature_id": "FARM-FEAT-1", "product_id": "FARM"}
    assert "has no flag service" in launch_flag_warning(services, state)
    assert launch_flag_warning(services, {**state, "flag": "farm.x"}) == ""
    services.flags = SimpleNamespace(available=lambda pid: True)
    assert launch_flag_warning(services, state) == ""
    rel["model"] = "train"
    services.flags = SimpleNamespace(available=lambda pid: False)
    assert launch_flag_warning(services, state) == ""


def test_changing_how_it_ships_through_the_api(engine):
    with TestClient(create_app(engine, auth=AuthConfig(mode="dev", secret=SECRET), web_dist=None)) as c:
        sign_in(c, "priya", teams=["acme-farm/product-owners"], orgs=["acme-farm"])
        a = engine.create(product_id="FARM", title="Offline crop log")
        b = engine.create(product_id="FARM", title="Soil sensor import")
        rid = engine.create_release(product_id="FARM", title="Spring season", model="launch", features=[b])
        r = c.put(f"/api/features/{a}/ship", json={"release_id": rid})
        assert r.status_code == 200 and r.json()["release_id"] == rid
        assert c.put(f"/api/features/{a}/ship", json={"release_id": "FARM-REL-99"}).status_code == 409
        sign_in(c, "someone", teams=["acme-farm/devs"], orgs=["acme-farm"])
        assert c.put(f"/api/features/{a}/ship", json={"release_id": ""}).status_code == 403

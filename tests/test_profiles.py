"""Workflow profiles: express changes skip discovery and UX design and let G4 approve the spec.
No profile skips architecture, G5 for risky changes, or G6. Defects are not a profile."""

import pytest

from conftest import approve
from vrixel_sdlc import profiles


def gates_asked(engine, fid):
    return [p.get("gate") for p in engine.pending(fid) if p["type"] == "gate"]


def test_products_choose_their_profiles():
    assert profiles.allowed(None, "general") == ["feature"]
    assert profiles.allowed({"profiles": ["feature", "express"]}, "general") == ["feature", "express"]
    assert "bugfix" not in profiles.PROFILES  # defects: fixed before production, incidents after
    assert profiles.allowed({"profiles": ["feature", "express"]}, "phi") == ["feature"]
    assert profiles.check({"profiles": ["feature", "express"]}, "phi") == [
        "workflow.profiles: express isn't available to phi products"]
    assert "workflow.profiles must include feature" in profiles.check({"profiles": ["express"]}, "general")
    assert any("not a profile" in p for p in profiles.check({"profiles": ["feature", "yolo"]}, "general"))
    assert any("default" in p for p in profiles.check({"profiles": ["feature"], "default": "express"}, "general"))
    assert profiles.check({"speed": 1}, "general") == ["workflow.speed is not a setting (use profiles, default)"]


def test_express_lets_g4_approve_the_spec_and_keeps_g6(engine, services):
    fid = engine.start(product_id="FARM", title="Rename the harvest button", profile="express")["feature_id"]
    [g4] = [p for p in engine.pending(fid) if p["type"] == "gate"]
    assert g4["gate"] == "g4" and g4["profile"] == "express" and g4["spec_preview"].startswith("# PRD")
    v = engine.view(fid)
    rail = {s["key"]: s["state"] for s in v["rail"]}
    assert rail["g2"] == "skipped" and rail["discovery"] == "skipped" and rail["design"] == "skipped"
    state = engine.graph.get_state(engine._config(fid)).values
    assert "Express" in services.artifacts.load(state["artifacts"]["OPP"])
    assert {d["gate"] for d in state["gate_decisions"] if d["approver"] == "profile:express"} == {"g1", "g2", "g3"}
    approve(engine, fid, "g4")
    asked = set()
    for _ in range(40):
        pend = [p for p in engine.pending(fid) if p["type"] == "gate"]
        if not pend:
            break
        asked |= {p["gate"] for p in pend}
        approve(engine, fid, pend[0]["gate"])
    assert "g6" in asked  # production always needs a person


def test_a_profile_the_product_doesnt_allow_is_refused(engine):
    with pytest.raises(ValueError, match="doesn't allow the express profile"):
        engine.start(product_id="CLN", title="Tweak a label", profile="express")
    with pytest.raises(ValueError, match="doesn't allow the express profile"):
        engine.start(product_id="SHOP", title="Tweak a label", profile="express")  # payments

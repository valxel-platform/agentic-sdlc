"""Workflow profiles: bug fixes and express changes skip discovery and UX design; express also
lets G4 approve the spec. No profile skips architecture, G5 for risky changes, or G6."""

import pytest

from conftest import approve
from vrixel_sdlc import profiles


def gates_asked(engine, fid):
    return [p.get("gate") for p in engine.pending(fid) if p["type"] == "gate"]


def test_products_choose_their_profiles():
    assert profiles.allowed(None, "general") == ["feature", "bugfix"]
    assert profiles.allowed({"profiles": ["feature", "bugfix", "express"]}, "general") == ["feature", "bugfix", "express"]
    assert profiles.allowed({"profiles": ["feature", "express"]}, "phi") == ["feature"]
    assert profiles.check({"profiles": ["feature", "express"]}, "phi") == [
        "workflow.profiles: express isn't available to phi products"]
    assert "workflow.profiles must include feature" in profiles.check({"profiles": ["bugfix"]}, "general")
    assert any("not a profile" in p for p in profiles.check({"profiles": ["feature", "yolo"]}, "general"))
    assert any("default" in p for p in profiles.check({"profiles": ["feature"], "default": "bugfix"}, "general"))
    assert profiles.check({"speed": 1}, "general") == ["workflow.speed is not a setting (use profiles, default)"]


def test_a_bugfix_starts_at_the_spec_and_skips_ux_design(engine, services):
    fid = engine.start(product_id="FARM", title="Login fails with a plus sign", profile="bugfix",
                       description="Farmers whose email has a + can't sign in")["feature_id"]
    v = engine.view(fid)
    assert v["profile"] == "bugfix" and gates_asked(engine, fid) == ["g2"]
    assert {s["key"]: s["state"] for s in v["rail"]}["discovery"] == "skipped"
    assert {s["key"]: s["state"] for s in v["rail"]}["g3"] == "skipped"
    brief = services.artifacts.load(engine.graph.get_state(engine._config(fid)).values["artifacts"]["OPP"])
    assert "can't sign in" in brief and "Bug fix" in brief
    approve(engine, fid, "g2")
    assert gates_asked(engine, fid) == ["g4"]  # straight to architecture, no design
    approve(engine, fid, "g4")
    state = engine.graph.get_state(engine._config(fid)).values
    assert state["phase_status"].get("design") == "skipped" and "UX" not in state["artifacts"]
    assert any(d["approver"] == "profile:bugfix" and d["gate"] == "g1" for d in state["gate_decisions"])
    v = engine.auto_drive(fid)
    assert v["status"] == "done"
    assert (engine.graph.get_state(engine._config(fid)).values["approvals"] or {}).get("g6")


def test_express_lets_g4_approve_the_spec_and_keeps_g6(engine):
    fid = engine.start(product_id="FARM", title="Rename the harvest button", profile="express")["feature_id"]
    [g4] = [p for p in engine.pending(fid) if p["type"] == "gate"]
    assert g4["gate"] == "g4" and g4["profile"] == "express" and g4["spec_preview"].startswith("# PRD")
    v = engine.view(fid)
    assert {s["key"]: s["state"] for s in v["rail"]}["g2"] == "skipped"
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

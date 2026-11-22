"""Separation of duties at gates (engineering review finding 6, T-133)."""

import dataclasses

import pytest
from conftest import approve

from valxel_sdlc import separation
from valxel_sdlc.config import Limits


def gates(engine, *gs):
    s = engine.services.settings
    engine.services.settings = dataclasses.replace(
        s, limits=dataclasses.replace(s.limits, separation_of_duties_gates=tuple(gs)))


def test_where_it_applies():
    lim = Limits()
    assert separation.applies(lim, "general", "g6") and separation.applies(lim, "general", "g5")
    assert not separation.applies(lim, "general", "g1")
    assert separation.applies(lim, "phi", "g1") and separation.applies(lim, "payments", "g2")
    assert not separation.applies(dataclasses.replace(lim, separation_of_duties_gates=()), "general", "g6")
    assert not separation.applies(lim, "phi", "")  # not a gate


def test_authors_are_the_starter_and_whoever_rewrote_the_phase():
    state = {"created_by": "Ann", "gate_decisions": [
        {"phase": "architecture", "decision": "handback", "approver": "bob"},
        {"phase": "spec", "decision": "handback", "approver": "cy"}]}
    assert separation.authors(state, {"phase": "architecture", "rewritten_by": "dee"}) == {"ann", "bob", "dee"}


def test_the_starter_cant_approve_where_it_applies_but_can_send_back(engine):
    gates(engine, "g1")
    fid = engine.start(product_id="FARM", title="Pest report", created_by="ann")["feature_id"]
    [g1] = engine.pending(fid)
    with pytest.raises(separation.SeparationOfDuties, match="you started this feature"):
        engine.decide(fid, g1["interrupt_id"], {"decision": "approve", "approver": "ANN"})
    assert engine.pending(fid)[0]["interrupt_id"] == g1["interrupt_id"]  # nothing happened
    v = engine.decide(fid, g1["interrupt_id"], {"decision": "approve", "approver": "po"})
    rec = [r for r in v["gate_decisions"] if r["gate"] == "g1"][-1]
    assert rec["approver"] == "po" and rec["separation"] == "checked"


def test_where_it_doesnt_apply_nothing_changes(engine):
    gates(engine)
    fid = engine.start(product_id="FARM", title="Pest report", created_by="ann")["feature_id"]
    v = approve(engine, fid, "g1", approver="ann")
    assert "separation" not in [r for r in v["gate_decisions"] if r["gate"] == "g1"][-1]


def test_a_rewritten_document_needs_another_approver(engine):
    gates(engine, "g4")
    fid = engine.start(product_id="FARM", title="Pest report", created_by="ann")["feature_id"]
    approve(engine, fid, "g1")
    approve(engine, fid, "g2")
    [g4] = [p for p in engine.pending(fid) if p["gate"] == "g4"]
    v = engine.decide(fid, g4["interrupt_id"], {"decision": "takeover", "approver": "arch"})
    [hb] = [p for p in v["pending"] if p["type"] == "handback"]
    v = engine.decide(fid, hb["interrupt_id"], {"decision": "handback", "approver": "arch",
                                                "artifact_content": "# Architecture\n\nArch's own version."})
    [again] = [p for p in v["pending"] if p.get("gate") == "g4"]
    assert again["rewritten_by"] == "arch" and set(again["allowed"]) == {"approve", "abort"}
    with pytest.raises(separation.SeparationOfDuties, match="you rewrote this document"):
        engine.decide(fid, again["interrupt_id"], {"decision": "approve", "approver": "arch"})
    v = engine.decide(fid, again["interrupt_id"], {"decision": "approve", "approver": "lead"})
    g4s = [r for r in v["gate_decisions"] if r["gate"] == "g4"]
    assert [r["decision"] for r in g4s][-1] == "approve" and g4s[-1]["rewritten_by"] == "arch"
    assert g4s[-1]["separation"] == "checked"


def test_handing_back_unchanged_isnt_writing(engine):
    gates(engine, "g4")
    fid = engine.start(product_id="FARM", title="Pest report", created_by="ann")["feature_id"]
    approve(engine, fid, "g1")
    approve(engine, fid, "g2")
    [g4] = [p for p in engine.pending(fid) if p["gate"] == "g4"]
    v = engine.decide(fid, g4["interrupt_id"], {"decision": "takeover", "approver": "arch"})
    [hb] = [p for p in v["pending"] if p["type"] == "handback"]
    v = engine.decide(fid, hb["interrupt_id"], {"decision": "handback", "approver": "arch"})
    assert not [p for p in v["pending"] if p.get("gate") == "g4"]  # the agent's draft, approved by arch


def test_the_api_says_why(engine):
    from fastapi.testclient import TestClient

    from valxel_sdlc.api.app import create_app
    from valxel_sdlc.web.auth import AuthConfig

    gates(engine, "g1")
    with TestClient(create_app(engine, auth=AuthConfig(mode="dev", secret="x" * 40), web_dist=None)) as c:
        c.post("/api/auth/dev-login", json={"login": "po-farm", "email": "po@farm.example",
                                            "teams": ["acme-farm/product-owners"]})
        fid = engine.start(product_id="FARM", title="Pest report", created_by="po-farm")["feature_id"]
        [g1] = engine.pending(fid)
        r = c.post(f"/api/features/{fid}/decisions/{g1['interrupt_id']}", json={"decision": "approve"})
    assert r.status_code == 403 and "didn't write the work" in r.text


def waiver(engine, on=True):
    s = engine.services.settings
    engine.services.settings = dataclasses.replace(
        s, limits=dataclasses.replace(s.limits, separation_of_duties_waiver=on))


def test_a_solo_founder_approves_with_a_reason_and_the_waiver_is_recorded_and_listed(engine):
    gates(engine, "g1")
    waiver(engine)
    fid = engine.start(product_id="FARM", title="Pest report", created_by="ann")["feature_id"]
    [g1] = engine.pending(fid)
    with pytest.raises(separation.SeparationOfDuties, match="say why no one else can"):
        engine.decide(fid, g1["interrupt_id"], {"decision": "approve", "approver": "ann", "waiver_reason": "ok"})
    reason = "I'm the only approver until our first hire; the advisor reviews waivers weekly"
    v = engine.decide(fid, g1["interrupt_id"], {"decision": "approve", "approver": "ann", "waiver_reason": reason})
    rec = [r for r in v["gate_decisions"] if r["gate"] == "g1"][-1]
    assert rec["separation"] == "waived" and rec["waiver_reason"] == reason
    [w] = engine.waivers()
    assert (w["feature_id"], w["gate"], w["approver"], w["reason"]) == (fid, "g1", "ann", reason)
    assert engine.waivers(since="9999-01-01") == []
    events = [e["data"] for e in engine.services.audit.outbox.events() if e["data"]["action"] == "gate.decide"]
    assert events[-1]["details"]["separation"] == "waived" and events[-1]["details"]["waiver_reason"] == reason


def test_a_reason_from_someone_else_is_not_a_waiver(engine):
    gates(engine, "g1")
    waiver(engine)
    fid = engine.start(product_id="FARM", title="Pest report", created_by="ann")["feature_id"]
    [g1] = engine.pending(fid)
    v = engine.decide(fid, g1["interrupt_id"], {"decision": "approve", "approver": "po",
                                                "waiver_reason": "not needed, but sent anyway by the client"})
    rec = [r for r in v["gate_decisions"] if r["gate"] == "g1"][-1]
    assert rec["separation"] == "checked" and "waiver_reason" not in rec
    assert engine.waivers() == []


def test_without_the_waiver_a_reason_changes_nothing():
    lim = dataclasses.replace(Limits(), separation_of_duties_gates=("g1",))
    state, item = {"created_by": "ann"}, {"type": "gate", "gate": "g1"}
    with pytest.raises(separation.SeparationOfDuties, match="Another approver decides"):
        separation.check(state, item, {"decision": "approve", "approver": "ann", "waiver_reason": "x" * 40},
                         "general", lim)

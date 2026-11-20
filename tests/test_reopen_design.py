"""Build finds the approved design wrong (graph/gates.reopen): the coder or reviewer says which
document and why, build stops at once, and people can send it back to that document. Its gate
decides again, then plan and build run again."""

import pytest

from valxel_sdlc.graph.gates import REOPEN, reopen
from valxel_sdlc.graph.phase import design_issues


def _to_escalation(engine, doc="architecture"):
    v = engine.start(product_id="FARM", title="Offline crop log", description="Record observations offline",
                     simulate_faults={"design_issue": doc})
    fid = v["feature_id"]
    for _ in range(20):
        pending = engine.pending(fid)
        esc = [p for p in pending if p["type"] == "escalation"]
        if esc:
            return fid, esc[0]
        gates = [p for p in pending if p["type"] == "gate"]
        assert gates, pending
        engine.decide(fid, gates[0]["interrupt_id"], {"decision": "approve", "approver": "maria"})
    raise AssertionError("never escalated")


def test_a_design_issue_stops_build_at_once_and_says_why(engine):
    fid, esc = _to_escalation(engine)
    assert esc["phase"] == "build"
    assert esc["reason"].startswith("The architecture needs changing (coder on ")
    [issue] = esc["design_issues"]
    assert issue["document"] == "architecture" and issue["by"] == "coder" and "404" in issue["evidence"]
    assert set(REOPEN) <= set(esc["allowed"])  # alongside retry, resolved and abort
    assert engine.view(fid)["loop_count"]["build"] == 1  # one try: no point trying again


def test_reopening_the_architecture_goes_back_through_g4_then_plan_and_build(engine):
    fid, esc = _to_escalation(engine)
    engine.decide(fid, esc["interrupt_id"], {"decision": "architecture", "approver": "lead",
                                              "feedback": "Return 202 and poll; the record comes later"})
    [g4] = [p for p in engine.pending(fid) if p["type"] == "gate"]
    assert g4["gate"] == "g4"  # the architecture was redone and its gate decides again
    v = engine.view(fid)
    reopened = [e for e in v["events"] if e["kind"] == "reopened"]
    assert reopened and reopened[0]["to"] == "architecture"
    v = engine.auto_drive(fid, approver="maria")
    assert v["status"] == "done"
    builds = [e for e in v["events"] if e["node"] == "build" and e["kind"] in ("passed", "escalated")]
    assert [e["kind"] for e in builds] == ["escalated", "passed"]


def test_the_feedback_carries_build_s_evidence_and_the_person_s_words():
    esc = {"design_issues": [{"task": "T1", "by": "reviewer", "document": "design", "section": "## States",
                              "problem": "no empty state", "evidence": "AC-3"}]}
    up = reopen({"reopened": []}, "design", esc, {"approver": "ana", "feedback": "add the empty state"})
    assert "no empty state (evidence: AC-3; reviewer on T1)" in up["feedback"]["design"]
    assert "From ana: add the empty state" in up["feedback"]["design"]
    assert up["approvals"] == {"g3": False} and up["ui"] is True and up["reopened"] == ["design"]
    assert reopen({}, "spec", esc, {"approver": "a", "feedback": ""})["approvals"] == {
        "g2": False, "g3": False, "g4": False}


def test_issues_come_from_the_coder_and_the_reviewer():
    r = {"T1": {"design_issue": {"document": "spec", "section": "", "problem": "p", "evidence": "e"}}, "T2": {}}
    v = {"T2": {"design_issue": {"document": "architecture", "section": "", "problem": "q", "evidence": "f"}}}
    assert [(i["task"], i["by"], i["document"]) for i in design_issues(r, v)] == [
        ("T1", "coder", "spec"), ("T2", "reviewer", "architecture")]


def test_reopen_is_only_offered_when_build_found_a_design_issue(engine, services):
    services.github.ci_default = "failure"  # red CI, no design issue
    fid, esc = _to_escalation(engine, doc="")
    assert "architecture" not in esc["allowed"]
    with pytest.raises(Exception, match="decision must be one of"):
        engine.decide(fid, esc["interrupt_id"], {"decision": "architecture", "approver": "lead"})

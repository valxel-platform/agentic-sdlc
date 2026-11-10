"""Agents' notes for the gate: what they assumed, changed, traded off and left open."""

from conftest import approve
from vrixel_sdlc import diary
from vrixel_sdlc.agents import formats, structured


def test_notes_are_read_from_a_document_open_questions_first():
    doc = "# HLD\n## ADRs\n- ADR-1: x\n## Agent notes\n- Assumed: v1 stays\n- open: who owns the cache?\n" \
          "- Traded off: polling over webhooks\n- Changed: kept the old route\n- something else\n## Diagram\nx"
    notes = diary.parse(doc)
    assert [n["kind"] for n in notes] == ["open", "changed", "assumed", "traded off"]
    assert notes[0]["text"] == "who owns the cache?"
    assert diary.parse("# PRD\n## Goals\n- G1") == []
    assert diary.entries(["Assumed: a", "nonsense", "Open:   "]) == [{"kind": "assumed", "text": "a"}]
    assert diary.markdown(notes).startswith("## Agent notes\n- Open: who owns the cache?")
    assert diary.summary(notes) == "1 open, 1 changed, 1 assumed, 1 traded off" and diary.summary([]) == "none"


def test_every_contract_asks_for_notes_and_the_coder_reply_carries_them():
    for phase in formats.CONTRACTS:
        assert "## Agent notes" in formats.contract(phase)
    assert "notes" in structured.CoderReply.model_json_schema()["properties"]


def test_the_gates_show_the_phase_notes_and_prs_carry_the_coders(engine, services):
    fid = engine.start(product_id="FARM", title="Farmer login", description="Add auth for farmer accounts")["feature_id"]
    for gate in ("g1", "g2", "g3"):
        approve(engine, fid, gate)
    [g4] = [p for p in engine.pending(fid) if p.get("gate") == "g4"]
    assert g4["agent_notes"][0] == {"kind": "open", "phase": "architecture",
                                    "text": "whether this change needs a feature flag beyond the one the pipeline creates"}
    approve(engine, fid, "g4")
    state = engine.graph.get_state(engine._config(fid)).values
    assert state["diaries"]["spec"][0]["kind"] == "assumed" and state["diaries"]["plan"]
    build = state["diaries"]["build"]
    assert build and all(n["task"].startswith("TASK-") and n["repo"] for n in build)
    bodies = [pr["body"] for pr in services.github.prs.values() if "body" in pr]
    assert bodies and all("## Agent notes\n- Assumed:" in b for b in bodies)
    assert any(e["kind"] == "agent_notes" for e in engine.view(fid)["events"])

"""Design reaches the build, design is skipped when nothing people see changes, and the approved
design and architecture are checked to fit together before the plan."""

from __future__ import annotations

from vrixel_sdlc import fit
from vrixel_sdlc.agents.parse import parse_screens, parse_tasks
from vrixel_sdlc.evaluators.checks import _screens

UX = "# UX spec: x\n## Screens\n- S1: Field list\n- S2: Field detail\n## States\n- Loading, empty, error\n"
HLD_UI = "## Affected repos\n- acme-farm/farm-api (provider)\n- acme-farm/farm-web (consumer)\n"
HLD_API = "## Affected repos\n- acme-farm/farm-api (provider)\n"
REPOS = [{"name": "acme-farm/farm-api", "role": "provider"}, {"name": "acme-farm/farm-web", "role": "consumer"}]


def test_web_tasks_must_build_every_screen():
    ctx = {"parent_texts": {"UX": UX}}
    assert parse_screens(UX) == {"S1": "Field list", "S2": "Field detail"}
    ok = [{"id": "TASK-1", "role": "provider"}, {"id": "TASK-2", "role": "consumer", "screens": ["S1", "S2"]}]
    assert _screens(ok, ctx) == []
    found = _screens([{"id": "TASK-2", "role": "consumer", "screens": ["S1", "S9"]}], ctx)
    assert "TASK-2 names screen S9, which the UX spec doesn't have" in found
    assert "Screen S2 (Field detail) from the UX spec isn't built by any web or mobile task" in found
    assert "names no screen" in _screens([{"id": "TASK-2", "role": "consumer"}], ctx)[0]
    assert _screens(ok, {"parent_texts": {}}) == []  # design skipped: nothing to build against


def test_the_machine_finds_a_design_and_architecture_that_dont_fit():
    assert fit.machine(UX, HLD_UI, REPOS, True) == []
    [f] = fit.machine("", HLD_UI, REPOS, False)
    assert f["to"] == "design" and "acme-farm/farm-web" in f["text"]
    [f] = fit.machine(UX, HLD_API, REPOS, True)
    assert f["to"] == "architecture" and "2 screen(s)" in f["text"]
    assert fit.machine("", HLD_API, REPOS, False) == []  # no screens, none changed


def test_the_plan_and_the_coder_get_the_approved_ux(engine):
    v = engine.start(product_id="FARM", title="Field list sorting", description="Sort the field list by moisture")
    v = engine.auto_drive(v["feature_id"])
    s = engine._state(v["feature_id"])
    assert "UX" in s["artifacts"] and s["fit"]["findings"] == []
    plan = engine.services.artifacts.load(s["artifacts"]["PLAN"])
    web = [t for t in parse_tasks(plan) if t["role"] == "consumer"]
    assert web and web[0]["screens"] == ["S1", "S2"]
    prompts = [r for r in engine.services.ledger.records(v["feature_id"]) if r.phase == "build"]
    assert prompts  # the build ran; the instruction itself is checked below
    from vrixel_sdlc.graph.phase import ux_instruction

    line = ux_instruction({"parent_texts": {"UX": UX}}, {"role": "consumer", "screens": ["S1"]})
    assert "S1 (Field list)" in line and "word for word" in line
    assert ux_instruction({"parent_texts": {"UX": UX}}, {"role": "provider"}) == ""


def test_design_is_skipped_when_nothing_people_see_changes_and_g2_can_turn_it_on(engine):
    fid = engine.create(product_id="FARM", title="Nightly export", description="Export readings to the data lake",
                        ui=False)
    g1 = next(p for p in engine.pending(fid) if p.get("gate") == "g1")
    engine.decide(fid, g1["interrupt_id"], {"decision": "approve", "approver": "po"})
    g2 = next(p for p in engine.pending(fid) if p.get("gate") == "g2")
    assert g2["ux_design"] == {"runs": False, "why": "The intake conversation found nothing people see changes"}
    # Off: no UX, no G3, and the fit check sends the design back because the stub architecture
    # changes the web app: the approver lets it carry on, with a reason.
    v = engine.auto_drive(fid, decisions={"escalation:fit": {"decision": "resolved", "feedback": "Only an API change"}})
    s = engine._state(fid)
    assert "UX" not in s["artifacts"] and s["phase_status"]["design"] == "skipped"
    assert not any(d["gate"] == "g3" for d in s["gate_decisions"])
    assert v["status"] == "done"
    assert any(e["node"] == "fit" and e["kind"] in ("accepted", "agree") for e in s["events"])

    on = engine.create(product_id="FARM", title="Field map", description="Show fields on a map", ui=False)
    v = engine.auto_drive(on, decisions={"g2": {"decision": "approve", "ui": True}})
    s = engine._state(on)
    assert s["ui"] is True and "UX" in s["artifacts"] and any(d["gate"] == "g3" for d in s["gate_decisions"])
    assert any(e["kind"] == "ux_design_on" for e in s["events"])


def test_a_mismatch_sends_the_design_back_and_the_fit_is_checked_again(engine):
    fid = engine.create(product_id="FARM", title="Nightly export", description="Export readings", ui=False)
    v = engine.auto_drive(fid, decisions={"escalation:fit": {"decision": "design", "feedback": "Show the export status"}})
    s = engine._state(fid)
    kinds = [e["kind"] for e in s["events"] if e["node"] == "fit"]
    assert kinds[:2] == ["mismatch", "back_to_design"] and kinds[-1] == "agree"
    assert "UX" in s["artifacts"] and s["feedback"]["design"].startswith("The design and the architecture don't fit")
    assert v["status"] == "done"

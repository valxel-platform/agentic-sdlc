"""Outcomes, step 2: features say which product goal they move, list the events that show it,
name the repo recording each, and the events are counted in staging before G6."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from valxel_sdlc.config import load_settings
from valxel_sdlc.engine import FeatureEngine
from valxel_sdlc.evaluators.checks import run_checks
from valxel_sdlc.graph.phase import instrumentation_findings, instrumentation_instruction
from valxel_sdlc.ops.cohorts import StubLogs
from valxel_sdlc.outcomes import instrumentation as ins
from valxel_sdlc.services import build_services

ROOT = Path(__file__).resolve().parents[1]
GOALS = [{"id": "GOAL-CLN-1", "title": "Faster check-in", "measure": "under 3 minutes"},
         {"id": "GOAL-CLN-2", "title": "Fewer walk-outs", "measure": "under 2%"}]
BRIEF = """# Opportunity brief: Kiosk check-in
## Problem
Patients wait at the desk.
## Capability
New capability: nothing covers self check-in
## Product goal
- GOAL-CLN-1: patients check themselves in instead of queueing
## Success metrics
- M1: median check-in under 3 minutes
"""
PRD = """# PRD: Kiosk check-in
## Goals
- G1: Faster check-in (measured by M1)
## Non-goals
- Payments at the kiosk
## Acceptance criteria
- AC-1 (G1): Given a booked patient, when they scan their code, then they are checked in
## Non-functional requirements
- None: nothing beyond the product's own
## Instrumentation
- checkin_completed (GOAL-CLN-1): fires when a patient finishes checking in at a kiosk; properties: wait_minutes, visit_type
## Constitution articles
- VX-5
"""


def discovery(text, **ctx):
    return [f for f in run_checks("discovery", text, ctx) if "goal" in f.lower() or "Goal" in f]


def test_the_brief_names_a_goal_the_product_has():
    assert discovery(BRIEF, product_goals=GOALS) == []
    assert discovery(BRIEF) == []  # checks without the roadmap (older callers) say nothing
    no_goal = BRIEF.replace("## Product goal\n- GOAL-CLN-1: patients check themselves in instead of queueing\n", "")
    assert "Name the product goal" in discovery(no_goal, product_goals=GOALS)[0]
    assert discovery(no_goal.replace("## Success", "## Product goal\n- None: a bug fix\n## Success"),
                     product_goals=GOALS) == []
    assert "isn't in roadmap/goals.yaml" in discovery(BRIEF.replace("GOAL-CLN-1", "GOAL-CLN-9"),
                                                       product_goals=GOALS)[0]
    assert "initiative serves GOAL-CLN-2" in discovery(BRIEF, product_goals=GOALS, initiative_goal="GOAL-CLN-2")[0]
    assert "no goals yet" in discovery(BRIEF, product_goals=[])[0]


def spec(prd, data_class="phi", brief=BRIEF):
    return [f for f in run_checks("spec", prd, {"parent_texts": {"OPP": brief}, "data_class": data_class,
                                                "articles": ["VX-5"]})
            if "event" in f.lower() or "property" in f.lower() or "Instrumentation" in f or "GOAL" in f]


def test_the_prd_lists_events_for_the_brief_goal():
    assert spec(PRD) == []
    gone = PRD.replace(PRD[PRD.index("## Instrumentation"):PRD.index("## Constitution")], "")
    assert "list the events that show it" in spec(gone)[0]
    assert spec(gone, brief=BRIEF.replace("- GOAL-CLN-1: patients", "- None: a fix; patients")) == []
    wrong = PRD.replace("(GOAL-CLN-1)", "(GOAL-CLN-2)")
    found = " ".join(spec(wrong))
    assert "doesn't name under Product goal" in found and "No event feeds GOAL-CLN-1" in found
    assert "lowercase" in spec(PRD.replace("checkin_completed", "CheckinCompleted"))[0]


@pytest.mark.parametrize("props, data_class, ok", [
    ("wait_minutes, visit_type", "phi", True),
    ("patient_email", "general", False),
    ("first_name", "general", False),
    ("clinic_name", "general", True),
    ("user_id", "general", True),
    ("user_id", "phi", False),
    ("patientId", "phi", False),
    ("account_number", "payments", False),
])
def test_event_properties_never_reach_a_person(props, data_class, ok):
    problems = ins.property_problems("e", [p.strip() for p in props.split(",")], data_class)
    assert (problems == []) is ok, problems


def test_the_hld_names_who_records_each_event():
    hld = ("# HLD: x\n## ADRs\n- ADR-1: x\n## Affected repos\n- acme-clinic/cln-intake-api (provider)\n"
           "## Contracts\n- x\n## Instrumentation\n- {line}\n")
    ctx = {"parent_texts": {"PRD": PRD}, "repos": [{"name": "acme-clinic/cln-intake-api", "role": "provider"}]}

    def found(line):
        return [f for f in run_checks("architecture", hld.format(line=line), ctx) if "Instrumentation" in f
                or "recorded by" in f]

    assert found("checkin_completed: acme-clinic/cln-intake-api") == []
    assert "has no repo" in found("other_event: acme-clinic/cln-intake-api")[0]
    assert "isn't an affected repo" in found("checkin_completed: acme-clinic/cln-web")[0]


def test_the_coder_is_told_and_held_to_it():
    ctx = {"instrumentation": [{"event": "checkin_completed", "goals": ["GOAL-CLN-1"], "when": "fires when done",
                                "properties": ["wait_minutes"], "repo": "acme-clinic/cln-intake-api"}]}
    task = {"repo": "acme-clinic/cln-intake-api", "role": "provider"}
    assert "checkin_completed" in instrumentation_instruction(ctx, task)
    assert 'ProductEvents.record("<name>")' in instrumentation_instruction(ctx, task)
    assert instrumentation_instruction(ctx, {"repo": "acme-clinic/cln-web", "role": "consumer"}) == ""
    web = {**ctx, "instrumentation": [{**ctx["instrumentation"][0], "repo": "acme-clinic/cln-web"}]}
    assert 'track("<name>")' in instrumentation_instruction(web, {"repo": "acme-clinic/cln-web", "role": "consumer"})
    said = {"T1": {**task, "tests_passed": True, "summary": "Instrumentation: checkin_completed"}}
    assert instrumentation_findings(ctx, said) == []
    said["T1"]["summary"] = "Adds the kiosk endpoint"
    assert "checkin_completed" in instrumentation_findings(ctx, said)[0]


def test_summary_lines_are_counted():
    entries = [("cln-intake-api", ins.line({"checkin_completed": 3})),
               ("cln-intake-api", '{"message": "' + ins.line({"checkin_completed": 2}).replace('"', '\\"') + '"}'),
               ("cln-intake-api", "valxel.flag_cohorts {\"flag\": \"x\"}")]
    assert ins.parse_counts(entries) == {"checkin_completed": 5}


# ---------------------------------------------------------------------- through the pipeline

def make(tmp_path):
    gov = tmp_path / "governance"
    shutil.copytree(ROOT / "examples" / "governance", gov)
    s = load_settings(mode="stub", database_url="", artifact_dir=tmp_path / "a", governance_source="local",
                      governance_root=gov)
    svc = build_services(s)
    return FeatureEngine(svc, InMemorySaver()), svc


def drive_to(eng, fid, last_gate):
    for _ in range(20):
        p = [x for x in eng.pending(fid) if x["type"] == "gate"]
        if not p or p[0]["gate"] == last_gate:
            return p[0] if p else None
        eng.decide(fid, p[0]["interrupt_id"], {"decision": "approve", "approver": "lead"})


def docs(eng, svc, fid):
    arts = eng._state(fid)["artifacts"]
    return {k: svc.artifacts.load(arts[k]) for k in ("OPP", "PRD", "HLD") if k in arts}


def test_a_feature_carries_its_goal_to_g6(tmp_path):
    eng, svc = make(tmp_path)
    svc.cohort_logs = StubLogs()
    fid = eng.create(product_id="CLN", title="Kiosk check-in")
    g6 = drive_to(eng, fid, "g6")
    d = docs(eng, svc, fid)
    assert "## Product goal\n- GOAL-CLN-1:" in d["OPP"]
    assert "kiosk_check_in_completed (GOAL-CLN-1)" in d["PRD"]
    assert "- kiosk_check_in_completed: acme-clinic/" in d["HLD"]
    check = g6["instrumentation"]
    assert check["checked"] and check["missing"] == ["kiosk_check_in_completed"]
    assert any(e["kind"] == "instrumentation" and "Never fired" in e["detail"] for e in eng.view(fid)["events"])


def test_events_that_fired_in_staging_pass(tmp_path):
    eng, svc = make(tmp_path)
    logs = StubLogs()
    logs.by_component["cln-intake-api"] = [ins.line({"kiosk_check_in_completed": 4})]
    svc.cohort_logs = logs
    fid = eng.create(product_id="CLN", title="Kiosk check-in")
    check = drive_to(eng, fid, "g6")["instrumentation"]
    assert check["missing"] == [] and check["fired"] == {"kiosk_check_in_completed": 4}
    assert ins.describe(check) == "All 1 events fired in staging"


def test_without_a_log_source_g6_says_so(tmp_path):
    eng, svc = make(tmp_path)
    fid = eng.create(product_id="CLN", title="Kiosk check-in")
    check = drive_to(eng, fid, "g6")["instrumentation"]
    assert not check["checked"] and "no log source" in check["why"]

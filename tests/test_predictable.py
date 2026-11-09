"""Predictable results: replies in fixed shapes, a checklist critic whose quotes must be real,
format contracts that match what the checks read, and caps on agent sessions."""

import json
import re
import shutil
from pathlib import Path

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from vrixel_sdlc.agents import formats, structured
from vrixel_sdlc.config import RoleConfig, load_settings
from vrixel_sdlc.engine import FeatureEngine
from vrixel_sdlc.evaluators import critic, rubrics
from vrixel_sdlc.runtime import AgentResult
from vrixel_sdlc.runtime.base import AgentTask
from vrixel_sdlc.runtime.claude_code import session_cap
from vrixel_sdlc.services import build_services

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def svc(tmp_path):
    root = tmp_path / "governance"
    shutil.copytree(ROOT / "examples" / "governance", root)
    s = load_settings(mode="stub", database_url="", artifact_dir=tmp_path / "a",
                      governance_source="local", governance_root=root)
    return build_services(s)


def test_replies_are_read_in_their_shape_or_refused_with_why():
    ok = '{"approve": true, "blocking": [], "notes": [], "score": 0.9}'
    assert structured.parse(ok, structured.ReviewReply)[0].approve is True
    assert structured.parse(f"Here you go:\n```json\n{ok}\n```", structured.ReviewReply)[0].score == 0.9
    reply, why = structured.parse('{"approve": "maybe"}', structured.ReviewReply)
    assert reply is None and "approve" in why and "score" in why
    assert structured.parse("looks good to me", structured.ReviewReply) == (None, "no JSON object in the reply")
    assert '"tests"' in structured.instruction(structured.CoderReply)


def test_the_checklist_decides_and_its_quotes_must_be_in_the_document():
    doc = "# PRD: Offline log\n## Goals\n- G1: Farmers **record** observations without a signal\n"
    items = (rubrics.Item("a", "first"), rubrics.Item("b", "second"), rubrics.Item("c", "third", must=False))
    reply = structured.CriticReply(score=0.95, items=[
        structured.ChecklistItem(id="a", met=True, evidence="farmers record observations without a signal"),
        structured.ChecklistItem(id="b", met=True, evidence="works offline for a week"),  # not in the document
        structured.ChecklistItem(id="c", met=False, note="no options")])
    passed, judged, findings = critic.judge(reply, doc, items)
    assert not passed  # a high 0-1 score doesn't save it
    assert [j["met"] for j in judged] == [True, False, False]
    assert findings == ["Checklist: second (its quote isn't in the document)"]  # 'should' items don't block


class Flaky:
    """A runtime whose first reply doesn't fit the schema."""

    name, capabilities = "generic", frozenset({"text"})

    def __init__(self, good: str):
        self.calls, self.good = [], good

    def run(self, task: AgentTask) -> AgentResult:
        self.calls.append(task)
        return AgentResult(text="I think it's fine" if len(self.calls) == 1 else self.good, tokens=10, cost_usd=0.01)


def test_the_critic_asks_once_more_when_the_reply_does_not_fit(svc, monkeypatch):
    content = "# PRD: Offline log\nFarmers record observations without a signal.\n" * 3
    items = rubrics.for_phase("spec")
    good = json.dumps({"score": 0.7, "items": [
        {"id": i.id, "met": True, "evidence": "farmers record observations"} for i in items]})
    flaky = Flaky(good)
    monkeypatch.setattr(svc, "runtime_for", lambda role: flaky)
    ctx = {"product_id": "FARM", "feature_id": "FARM-FEAT-9", "parent_texts": {}}
    v = critic.critique(svc, phase="spec", rubric_skills=(), content=content, ctx=ctx)
    assert len(flaky.calls) == 2 and "didn't fit the schema" in flaky.calls[1].layers.tail
    assert flaky.calls[0].schema == structured.schema(structured.CriticReply)
    assert v.passed and v.score == 1.0 and v.legacy_score == 0.7 and v.cost_usd == pytest.approx(0.02)

    never = Flaky("still not JSON")
    never.good = "still not JSON"
    monkeypatch.setattr(svc, "runtime_for", lambda role: never)
    v = critic.critique(svc, phase="spec", rubric_skills=(), content=content, ctx=ctx)
    assert not v.passed and "didn't fit the checklist schema" in v.notes[0]


def test_a_full_run_records_the_checklist_and_what_the_old_score_would_have_decided(svc):
    eng = FeatureEngine(svc, InMemorySaver())
    fid = eng.create(product_id="FARM", title="Offline crop log")
    v = eng.auto_drive(fid)
    assert v["status"] == "done"
    spec = next(e for e in v["events"] if e["node"] == "spec" and e["kind"] == "passed")
    assert spec["critic"]["passed"] and spec["critic"]["legacy_passed"]
    assert {i["id"] for i in spec["critic"]["items"]} == {i.id for i in rubrics.for_phase("spec")}


def test_each_format_contract_names_every_section_the_stub_documents_use(svc):
    """The stub templates pass the checks; a contract missing one of their sections would leave a
    real model guessing."""
    eng = FeatureEngine(svc, InMemorySaver())
    fid = eng.create(product_id="FARM", title="Offline crop log DynamoDB")
    v = eng.auto_drive(fid)
    kinds = {"discovery": "OPP", "spec": "PRD", "design": "UX", "architecture": "HLD", "plan": "PLAN",
             "release": "REL", "operate": "OPS"}
    extra = {"Reviewer feedback addressed", "Evaluator findings addressed", "Quality evidence", "Accepted risks",
             "New intakes", "Handoff", "Diagrams"}
    for phase, kind in kinds.items():
        doc = svc.artifacts.load(eng._state(fid)["artifacts"][kind])
        used = set(re.findall(r"^## (.+?)\s*$", doc, re.M)) - extra
        named = set(re.findall(r"^## (.+?)(?:\s{2,}\(.*)?$", formats.CONTRACTS[phase], re.M))
        assert used <= {n.strip() for n in named}, (phase, used - named)
    assert formats.contract("build") == "" and "Acceptance criteria" in formats.contract("spec")


def test_a_session_is_capped_by_its_role_and_by_what_is_left_of_the_budget():
    role = RoleConfig(runtime="claude_code", model="anthropic/claude-sonnet-5-5", max_turns=30, max_call_usd=8.0)
    task = lambda left: AgentTask(role="coder", phase="build", tags=None, layers=None, max_budget_usd=left)  # noqa: E731
    assert session_cap(task(None), role) == 8.0
    assert session_cap(task(3.25), role) == 3.25
    assert session_cap(task(-1.0), role) == 0.0


def test_a_retry_returns_only_the_sections_it_changes():
    from vrixel_sdlc.agents.parse import merge_sections

    prev = "# PRD: X\nFeature: F\n\n## Goals\n- G1\n\n## Acceptance criteria\n- AC-1: vague\n\n## Constitution articles\n- VX-5\n"
    rev = "## Acceptance criteria\n- AC-1: Given a, when b, then c\n\n## Non-goals\n- none\n"
    assert merge_sections(prev, rev) == (
        "# PRD: X\nFeature: F\n\n## Goals\n- G1\n\n## Acceptance criteria\n- AC-1: Given a, when b, then c\n\n"
        "## Constitution articles\n- VX-5\n\n## Non-goals\n- none\n")
    assert merge_sections(prev, "# PRD: Y\n\n## Goals\n- G2\n").startswith("# PRD: Y\n\n## Goals\n- G2\n\n## Acceptance")
    assert merge_sections(prev, "no sections at all") == "no sections at all"


def test_a_retry_sends_the_previous_draft_and_keeps_what_it_did_not_change(svc, monkeypatch):
    tails = []
    real = svc.runtime_for

    def spy(role):
        rt = real(role)

        class Spy:
            name, capabilities = rt.name, rt.capabilities

            def run(self, task):
                if task.role == "spec":
                    tails.append(task.layers.tail)
                return rt.run(task)
        return Spy()

    monkeypatch.setattr(svc, "runtime_for", spy)
    eng = FeatureEngine(svc, InMemorySaver())
    fid = eng.create(product_id="FARM", title="Offline crop log", simulate_faults={"spec": 1})
    v = eng.auto_drive(fid)
    assert v["status"] == "done" and v["loop_count"]["spec"] == 2
    assert "<previous_draft>" not in tails[0] and "<previous_draft>" in tails[1] and "is not testable" in tails[1]
    prd = svc.artifacts.load(eng._state(fid)["artifacts"]["PRD"])
    assert prd.count("## Acceptance criteria") == 1 and "Given a signed-in user" in prd

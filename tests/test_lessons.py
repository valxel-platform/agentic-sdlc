"""Taking over a phase that goes round in circles, and learning from people's corrections."""

import shutil
from pathlib import Path

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from conftest import approve
from vrixel_sdlc import lessons
from vrixel_sdlc.config import load_settings
from vrixel_sdlc.engine import FeatureEngine
from vrixel_sdlc.governance.errors import GovernanceError
from vrixel_sdlc.graph import InvalidDecision
from vrixel_sdlc.services import build_services

ROOT = Path(__file__).resolve().parents[1]
FARM_GOV = "acme-farm/farm-governance"


@pytest.fixture
def gov(tmp_path):
    root = tmp_path / "governance"
    shutil.copytree(ROOT / "examples" / "governance", root)
    return root


@pytest.fixture
def eng(tmp_path, gov):
    s = load_settings(mode="stub", database_url="", artifact_dir=tmp_path / "a",
                      governance_source="local", governance_root=gov)
    return FeatureEngine(build_services(s), InMemorySaver())


def at(engine, fid, gate):
    [p] = [p for p in engine.pending(fid) if p.get("gate") == gate and p["type"] == "gate"]
    return p


def revise(engine, fid, gate, feedback, **extra):
    p = at(engine, fid, gate)
    return engine.decide(fid, p["interrupt_id"], {"decision": "revise", "approver": "pm", "feedback": feedback, **extra})


def lesson_prs(engine):
    return [pr for pr in engine.services.github.prs.values() if pr["repo"] == FARM_GOV and "lessons" in pr["labels"]]


def lesson_file(engine, pr):
    [path] = [f for f in pr["files"]]
    return lessons.parse(engine.services.github.files[(FARM_GOV, pr["branch"], path)], path)


def prd(engine, fid):
    return engine.graph.get_state(engine._config(fid)).values["artifacts"]["PRD"]


def to_g2(engine, title="Offline crop log"):
    fid = engine.start(product_id="FARM", title=title)["feature_id"]
    approve(engine, fid, "g1")
    return fid


def test_a_gate_going_round_in_circles_suggests_then_requires_a_takeover(eng):
    fid = to_g2(eng)
    first = at(eng, fid, "g2")
    assert first["revisions"] == 0 and not first["stuck"] and "revise" in first["allowed"]
    revise(eng, fid, "g2", "Add the offline sync limits.", reason="missing_requirement")
    p = at(eng, fid, "g2")
    assert p["revisions"] == 1 and not p["stuck"]
    assert [h["feedback"] for h in p["history"]] == ["Add the offline sync limits."]
    revise(eng, fid, "g2", "Name who can see the log.")
    p = at(eng, fid, "g2")
    assert p["revisions"] == 2 and p["stuck"]  # takeover_after_revisions: suggest taking it over
    # The agents still have the first round's feedback, so the second round doesn't undo it.
    state = eng.graph.get_state(eng._config(fid)).values
    assert "Name who can see the log." in state["feedback"]["spec"]
    assert "Add the offline sync limits." in state["feedback"]["spec"]
    revise(eng, fid, "g2", "Still missing the limits.")
    p = at(eng, fid, "g2")
    assert p["revisions"] == 3 and "revise" not in p["allowed"]  # max_gate_revisions: take over or stop
    assert set(p["allowed"]) == {"approve", "takeover", "abort"}
    with pytest.raises(InvalidDecision):
        revise(eng, fid, "g2", "Once more")


def test_taking_over_keeps_the_persons_version_and_proposes_a_lesson(eng):
    fid = to_g2(eng)
    p = at(eng, fid, "g2")
    v = eng.decide(fid, p["interrupt_id"], {"decision": "takeover", "approver": "pm", "reason": "missing_requirement"})
    [hb] = [x for x in v["pending"] if x["type"] == "handback"]
    draft = hb["content"]
    assert draft.startswith("#") and hb["artifact"]["kind"] == "PRD"  # the agents' draft, to edit
    mine = draft.rstrip() + "\n\n## Data retention\nObservations are kept for 7 years, then deleted.\n"
    v = eng.decide(fid, hb["interrupt_id"], {"decision": "handback", "approver": "pm", "artifact_content": mine,
                                             "feedback": "Specs must say how long data is kept."})
    assert prd(eng, fid)["version"] == 2
    assert eng.services.artifacts.load(prd(eng, fid)).endswith("then deleted.\n")
    kinds = [e["kind"] for e in v["events"]]
    assert "taken_over" in kinds and "lesson_proposed" in kinds
    [pr] = lesson_prs(eng)
    lesson = lesson_file(eng, pr)
    assert lesson.id == "L-001" and lesson.phase == "spec"
    assert "Data retention" in lesson.text and "how long data is kept" in lesson.text
    assert lesson.sources == (f"{fid} G2 takeover by pm (missing_requirement)",)
    # Design and architecture start from the person's version.
    assert [x["gate"] for x in v["pending"]] == ["g3", "g4"] or sorted(x["gate"] for x in v["pending"]) == ["g3", "g4"]


def test_handing_back_unchanged_learns_nothing_from_a_diff(eng):
    fid = to_g2(eng)
    p = at(eng, fid, "g2")
    v = eng.decide(fid, p["interrupt_id"], {"decision": "takeover", "approver": "pm"})
    [hb] = [x for x in v["pending"] if x["type"] == "handback"]
    v = eng.decide(fid, hb["interrupt_id"], {"decision": "handback", "approver": "pm", "artifact_content": hb["content"]})
    assert prd(eng, fid)["version"] == 1 and not lesson_prs(eng)


def test_remember_this_proposes_the_persons_own_lesson(eng):
    fid = to_g2(eng)
    v = revise(eng, fid, "g2", "Add acceptance criteria for a lost signal.", reason="missing_requirement",
               remember=True, lesson="Every spec has acceptance criteria for losing the network mid-task.")
    assert any(e["kind"] == "lesson_proposed" for e in v["events"])
    [pr] = lesson_prs(eng)
    lesson = lesson_file(eng, pr)
    assert lesson.text == "Every spec has acceptance criteria for losing the network mid-task." and lesson.check
    over = lessons.overview(eng.services, "FARM")
    assert [p["id"] for p in over["proposed"]] == ["L-001"] and over["approved"] == []
    assert over["proposed"][0]["pr"] == pr["url"] and over["corrections"] == 1


def test_a_plain_revise_is_recorded_but_proposes_nothing(eng):
    fid = to_g2(eng)
    revise(eng, fid, "g2", "Tighten the goals.", reason="quality")
    assert not lesson_prs(eng)
    assert [c["kind"] for c in eng.services.lessons.corrections("FARM")] == ["revise"]


def test_the_same_correction_on_several_features_proposes_a_lesson(eng):
    for n in range(lessons.REPEAT_FEATURES):
        fid = to_g2(eng, f"Feature {n}")
        revise(eng, fid, "g2", f"Say what happens offline ({n}).", reason="missing_requirement")
        assert len(lesson_prs(eng)) == (1 if n == lessons.REPEAT_FEATURES - 1 else 0)
    [pr] = lesson_prs(eng)
    lesson = lesson_file(eng, pr)
    assert lesson.phase == "spec" and "something required was missing" in lesson.text
    assert len(lesson.sources) == lessons.REPEAT_FEATURES
    fid = to_g2(eng, "One more")  # proposed once, not again on every later feature
    revise(eng, fid, "g2", "Say what happens offline.", reason="missing_requirement")
    assert len(lesson_prs(eng)) == 1


class Spy:
    def __init__(self, inner):
        self.inner, self.tasks = inner, []
        self.name, self.capabilities = inner.name, inner.capabilities

    def run(self, task):
        self.tasks.append(task)
        return self.inner.run(task)


def test_approved_lessons_reach_the_agents_and_the_critics_checklist(eng, gov):
    (gov / "acme-farm" / "farm-governance" / "lessons").mkdir()
    (gov / "acme-farm" / "farm-governance" / "lessons" / "L-001.yaml").write_text(lessons.render(lessons.Lesson(
        id="L-001", phase="spec", text="Every spec says how long data is kept.", check=True, sources=("FARM-FEAT-1",))))
    (gov / "acme-farm" / "farm-governance" / "lessons" / "L-002.yaml").write_text(lessons.render(lessons.Lesson(
        id="L-002", phase="architecture", text="Prefer the existing queue over a new one.")))
    svc = eng.services
    spies: dict[str, Spy] = {}
    real = svc.runtime_for

    def spy_for(role):
        return spies.setdefault(role, Spy(real(role)))

    svc.runtime_for = spy_for
    fid = to_g2(eng)
    spec_tail = next(t.layers.tail for s in spies.values() for t in s.tasks if t.phase == "spec" and t.role != "critic")
    assert "L-001: Every spec says how long data is kept." in spec_tail and "L-002" not in spec_tail
    critic_tail = next(t.layers.tail for t in spies["critic"].tasks if t.phase == "spec")
    assert "lesson-L-001 (must): Every spec says how long data is kept." in critic_tail
    v = eng.view(fid)
    passed = [e for e in v["events"] if e["node"] == "spec" and e["kind"] == "passed"][-1]
    assert "lesson-L-001" in [i["id"] for i in passed["critic"]["items"]]
    over = lessons.overview(svc, "FARM")
    assert [x["id"] for x in over["approved"]] == ["L-001", "L-002"]


def test_a_merged_proposal_shows_as_approved(eng, gov):
    fid = to_g2(eng)
    revise(eng, fid, "g2", "x", remember=True, lesson="Specs list the roles that can read each record.")
    [pr] = lesson_prs(eng)
    path = pr["files"][0]
    target = gov / "acme-farm" / "farm-governance" / path
    target.parent.mkdir(exist_ok=True)
    target.write_text(eng.services.github.files[(FARM_GOV, pr["branch"], path)])  # people merged it
    over = lessons.overview(eng.services, "FARM")
    assert over["proposed"] == [] and over["approved"][0]["pr"] == pr["url"]
    fid2 = to_g2(eng, "Next feature")  # new ids continue after the merged one
    revise(eng, fid2, "g2", "y", remember=True, lesson="Specs name the offline limits.")
    assert lesson_file(eng, lesson_prs(eng)[-1]).id == "L-002"


def test_an_escalation_finished_by_a_person_is_kept_and_learned_from(eng):
    fid = eng.start(product_id="FARM", title="Rain gauge", simulate_faults={"spec": 99})["feature_id"]
    v = approve(eng, fid, "g1")
    [esc] = v["pending"]
    assert esc["type"] == "escalation" and esc["content"]  # the agents' last draft, to finish
    mine = "# PRD: Rain gauge\n\n## Goals\n- G1: Farmers see rainfall per field.\n\n## Acceptance criteria\n- AC1: ...\n"
    v = eng.decide(fid, esc["interrupt_id"], {"decision": "resolved", "approver": "pm", "artifact_content": mine,
                                              "feedback": "Goals must be per field."})
    assert eng.services.artifacts.load(prd(eng, fid)) == mine
    assert any(e["kind"] == "lesson_proposed" for e in v["events"])
    [c] = eng.services.lessons.corrections("FARM")
    assert c["kind"] == "resolved" and c["changes"]["any"] and "diff" not in c["changes"]


def test_a_broken_lesson_file_stops_the_snapshot_with_where(eng, gov):
    (gov / "acme-farm" / "farm-governance" / "lessons").mkdir()
    (gov / "acme-farm" / "farm-governance" / "lessons" / "L-001.yaml").write_text("id: L-001\nphase: spec\n")
    with pytest.raises(GovernanceError, match="lessons/L-001.yaml: a lesson needs id, phase and text"):
        eng.services.snapshot("FARM")


def test_changes_lists_sections_people_touched():
    before = "# PRD\n## Goals\n- a\n## Scope\n- b\n"
    after = "# PRD\n## Goals\n- a, offline\n## Retention\n- 7 years\n"
    ch = lessons.changes(before, after)
    assert ch["added"] == ["Retention"] and ch["removed"] == ["Scope"] and ch["changed"] == ["Goals"] and ch["any"]
    assert not lessons.changes(before, before)["any"]


def test_the_web_api_takes_remember_and_lists_lessons(eng):
    from fastapi.testclient import TestClient

    from test_api import SECRET, sign_in, wait_idle
    from vrixel_sdlc.api.app import create_app
    from vrixel_sdlc.web.auth import AuthConfig

    with TestClient(create_app(eng, auth=AuthConfig(mode="dev", secret=SECRET), web_dist=None)) as client:
        teams = ["acme-farm/product-owners"]
        sign_in(client, "ana", teams=teams, orgs=["acme-farm"])
        fid = client.post("/api/features", json={"product_id": "FARM", "title": "Soil test"}).json()["feature_id"]
        wait_idle(eng, fid)
        g1 = at(eng, fid, "g1")
        assert client.post(f"/api/features/{fid}/decisions/{g1['interrupt_id']}",
                           json={"decision": "approve"}).status_code == 202
        wait_idle(eng, fid)
        g2 = at(eng, fid, "g2")
        r = client.post(f"/api/features/{fid}/decisions/{g2['interrupt_id']}",
                        json={"decision": "revise", "feedback": "Add units.", "reason": "missing_requirement",
                              "lesson": "Every measurement in a spec names its unit."})
        assert r.status_code == 202
        wait_idle(eng, fid)
        over = client.get("/api/products/FARM/lessons").json()
        assert [p["text"] for p in over["proposed"]] == ["Every measurement in a spec names its unit."]
        assert client.get("/api/products/CLN/lessons").status_code == 403

"""Research before a new product or initiative: a note whose claims carry a source and a
confidence, cited by the brief or the initiative, shown at their gates, recorded in Git, and its
assumptions listed again at the initiative's review."""

from __future__ import annotations

import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from vrixel_sdlc import research as rs
from vrixel_sdlc.config import load_settings
from vrixel_sdlc.engine import FeatureEngine
from vrixel_sdlc.inception import Vision
from vrixel_sdlc.inception import run as inception_run
from vrixel_sdlc.runtime.base import AgentTask
from vrixel_sdlc.runtime.claude_code import session_options
from vrixel_sdlc.services import build_services

ROOT = Path(__file__).resolve().parents[1]

NOTE = """# Research: Walk-in intake
## Who has the problem
Front desks at small clinics, every morning.
## How they solve it today
- Paper sign-in sheets: patients write their name; free (S1)
- Phreesia: tablet check-in; price not public (S2)
## Market signals
- Clinics report front-desk turnover (S1)
## Evidence
- E1: Paper sheets are the most common walk-in check-in (confidence: medium; source: S1)
- E2: Tablet check-in products target larger practices (confidence: medium; source: S2)
- E3: The clinic's own staff asked for this (confidence: high; source: the request)
## Assumptions
- A1: We believe front desks will register walk-ins in the app; test: 80% of walk-ins registered in the app by week 4
## Open questions
- How long registration takes today
## Sources
- S1: [Clinic operations survey](https://example.org/survey)
- S2: [Phreesia check-in](https://example.com/phreesia)
"""


# --------------------------------------------------------------------------- the note

def test_a_good_note_passes_and_its_parts_parse():
    assert rs.check(NOTE, depth="quick", web=True) == []
    assert [e["id"] for e in rs.evidence(NOTE)] == ["E1", "E2", "E3"]
    assert rs.evidence(NOTE)[2] | {} == {**rs.evidence(NOTE)[2], "confidence": "high", "source": "the request"}
    [a] = rs.assumptions(NOTE)
    assert a["id"] == "A1" and a["test"].startswith("80% of walk-ins")
    assert rs.sources(NOTE)["S2"]["url"] == "https://example.com/phreesia"


def test_the_checks_catch_unsupported_claims():
    bad = NOTE.replace("(confidence: medium; source: S2)", "(confidence: high; source: S9)") \
              .replace("(confidence: high; source: the request)", "(confidence: high; source: unsourced)")
    found = " ".join(rs.check(bad, depth="quick", web=True))
    assert "S9 isn't under Sources" in found
    assert "E3: an unsourced claim can only be low confidence" in found
    # Without a search, a web source can't be cited at all.
    assert any("nothing was searched" in f for f in rs.check(NOTE, depth="own", web=False))
    # A thorough search needs more sources than two.
    assert any("at least 5 sources" in f for f in rs.check(NOTE, depth="thorough", web=True))
    no_test = NOTE.replace("; test: 80% of walk-ins registered in the app by week 4", "")
    assert any("isn't in the form '- A1: We believe" in f for f in rs.check(no_test, depth="quick", web=True))


def test_a_document_must_cite_the_note():
    doc = ("## Evidence\n- Paper sheets are common (E1)\n- Everyone hates paper\n- Tablets are pricey (E7)\n"
           "## Assumptions\n- A1: We believe X; test: Y\n")
    found = rs.citations(doc, NOTE)
    assert "Evidence: 'Everyone hates paper' cites no research" in found[0]
    assert "Evidence: E7 isn't in the research note" in found
    assert any("list what the plan bets on" in f for f in rs.citations("## Evidence\n- a (E1)\n", NOTE))


def test_the_stub_note_passes_its_own_checks():
    note = rs.stub_note("Aqua Watch", "Farms lose crops. More words here.", "quick")
    assert rs.check(note, depth="quick", web=False) == []


def test_only_runtimes_that_can_search_are_asked_to():
    def svc(mode, runtime, provider="anthropic"):
        return SimpleNamespace(settings=SimpleNamespace(mode=mode, role=lambda r: SimpleNamespace(provider=provider)),
                               runtime_for=lambda r: SimpleNamespace(name=runtime))

    assert rs.can_search(svc("live", "claude_code"), "strategist")
    assert rs.can_search(svc("live", "generic"), "strategist")
    assert not rs.can_search(svc("live", "generic", provider="openai"), "strategist")
    assert not rs.can_search(svc("live", "codex"), "strategist")
    assert not rs.can_search(svc("stub", "claude_code"), "strategist")


def test_claude_code_may_search_only_in_a_research_session(services):
    from vrixel_sdlc.gateway import CallTags, PromptLayers

    role = services.settings.role("strategist")
    tags = CallTags(product_id="X", feature_id="X-1", phase="research", role="strategist", data_class="general",
                    workspace="w", prompt_version="research@1")
    task = AgentTask(role="strategist", phase="research", tags=tags,
                     layers=PromptLayers(tools="", skills="", constitution="", product_context="", feature_context="",
                                         tail="t"), web_searches=5)
    assert session_options(task, role, "anthropic/claude", cwd=None, plugin_dirs=[])["allowed_tools"] == \
        ["WebSearch", "WebFetch"]
    task.web_searches = 0
    assert "allowed_tools" not in session_options(task, role, "anthropic/claude", cwd=None, plugin_dirs=[])


def test_the_claude_api_gets_the_web_search_tool_and_bills_each_search():
    from vrixel_sdlc.gateway.prompt import PromptLayers
    from vrixel_sdlc.gateway.providers import AnthropicProvider, ModelRequest

    sent = {}

    class Client:
        class messages:  # noqa: N801
            @staticmethod
            def create(**kw):
                sent.update(kw)
                usage = SimpleNamespace(input_tokens=10, output_tokens=5, cache_creation_input_tokens=0,
                                        cache_read_input_tokens=0, server_tool_use=SimpleNamespace(web_search_requests=3))
                return SimpleNamespace(content=[SimpleNamespace(text="note")], usage=usage, id="r1")

    layers = PromptLayers(tools="", skills="", constitution="", product_context="", feature_context="", tail="t")
    resp = AnthropicProvider(Client()).complete(ModelRequest(model="anthropic/claude", layers=layers, workspace="w",
                                                             web_searches=4))
    assert sent["tools"] == [{"type": "web_search_20250305", "name": "web_search", "max_uses": 4}]
    assert resp.web_searches == 3
    sent.clear()
    AnthropicProvider(Client()).complete(ModelRequest(model="anthropic/claude", layers=layers, workspace="w"))
    assert "tools" not in sent


def test_attachments_stay_out_of_a_search_for_phi(services, monkeypatch):
    seen = []

    class Runtime:
        name = "claude_code"

        def run(self, task):
            seen.append(task)
            return SimpleNamespace(text=NOTE, cost_usd=0.0)

    monkeypatch.setattr(rs, "can_search", lambda s, r: True)
    monkeypatch.setattr(services, "runtime_for", lambda role, variant="": Runtime())
    v = Vision(name="Walk-in intake", org="acme", vision="Clinics lose walk-ins at the desk.",
               attachments_text="Files: patient list with names")
    r = inception_run.research(services, v)
    assert r.status == "passed" and r.web and r.attachments_withheld
    assert "patient list" not in seen[0].layers.tail and seen[0].web_searches == rs.SEARCHES["quick"]
    assert "attachments weren't sent" in rs.summary(r.as_dict())
    # Their own research: no search, and the attachments are read.
    v.research_depth = "own"
    monkeypatch.setattr(rs, "can_search", lambda s, r: False)
    r = inception_run.research(services, v)
    assert not r.web and "patient list" in seen[-1].layers.tail and seen[-1].web_searches == 0


# --------------------------------------------------------------------------- inception

def test_inception_researches_first_and_the_brief_cites_it(services):
    v = Vision(name="Aqua Watch", org="acme-water", vision="Small farms lose crops because nobody sees soil "
               "moisture in time.", goals=["Fewer dry fields (measure: under 2 a month per farm)"])
    note = inception_run.research(services, v)
    assert note.status == "passed" and not note.web  # stub mode: no search
    b = inception_run.draft(services, v, "brief", research=note.text)
    assert b.status == "passed"
    assert "## Evidence" in b.text and "(E1)" in b.text and "- A1: We believe" in b.text
    # The checks hold the brief to it.
    ctx = inception_run.context_for(services, v, "brief", research=note.text)
    from vrixel_sdlc.inception import checks

    broken = b.text.replace("(E1)", "(E9)")
    assert "Evidence: E9 isn't in the research note" in checks.run("brief", broken, ctx)


def test_the_inception_thread_shows_the_research_at_the_brief_gate(tmp_path):
    s = load_settings(mode="stub", database_url="", artifact_dir=tmp_path / "a")
    eng = FeatureEngine(build_services(s), InMemorySaver())
    v = Vision(name="Aqua Watch", org="acme-water", vision="Small farms lose crops because nobody sees soil "
               "moisture in time.", research_depth="thorough")
    tid = eng.create_inception(v, created_by="sunil")
    p = next(x for x in eng.pending(tid) if x.get("gate") == "brief")
    assert "# The research behind it" in p["preview"] and "## Evidence" in p["preview"]
    assert p["research"].startswith("no web search available here")
    view = eng.view(tid)
    assert view["research"].startswith("# Research: Aqua Watch") and view["reports"]["research"]["depth"] == "thorough"


# --------------------------------------------------------------------------- initiatives

@pytest.fixture
def gov_root(tmp_path):
    root = tmp_path / "governance"
    shutil.copytree(ROOT / "examples" / "governance", root)
    return root


def make(tmp_path, gov_root):
    s = load_settings(mode="stub", database_url="", artifact_dir=tmp_path / "a",
                      governance_source="local", governance_root=gov_root)
    svc = build_services(s)
    return FeatureEngine(svc, InMemorySaver()), svc


def test_an_initiative_cites_its_research_which_is_recorded_beside_it(tmp_path, gov_root):
    from vrixel_sdlc.roadmap.record import parse_initiative

    eng, svc = make(tmp_path, gov_root)
    iid = eng.create_initiative(product_id="CLN", title="Walk-in intake", created_by="maria", research="own",
                                request="Register walk-in patients at the front desk; Show the waiting queue")
    g0 = next(p for p in eng.pending(iid) if p.get("gate") == "g0")
    assert g0["findings"] == [] and "# The research behind it" in g0["preview"]
    assert g0["research"] == "from your own research"
    assert "- A1: We believe" in eng.view(iid)["content"]
    eng.decide(iid, g0["interrupt_id"], {"decision": "approve", "approver": "maria-po"})
    pr = next(p for p in svc.github.prs.values() if p["branch"] == f"vrixel/{iid}")
    files = {path: text for (repo, branch, path), text in svc.github.files.items() if branch == pr["branch"]}
    assert files[f"roadmap/research/{iid}.md"].startswith("# Research: Walk-in intake")
    record = parse_initiative(files[f"roadmap/initiatives/{iid}.md"])
    assert "- A1: We believe" in record["assumptions"]


def test_the_initiative_review_lists_what_the_plan_assumed():
    from vrixel_sdlc.outcomes import review

    ev = {"goal_id": "", "goal": {}, "status": "no_data", "features": [], "readouts": {}, "spent_usd": 0.0,
          "budget_usd": 100.0, "retire": [], "in_progress": [],
          "assumptions": [{"id": "A1", "belief": "We believe X", "test": "Y by week 4"}]}
    text = review.preview({"id": "CLN-INI-1"}, ev, "its review date")
    assert "## What the plan assumed" in text and "- A1: We believe X (test: Y by week 4)" in text

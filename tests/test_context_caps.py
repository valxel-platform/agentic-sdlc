"""Layer caps and per-task context (the context design's C3 and C4).

Each prompt layer has a token cap; a layer over it is cut at a heading with a note naming what was
left out. A build task's coder and reviewer get the feature's documents cut down to that task, and
the full documents sit in the session's home folder."""

import json
from types import SimpleNamespace

from valxel_sdlc.agents.context import build_layers, task_texts
from valxel_sdlc.context import fit_problems
from valxel_sdlc.gateway import cap_tokens, fit
from valxel_sdlc.graph.phase import feature_folder, for_task


def doc(*sections: tuple[str, int]) -> str:
    return "# Title\n\n" + "\n".join(f"## {name}\n" + ("word " * size) + "\n" for name, size in sections)


def test_a_layer_under_its_cap_is_untouched_and_one_over_is_cut_at_a_heading():
    text = doc(("Problem", 50), ("Design", 50))
    assert fit(text, 10_000, "X") == (text, "")
    assert fit(text, 0, "X") == (text, "")  # no cap

    big = doc(("Problem", 200), ("Design", 4000), ("Risks", 4000), ("Rollout", 100))
    out, note = fit(big, 1000, "The feature context")
    assert cap_tokens(out) <= 1000
    assert "## Problem" in out and "## Design" not in out
    assert note == "[The feature context was cut to fit its 1000-token cap. Left out: Design, Risks, Rollout.]"
    assert out.rstrip().endswith(note)
    assert fit(big, 1000, "The feature context") == (out, note)  # same input, same prompt: it caches


def test_one_section_bigger_than_the_cap_keeps_its_start():
    out, note = fit(doc(("Everything", 20_000)), 500, "L4")
    assert cap_tokens(out) <= 500 and out.startswith("# Title")
    out, note = fit("## Huge\n" + "word " * 20_000, 500, "L4")
    assert "Left out: the rest of Huge." in note and out.startswith("## Huge")


def test_the_size_check_is_cautious():
    text = "x" * 3500
    assert cap_tokens(text) == 1000 and cap_tokens("") == 0  # 3.5 characters a token, not 4


def test_build_layers_caps_each_layer_and_records_what_it_cut(settings):
    from valxel_sdlc.constitution import Constitution

    con = Constitution(text=doc(*[(f"VX-{i}", 400) for i in range(1, 30)]), version="v", article_ids=("VX-1",))
    from valxel_sdlc.services import build_services

    product = build_services(settings).snapshot("FARM").product
    layers = build_layers(role="coder", skills=(), constitution=con, product=product,
                          parent_texts={"PLAN": doc(("Tasks", 30_000))}, tail="do it",
                          caps={"L3": 2000, "L5": 3000})
    assert set(layers.meta["trimmed"]) == {"L3", "L5"}
    assert "The constitution was cut to fit its 2000-token cap. Left out: VX-" in layers.constitution
    assert cap_tokens(layers.feature_context) <= 3000
    plain = build_layers(role="coder", skills=(), constitution=con, product=product, parent_texts={}, tail="")
    assert plain.meta == {} and plain.constitution == con.text  # no caps given: nothing cut


PLAN = """# Plan: Offline crop log
## Tasks
```json
{"tasks": [
  {"id": "TASK-1", "repo": "acme-farm/farm-api", "role": "provider", "acs": ["AC-1"], "adrs": ["ADR-F1-1"],
   "files": ["src/api/log.py"]},
  {"id": "TASK-2", "repo": "acme-farm/farm-web", "role": "consumer", "acs": ["AC-2"], "adrs": ["ADR-F1-2"],
   "files": ["src/ui/log.tsx"], "screens": ["S1"]}
]}
```
## Traceability
- AC-1 -> TASK-1
- AC-2 -> TASK-2
- NFR-1 -> TASK-1
## Test cases
- TC-1 (AC-1, happy): saves a log -> TASK-1 tests/test_log.py::test_ac1_happy
- TC-2 (AC-2, happy): shows the form -> TASK-2 tests/log.test.tsx::test_ac2_happy
"""

HLD = """# HLD: Offline crop log
## ADRs
- ADR-F1-1: farm-api stores logs in Postgres
- ADR-F1-2: farm-web queues entries offline
- ADR-F1-3: acme-farm/farm-api exposes a bulk endpoint
## Affected repos
- acme-farm/farm-api (provider)
- acme-farm/farm-web (consumer)
## Endpoints
- acme-farm/farm-api: project; calls none
- acme-farm/farm-web: external; calls farm-api
## Data
- crop_logs: new; id, farm_id, notes
## Threats
- Boundary: acme-farm/farm-web: form input
- Spoofing: another farm's user; prevented: farm_id from the token; detected: audit
"""

UX = """# UX: Offline crop log
## Screens
- S1: Log form (web: acme-farm/farm-web)
- S2: History (web: acme-farm/farm-web)
## States
- S1: empty, saving, saved, offline
- S2: empty, list
## Copy
- Save: "Save log"
"""


def test_a_provider_task_sees_its_own_plan_entry_decisions_and_lines():
    texts = task_texts({"PLAN": PLAN, "HLD": HLD, "UX": UX}, {"id": "TASK-1", "repo": "acme-farm/farm-api",
                       "role": "provider", "acs": ["AC-1"], "adrs": ["ADR-F1-1"]}, "~/feature/")
    plan = texts["PLAN"]
    assert '"TASK-1"' in plan and '"TASK-2"' not in plan and "Other tasks in this feature: TASK-2 (acme-farm/farm-web)." in plan
    assert "AC-1 -> TASK-1" in plan and "AC-2 -> TASK-2" not in plan and "TC-2" not in plan
    assert "left out: 1 Traceability line about other tasks; 1 Test cases line about other tasks" in plan
    hld = texts["HLD"]
    assert "ADR-F1-1" in hld and "ADR-F1-2" not in hld and "ADR-F1-3" not in hld  # only what the planner tagged
    assert "acme-farm/farm-api: project" in hld
    assert "acme-farm/farm-web: external; calls farm-api" in hld  # who calls it matters to the provider
    assert "acme-farm/farm-web (consumer)" not in hld
    assert "## Data" in hld and "Spoofing" in hld  # a provider's data; a threat to every task
    assert "Boundary: acme-farm/farm-web" not in hld and "The full HLD is in ~/feature/." in hld
    assert texts["UX"].startswith("[The UX spec is left out: TASK-1 builds no screens. Read it from ~/feature/")


def test_a_web_task_sees_its_screens_and_no_data_section():
    texts = task_texts({"HLD": HLD, "UX": UX, "BRIEF": "kept whole"},
                       {"id": "TASK-2", "repo": "acme-farm/farm-web", "role": "consumer", "acs": ["AC-2"],
                        "screens": ["S1"]})
    assert "S1: Log form" in texts["UX"] and "S2" not in texts["UX"] and '"Save log"' in texts["UX"]
    hld = texts["HLD"]
    assert "## Data" not in hld and "Data" in hld.rsplit("[", 1)[-1]  # left out, and said so
    # No ADR tags (an older plan): the ADRs naming its repo, by full or short name.
    assert "ADR-F1-2" in hld and "ADR-F1-1" not in hld and "ADR-F1-3" not in hld
    assert texts["BRIEF"] == "kept whole" and "The full HLD is in the feature." in hld


def test_the_stub_planner_tags_tasks_with_the_adrs_naming_their_repo():
    from valxel_sdlc.agents.templates import _adrs_naming

    assert _adrs_naming(HLD, "acme-farm/farm-api") == ["ADR-F1-1", "ADR-F1-3"]
    assert _adrs_naming(HLD, "acme-farm/farm-web") == ["ADR-F1-2"]
    assert _adrs_naming("", "acme-farm/farm-web") == []


def test_the_full_documents_go_to_the_sessions_home_not_the_repo(tmp_path):
    box = SimpleNamespace(home=str(tmp_path / "home"), cwd=str(tmp_path / "work"))
    assert feature_folder(box, {"PLAN": PLAN, "HLD": HLD}) == "~/feature/"
    assert (tmp_path / "home" / "feature" / "HLD.md").read_text() == HLD
    assert not (tmp_path / "work").exists()
    assert feature_folder(SimpleNamespace(home=""), {"PLAN": PLAN}) == ""
    ctx = {"parent_texts": {"PLAN": PLAN}, "feature_id": "F1"}
    mine = for_task(ctx, {"id": "TASK-2", "acs": ["AC-2"]})
    assert '"TASK-1"' not in mine["parent_texts"]["PLAN"] and ctx["parent_texts"]["PLAN"] == PLAN


def test_caps_come_from_config_and_a_window_too_small_is_refused(settings):
    assert settings.context_caps == {"L2": 15000, "L3": 8000, "L4": 10000, "L5": 25000, "reserve": 64000}
    assert fit_problems(settings, settings.roles) == []  # 1M windows hold 122k
    small = SimpleNamespace(context_caps=settings.context_caps, role=settings.role,
                            context_window=lambda m: 100_000 if "haiku" in m else settings.context_window(m))
    [problem] = fit_problems(small, ["router", "coder"])
    assert problem.startswith("router: anthropic/claude-haiku-5-5's 100,000-token window can't hold")


def test_a_stub_feature_builds_with_per_task_context(engine, monkeypatch):
    from tests.test_quality import drive_gates
    from valxel_sdlc.graph import phase

    seen, real = [], phase.call_agent

    def spy(services, **kw):
        if kw.get("phase") == "build":
            seen.append((kw["role"], kw["ctx"]["parent_texts"]))
        return real(services, **kw)

    monkeypatch.setattr(phase, "call_agent", spy)
    drive_gates(engine, engine.start(product_id="FARM", title="Offline crop log", description="x")["feature_id"])
    coders = [texts for role, texts in seen if role == "coder"]
    reviewers = [texts for role, texts in seen if role == "reviewer"]
    assert len(coders) >= 2 and reviewers
    for texts in coders + reviewers:
        plan = json.loads(texts["PLAN"].split("```json\n", 1)[1].split("\n```", 1)[0])
        assert len(plan["tasks"]) == 1  # its own task, the others named in a line

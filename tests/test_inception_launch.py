"""Inception's launch (T-100): the product gate confirms the first initiative's launch date and
must-have features; the record carries them, and the first features join the launch, which knows
which planned features haven't started."""

from __future__ import annotations

import shutil
from datetime import UTC, datetime, timedelta

import pytest
import yaml
from test_api import sign_in, wait_idle
from test_inception_gates import (  # noqa: F401
    BODY,
    COUNCIL,
    client,
    decide,
    wait_idle_status,
)
from test_roadmap import ROOT, make

from valxel_sdlc.inception.graph import launch_choice, launch_plan
from valxel_sdlc.roadmap.record import render_initiative
from valxel_sdlc.roadmap.views import product_roadmap

LATER = (datetime.now(UTC) + timedelta(days=60)).date().isoformat()
ROADMAP = """# Roadmap: Aqua Watch
## Initiatives
- INI-1: First alerts [horizon: now; budget: 900; goals: PG1; after: none]
    - Moisture alerts: texts the manager when a field runs dry [builds: api]
    - Field map: shows each field's moisture [builds: web]
- INI-2: Water use [horizon: next; budget: 600; goals: PG2; after: INI-1]
    - Usage report: water per hectare [builds: api]
"""


def test_the_first_initiative_is_the_launch():
    plan = launch_plan(ROADMAP)
    assert plan["title"] == "First alerts" and plan["must"] == plan["features"] == ["Moisture alerts", "Field map"]
    assert launch_choice(plan, {}) == {"target_date": "", "must": ["Moisture alerts", "Field map"]}
    assert launch_choice(plan, {"target_date": LATER, "must": ["Moisture alerts"]})["must"] == ["Moisture alerts"]
    with pytest.raises(ValueError, match="future"):
        launch_choice(plan, {"target_date": "2020-01-01"})
    with pytest.raises(ValueError, match="Not a feature"):
        launch_choice(plan, {"must": ["Billing"]})
    assert launch_plan("# Roadmap\n") is None


def test_the_product_gate_sets_the_launch_and_setup_records_it(client, engine, services, monkeypatch):  # noqa: F811
    monkeypatch.setenv("VALXEL_CONFIG_REPO", "valxel-platform/sdlc-config")
    sign_in(client, "lead", teams=COUNCIL)
    tid = client.post("/api/inceptions", json=BODY).json()["feature_id"]
    decide(client, engine, tid, "approve")
    v = wait_idle(engine, tid)
    [item] = [p for p in v["pending"] if p["type"] == "gate"]
    plan = item["launch"]
    assert plan and plan["must"] == plan["features"] and plan["target_date"] == ""
    first = plan["features"][0]
    decide(client, engine, tid, "approve", target_date=LATER, must=[first])
    v = wait_idle_status(engine, tid, "set_up")
    files = {p: t for (repo, _, p), t in services.github.files.items() if repo == "acme-water/aw-governance"}
    ini = files["roadmap/initiatives/AW-INI-1.md"]
    meta = yaml.safe_load(ini.split("---")[1])
    assert meta["release_model"] == "launch" and str(meta["target_date"]) == LATER
    assert f"- {first} (must):" in ini and ini.count("(must)") == 1
    later = files.get("roadmap/initiatives/AW-INI-2.md", "")
    assert "target_date" not in later and (not later or "release_model: each" in later)
    assert any(e["node"] == "product_gate" and LATER in e["detail"] for e in v["events"])


@pytest.fixture
def gov_root(tmp_path):
    root = tmp_path / "governance"
    shutil.copytree(ROOT / "examples" / "governance", root)
    d = root / "acme-farm" / "farm-governance" / "roadmap" / "initiatives"
    d.mkdir(parents=True, exist_ok=True)
    (d / "FARM-INI-1.md").write_text(render_initiative(
        {"id": "FARM-INI-1", "title": "First season", "status": "approved", "release_model": "launch",
         "target_date": LATER, "lanes": ["FARM"], "approved_by": "council"},
        body="## Outcome\n\nFarms log crops.\n\n## Candidate features\n\n- Offline crop log (must): log offline\n"
             "- Soil sensor import: read sensors\n"))
    return root


def test_its_features_join_the_launch_and_it_shows_what_hasnt_started(tmp_path, gov_root):
    eng, _ = make(tmp_path, gov_root)
    fid = eng.create(product_id="FARM", title="Offline crop log", initiative_id="FARM-INI-1")
    ship = eng.view(fid)["ship"]
    rel = eng.initiative_release("FARM-INI-1", create=False)
    assert rel and ship.get("release_id") == rel["id"] and rel["model"] == "launch"
    r = eng.release_readiness(rel["id"])
    assert r["target_date"] == LATER and r["must"]["total"] == 1
    assert [c["title"] for c in r["not_started"]] == ["Soil sensor import"]
    [ini] = [i for i in product_roadmap(eng, "FARM")["initiatives"] if i["id"] == "FARM-INI-1"]
    assert [(c["title"], c["started"], c["must"]) for c in ini["candidates"]] == [
        ("Offline crop log", True, True), ("Soil sensor import", False, False)]


def test_later_initiatives_ship_each_feature_at_g6(services):
    from valxel_sdlc.inception import Vision, setup
    from valxel_sdlc.inception.run import run

    v = Vision(name="Aqua Watch", org="acme-water", vision="Alert farm managers when fields run dry.",
               goals=["Fewer dry fields (measure: under 2 a month)"], users=["Farm managers"], budget_usd=1500)
    [b] = run(services, v)
    files = setup.initiative_files(b.text, ROADMAP, "lead", {"target_date": LATER, "must": ["Field map"]})
    first, later = (yaml.safe_load(files[f"roadmap/initiatives/AW-INI-{n}.md"].split("---")[1]) for n in (1, 2))
    assert first["release_model"] == "launch" and str(first["target_date"]) == LATER
    assert later["release_model"] == "each" and "target_date" not in later
    assert "- Field map (must):" in files["roadmap/initiatives/AW-INI-1.md"]

"""Product inception: the brief, then (once approved) the architecture and roadmap, each checked by
machine and by the critic. Stub mode writes documents that pass, so these tests run the flow and
then break the documents on purpose to see the checks catch it."""

from __future__ import annotations

import json

import pytest

from vrixel_sdlc import cli
from vrixel_sdlc.inception import Vision, checks, formats
from vrixel_sdlc.inception.run import context_for, draft, platform, run

VISION = """# Aqua Watch

Small farms lose crops because nobody sees soil moisture until it's too late. Aqua Watch alerts the
manager on their phone when a field runs dry.

## Goals
- Fewer dry-field incidents (measure: under 2 a month per farm within six months)
- Less water used (measure: 15% less water per hectare in the first season)

## Users
- Farm managers: decide where to irrigate, out in the fields on a phone

## Budget
1,500
"""


@pytest.fixture
def vision():
    return Vision.from_markdown(VISION, org="acme-water")


def test_a_vision_reads_from_markdown(vision):
    assert vision.name == "Aqua Watch" and vision.budget_usd == 1500
    assert len(vision.goals) == 2 and vision.users[0].startswith("Farm managers")
    assert "soil moisture" in vision.vision and "## " not in vision.vision


def test_the_brief_stops_for_its_gate_and_runs_as_the_strictest_class(services, vision):
    [b] = run(services, vision)
    assert b.doc == "brief" and b.status == "passed" and b.data_class == "phi"
    assert formats.product(b.text) == {"id": "AW", "org": "acme-water", "prefix": "aw-", "budget_usd": 1500.0}
    assert [g["id"] for g in formats.goals(b.text)] == ["PG1", "PG2"]


def test_with_the_approved_brief_the_architecture_and_roadmap_follow(services, vision):
    [b] = run(services, vision)
    a, r = run(services, vision, brief=b.text)
    assert a.status == r.status == "passed"
    assert a.data_class == "general"  # the approved brief's class from here on
    names = [c["name"] for c in formats.components(a.text)]
    assert {"api", "bff", "mobile", "events"} <= set(names)  # a phone app and alerts in the vision
    # The database is sized by the capacity plan: paused in dev and staging, small in the pilot.
    assert a.monthly_usd == {"dev": 5.0, "staging": 5.0, "production": 15.0}
    assert "## Capacity" in a.text and "| records (database) |" in a.text and "## Monthly cost" not in a.text
    inis = formats.initiatives(r.text)
    assert inis[0]["id"] == "INI-1" and inis[0]["horizon"] == "now" and not inis[0]["after"]
    assert sum(i["budget_usd"] for i in inis) <= 1500


def test_brief_checks(services, vision):
    [b] = run(services, vision)
    ctx = context_for(services, vision, "brief")
    assert checks.brief(b.text, ctx) == []
    bad = (b.text.replace("(measure: under 2 a month per farm within six months)", "(measure: fewer of them)")
           .replace("- general:", "- secret:").replace("- id: AW", "- id: FARM").replace("- org: acme-water", "- org: acme"))
    found = "\n".join(checks.brief(bad, ctx))
    assert "PG1: its measure has no number" in found
    assert "Data class: start the line with one of" in found
    assert "FARM is already a registered product" in found
    assert "org is acme-water" in found


def test_architecture_checks(services, vision):
    [b] = run(services, vision)
    [a] = run(services, vision, brief=b.text, through="architecture")
    ctx = context_for(services, vision, "architecture", brief=b.text)
    assert checks.architecture(a.text, ctx) == []
    bad = (a.text.replace("api (provider)", "api (database)")
           .replace("[repo: acme-water/aw-web]", "[repo: other-org/web]")
           .replace("[repo: acme-water/aw-bff]", "[repo: acme-water/bff-service]")
           .replace("- records: postgres-crossplane; database=aw", "- records: mongodb; database=aw\n- cache: redis-crossplane\n    reads: billing")
           .replace("95% of API requests answer within 300 ms at the expected peak", "fast enough")
           .replace("(alternatives: DynamoDB; ", "("))
    found = "\n".join(checks.architecture(bad, ctx))
    assert "role 'database' isn't one of" in found
    assert "repo bff-service must start with the prefix aw-" in found
    assert "records: mongodb isn't in the platform's AWS catalog" in found
    assert "'billing' uses it but isn't a component" in found
    assert "NFR-1: needs a number" in found
    assert "ADR-AW-1: say which alternatives" in found


def test_regulated_data_needs_its_audit_records_placed(services):
    v = Vision.from_markdown("# Clinic intake\n\nPatients fill their intake on a tablet.\n\n## Goals\n- Faster check-in "
                             "(measure: under 3 minutes)\n\n## Budget\n900\n", org="acme-health")
    [b] = run(services, v)
    assert formats.data_class(b.text)[0] == "phi"
    a, _ = run(services, v, brief=b.text)
    assert a.status == "passed" and a.data_class == "phi"
    ctx = context_for(services, v, "architecture", brief=b.text)
    no_audit = a.text.replace("## Audit", "## Records").replace("audit", "log").replace(
        "one place for rules and audit", "one place for rules")
    assert any("audit records go" in f for f in checks.architecture(no_audit, ctx))


def test_roadmap_checks(services, vision):
    [b] = run(services, vision)
    a, r = run(services, vision, brief=b.text)
    ctx = context_for(services, vision, "roadmap", brief=b.text, architecture=a.text)
    assert checks.roadmap(r.text, ctx) == []
    bad = (r.text.replace("after: none", "after: INI-2")
           .replace("[builds: api, bff, mobile, events]", "[builds: api]")
           .replace("goals: PG2;", "goals: PG1;")
           .replace("budget: 750", "budget: 5000"))
    found = "\n".join(checks.roadmap(bad, ctx))
    assert "INI-1: comes after INI-2, which isn't listed before it" in found
    assert "the first initiative can't wait for another" in found
    assert "thin working version" in found
    assert "mobile: no feature builds it" in found
    assert "PG2: no initiative serves it" in found
    assert "over the brief's $1,500" in found


def test_a_brief_that_cant_pass_escalates_with_what_is_wrong(services):
    v = Vision.from_markdown("# Vague idea\n\nSomething useful.\n\n## Goals\n- People like it\n", org="acme-x")
    d = draft(services, v, "brief", max_loops=2)
    assert d.status == "escalated" and d.attempts == 2
    assert any("measure has no number" in f for f in d.findings)


def test_an_existing_product_describes_what_runs_today(services):
    v = Vision.from_markdown("# Farm app\n\nWhere we take it next.\n\n## Goals\n- Less water (measure: 10% a season)\n",
                             org="acme-farm", product_id="FARM", existing=True)
    [b] = run(services, v)
    assert b.status == "passed" and formats.product(b.text)["id"] == "FARM"
    ctx = context_for(services, v, "brief")
    assert any("this is FARM; keep its id" in f for f in checks.brief(b.text.replace("- id: FARM", "- id: FRM"), ctx))
    a, r = run(services, v, brief=b.text)
    assert a.status == r.status == "passed"
    actx = context_for(services, v, "architecture", brief=b.text)
    found = checks.architecture(a.text.replace("## Current state", "## Today").replace("## Gap", "## Changes"), actx)
    assert "Missing the '## Current state' section" in found and "Missing the '## Gap' section" in found


def test_the_platform_is_read_from_its_governance_repo(services):
    p = platform(services)
    assert "VX-1" in p["constitution"] and "postgres-crossplane" in p["catalog"]["types"]


def test_the_command_line(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "vision.md").write_text(VISION)
    assert cli.main(["inception", "--vision", "vision.md", "--org", "acme-water", "--out", "out"]) == 0
    assert (tmp_path / "out" / "BRIEF.md").exists() and not (tmp_path / "out" / "PARCH.md").exists()
    assert cli.main(["inception", "--vision", "vision.md", "--org", "acme-water", "--brief", "out/BRIEF.md",
                     "--out", "out"]) == 0
    report = json.loads((tmp_path / "out" / "report.json").read_text())
    assert [d["doc"] for d in report] == ["architecture", "roadmap"]
    assert report[0]["monthly_usd"]["production"] == 15.0 and report[0]["capacity"]["tier"] == 3
    assert cli.main(["inception", "--vision", "vision.md", "--out", "out"]) == 2  # no org

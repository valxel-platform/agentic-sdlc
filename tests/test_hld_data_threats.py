"""The architecture's Data and Threats sections: what G4 sees about data risk and security risk."""

from __future__ import annotations

from vrixel_sdlc.agents.parse import parse_data, parse_threats
from vrixel_sdlc.evaluators.checks import _data, _permission_cases, _threats
from vrixel_sdlc.graph.phase import security_instruction

REPOS = [{"name": "acme/api", "role": "provider"}, {"name": "acme/bff", "role": "bff"}]
HLD = """## Endpoints
- acme/api: project
- acme/bff: external; calls api
## Data
- visits: changed; adds checked_in_at, drops legacy_status
- Migration: V3__visits.sql; one step
- Backfill: none
## Threats
- Boundary: acme/api: requests from the BFF
- Authorization: acme/api POST /visits: front-desk staff of the clinic; only their clinic's visits
- Disclosure: another clinic's staff; prevented: queries scoped to the clinic; detected: audit reads
- Tampering: anyone
"""


def test_the_sections_parse():
    d = parse_data(HLD)
    assert d["tables"] == [{"table": "visits", "change": "changed", "what": "adds checked_in_at, drops legacy_status"}]
    assert d["migration"].startswith("V3__") and d["rollback"] == ""
    t = parse_threats(HLD)
    assert t["boundaries"] == [{"repo": "acme/api", "where": "requests from the BFF"}]
    assert t["authorization"][0] == {"repo": "acme/api", "endpoint": "POST /visits",
                                     "rule": "front-desk staff of the clinic; only their clinic's visits"}
    assert t["threats"][0]["prevented"] == "queries scoped to the clinic" and t["bad"] == ["- Tampering: anyone"]


def test_data_checks():
    found = _data(HLD, REPOS, {"data_class": "phi"})
    assert any("no Rollback line" in f for f in found)
    assert any("visits drop or rename a column: expand now and contract" in f for f in found)
    assert any("phi product's tables need a Retention line" in f for f in found)
    ok = HLD.replace("one step", "expand now, contract in a later release") + "- Rollback: kept\n- Retention: 7 years\n"
    ok = ok.replace("## Threats", "- Rollback: kept\n- Retention: 7 years\n## Threats")
    assert _data(ok, REPOS, {"data_class": "phi"}) == []
    assert _data("## Endpoints\n", [{"name": "acme/web", "role": "consumer"}], {}) == []  # no data-owning repo
    assert "no Data section" in _data("## Endpoints\n", REPOS, {})[0]


def test_threat_checks_apply_to_every_product():
    found = _threats(HLD, REPOS, {"data_class": "general"})
    assert "Threats: acme/bff is reachable (external) but has no Boundary line" in found
    assert any("acme/bff serves requests but has no Authorization line" in f for f in found)
    assert any("'- Tampering: anyone' isn't" in f for f in found)
    assert "no Threats section" in _threats("## Endpoints\n- acme/api: project\n", REPOS, {})[0]


def test_authorized_components_need_a_permission_test_and_the_coder_hears_the_rules():
    tasks = [{"id": "TASK-1", "repo": "acme/api", "role": "provider"}]
    ctx = {"parent_texts": {"HLD": HLD}}
    plan = "## Test cases\n- TC-1 (AC-1, happy): x -> TASK-1 tests/test_v.py::test_ok\n"
    assert "acme/api has an Authorization line" in _permission_cases(plan, tasks, ctx)[0]
    plan += "- TC-2 (AC-1, permission): another clinic is refused -> TASK-1 tests/test_v.py::test_refused\n"
    assert _permission_cases(plan, tasks, ctx) == []
    line = security_instruction({"hld_text": HLD}, {"repo": "acme/api", "role": "provider"})
    assert "POST /visits: front-desk staff of the clinic; only their clinic's visits" in line

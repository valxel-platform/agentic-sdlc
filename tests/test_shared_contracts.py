"""The shared contracts the platform owns (standard_files/contracts, T-15): what the tool reads is
what they say, and every repo carries them as managed files."""

import json

import pytest
from jsonschema import Draft202012Validator

from valxel_sdlc import repo_standards as rs
from valxel_sdlc.ops import cohorts
from valxel_sdlc.outcomes import instrumentation


def _validator(name):
    schema = json.loads(rs.standard_file(f"contracts/{name}.schema.json"))
    Draft202012Validator.check_schema(schema)
    assert schema["x-version"] == "1.0.0"
    return Draft202012Validator(schema)


def test_the_tools_own_events_line_meets_the_contract_and_reads_back():
    v = _validator("valxel.events.v1")
    line = instrumentation.line({"checkin_completed": 12, "checkin.abandoned": 1})
    payload = json.loads(line.split(instrumentation.MARKER, 1)[1])
    assert list(v.iter_errors(payload)) == []
    assert instrumentation.parse_counts([("c", line)]) == {"checkin_completed": 12, "checkin.abandoned": 1}
    for bad in ({"Checkin": 1}, {"checkin": -1}, {"checkin": "3"}, {}):
        assert list(v.iter_errors(bad)), bad


@pytest.mark.parametrize("line", [
    '{"flag": "cln.cln-feat-3.kiosk", "on": {"requests": 40, "errors": 0}, "off": {"requests": 760, "errors": 2}}',
    '{"flag": "f", "on": {"requests": 40, "errors": 0, "slow": 3}, "off": {"requests": 7, "errors": 0, "slow": 0}}',
    ('{"flag": "f", "source": "web", "on": {"requests": 1, "errors": 0, "events": {"checkin_completed": 9}},'
     ' "off": {"requests": 2, "errors": 0, "events": {"checkin_completed": 120}}}'),
])
def test_the_cohort_lines_the_tool_documents_meet_the_contract_and_read_back(line):
    v = _validator("valxel.flag_cohorts.v1")
    rec = json.loads(line)
    assert list(v.iter_errors(rec)) == []
    got = cohorts.parse([f"{cohorts.MARKER} {line}"], [rec["flag"]])[rec["flag"]]
    assert got["lines"] == 1 and got["on"]["requests"] == rec["on"]["requests"]


def test_a_cohort_line_that_drifts_is_caught():
    v = _validator("valxel.flag_cohorts.v1")
    assert list(v.iter_errors({"flag": "f", "on": {"requests": 1}, "off": {"requests": 1, "errors": 0}}))
    assert list(v.iter_errors({"flag": "f", "on": {"requests": 1, "errors": 0}, "off": {"requests": 1, "errors": 0},
                               "user": "u-1"}))  # never a person


def test_every_repo_carries_the_contracts():
    from pathlib import Path

    import yaml

    cfg = yaml.safe_load((Path(rs.__file__).resolve().parents[2] / "config" / "repos.yaml").read_text())
    managed = next(v["managed_files"] for v in cfg.values() if isinstance(v, dict) and "managed_files" in v)
    assert managed["contracts/events/valxel.events.v1.schema.json"] == "contracts/valxel.events.v1.schema.json"
    assert managed["contracts/audit/valxel.audit.v1.schema.json"].startswith("github:valxel-shared-services/ss-audit-sink/")

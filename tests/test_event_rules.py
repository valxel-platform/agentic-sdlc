"""VX-16: changes go to Kafka first; designs with event workers name their events, and the build
publishes and applies them."""

from valxel_sdlc.agents.parse import parse_events, parse_affected_repos
from valxel_sdlc.evaluators.checks import run_checks
from valxel_sdlc.graph.phase import events_findings, events_instruction
from valxel_sdlc.repo_roles import REPO_ORDER

HLD = """# HLD: Consent capture

## ADRs
- ADR-CLN-FEAT-1-1: Extend the intake API

## Affected repos
- acme-clinic/cln-intake-api (provider)
- acme-clinic/cln-consent-worker (event-worker)

## Contracts
- acme-clinic/cln-intake-api: 1.2.0 -> 1.3.0

## Diagram
```mermaid
flowchart LR
  api[cln-intake-api] --> w[cln-consent-worker]
```

## Audit
- consent.change: consent
{events}"""

EVENTS = """
## Events
- valxel.consent.recorded.v1: published by cln-intake-api, applied by cln-consent-worker
- valxel.consent.withdrawn.v1: published by cln-intake-api, applied by cln-consent-worker
"""


def ctx() -> dict:
    return {"data_class": "phi", "repos": [{"name": "acme-clinic/cln-intake-api", "role": "provider"},
                                           {"name": "acme-clinic/cln-consent-worker", "role": "event-worker"}]}


def test_event_workers_are_a_role_that_merges_after_providers() -> None:
    assert parse_affected_repos(HLD.format(events=""))[1] == {"name": "acme-clinic/cln-consent-worker", "role": "event-worker"}
    assert REPO_ORDER["provider"] < REPO_ORDER["event-worker"] < REPO_ORDER["consumer"]


def test_the_events_section_is_parsed() -> None:
    assert [e["type"] for e in parse_events(EVENTS)] == ["valxel.consent.recorded.v1", "valxel.consent.withdrawn.v1"]
    assert parse_events("## Events\n- consent recorded\n") == []


def test_designs_with_event_workers_must_name_their_events() -> None:
    missing = "no Events section"
    assert any(missing in f for f in run_checks("architecture", HLD.format(events=""), ctx()))
    assert not any(missing in f for f in run_checks("architecture", HLD.format(events=EVENTS), ctx()))
    no_worker = HLD.format(events="").replace("- acme-clinic/cln-consent-worker (event-worker)\n", "")
    assert not any(missing in f for f in run_checks("architecture", no_worker, ctx()))


def test_coders_publish_first_and_workers_apply_once() -> None:
    c = {"events": parse_events(EVENTS)}
    provider = events_instruction(c, {"role": "provider"})
    assert "never write a database in the request" in provider and "valxel.consent.recorded.v1" in provider
    worker = events_instruction(c, {"role": "event-worker"})
    assert "only moves forward" in worker and "InvalidEventException" in worker
    assert events_instruction(c, {"role": "consumer"}) == ""
    assert "never write a database" in events_instruction({}, {"role": "provider"})  # VX-16 applies regardless


def test_the_build_must_publish_or_apply_what_the_design_names() -> None:
    c = {"events": parse_events(EVENTS)}
    worker = {"role": "event-worker", "repo": "o/w", "tests_passed": True}
    ok = {"T1": {**worker, "summary": "Events: valxel.consent.recorded.v1, valxel.consent.withdrawn.v1"}}
    half = {"T1": {**worker, "summary": "Events: valxel.consent.recorded.v1"}}
    web = {"T1": {"role": "consumer", "repo": "o/web", "tests_passed": True, "summary": ""}}
    assert events_findings(c, ok) == [] and events_findings(c, web) == [] and events_findings({}, half) == []
    assert events_findings(c, half) == [
        "No task publishes or applies the events the HLD names: valxel.consent.withdrawn.v1 (VX-16)"]

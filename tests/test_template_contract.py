"""The template contract: every role's template brings security, observability, audit and metrics."""

import pytest

from valxel_sdlc import template_contract as tc
from valxel_sdlc.integrations.github import StubGitHub
from valxel_sdlc.quality.sources import headless
from valxel_sdlc.repo_standards import Target, capability_drift

AREAS = {"security", "observability", "audit", "business-metrics"}


def test_the_shipped_contract_covers_every_area_and_every_role():
    from valxel_sdlc.repo_roles import ROLES

    c = tc.contract()
    assert {cap["area"] for cap in c["capabilities"].values()} == AREAS
    assert set(c["groups"]["all"]) == set(ROLES)
    for role in ROLES:  # every role has its security baseline
        assert {"security-scans", "dependency-review", "dependency-updates", "security-policy"} <= set(tc.required(role))
    for role in c["groups"]["deployed"]:
        assert {"tracing", "error-reports", "image-scan", "supply-chain-pins"} <= set(tc.required(role))
        assert role == "consumer" or "json-logs" in tc.required(role)  # a web app's container only serves files


def test_a_template_declares_where_each_capability_lives_and_the_check_reads_it():
    gh = StubGitHub()
    repo = "valxel-platform/template-job"
    files = {".valxel/capabilities.yaml": "tracing: src/job/telemetry.py\naudit: src/job/audit.py\n"
                                          "error-reports: src/job/nowhere.py\n",
             "src/job/telemetry.py": "from opentelemetry import trace\n", "src/job/audit.py": "# no events yet\n"}
    for path, text in files.items():
        gh.files[(repo, "main", path)] = text
    gaps = {g["capability"]: g["problem"] for g in tc.check(gh, repo, "job")}
    assert "tracing" not in gaps
    assert "doesn't contain" in gaps["audit"] and "doesn't exist" in gaps["error-reports"]
    assert gaps["flags"] == "not declared in .valxel/capabilities.yaml"
    assert tc.check(gh, "valxel-platform/other", "job")[0]["problem"] == ".valxel/capabilities.yaml is missing"
    assert tc.check(gh, repo, "tool") == []  # not a role: nothing asked
    drift = capability_drift(gh, Target(repo, "template", "platform", {"role": "job"}))
    assert drift and all(d.area == "capabilities" and d.fix == "manual" for d in drift)


def test_a_gap_fails_the_repo_check_though_no_apply_can_fix_it():
    from valxel_sdlc.repo_standards import Drift, Report

    rep = Report("valxel-platform/template-job", "template", drift=[Drift("capabilities", "audit", "x", "y", fix="manual")])
    assert not rep.ok
    assert Report("r", "lane", drift=[Drift("visibility", "v", "private", "public", fix="manual")]).ok


def test_the_tools_rules_follow_the_contract():
    from valxel_sdlc.graph.phase import AUDITED_ROLES, FLAGGED_ROLES

    assert set(AUDITED_ROLES) == set(tc.roles_with("audit")) and "job" in AUDITED_ROLES
    assert set(FLAGGED_ROLES) == set(tc.roles_with("flags"))
    assert [r for r in ("provider", "event-worker", "reporting", "job", "bff", "mobile") if headless(r)] == [
        "event-worker", "reporting", "job"]


def test_an_instrumentation_event_goes_only_to_a_repo_that_can_record_it():
    from valxel_sdlc.evaluators.checks import _emitters

    prd = "## Instrumentation\n- alert_opened (GOAL-AW-1): fires when a manager opens an alert\n"
    hld = "## Instrumentation\n- alert_opened: acme-farm/farm-bff\n"
    out = _emitters(hld, [{"name": "acme-farm/farm-bff", "role": "bff"}], {"parent_texts": {"PRD": prd}})
    assert any("no product events module" in f for f in out)
    assert _emitters(hld.replace("farm-bff", "farm-api"), [{"name": "acme-farm/farm-api", "role": "provider"}],
                     {"parent_texts": {"PRD": prd}}) == []


@pytest.mark.parametrize("bad", ["capabilities: {x: {area: style, roles: all, marker: a}}",
                                 "capabilities: {x: {area: audit, roles: nobody, marker: a}}",
                                 "capabilities: {x: {area: audit, roles: [job], marker: '('}}",
                                 "capabilities: {x: {area: audit, roles: [job]}}",
                                 "audit_event: {repos: {a/b: f.py}}",
                                 "audit_event: {type: valxel.audit.v1, repos: [a/b]}",
                                 "capabilities: {audit: {area: audit, roles: [job], marker: 'x\\.audit\\.v1'}}\n"
                                 "audit_event: {type: valxel.audit.v1}"])
def test_a_broken_contract_is_refused(bad):
    with pytest.raises(tc.ContractError):
        tc.parse(bad)


def test_a_template_on_another_audit_type_fails_the_contract():
    gh = StubGitHub()
    repo = "valxel-platform/template-job"
    gh.files[(repo, "main", ".valxel/capabilities.yaml")] = "audit: src/job/audit.py\n"
    gh.files[(repo, "main", "src/job/audit.py")] = 'EVENT_TYPE = "vrixel.audit.v1"\nclass Auditor: ...\n'
    assert "audit" in {g["capability"] for g in tc.check(gh, repo, "job")}
    gh.files[(repo, "main", "src/job/audit.py")] = 'EVENT_TYPE = "valxel.audit.v1"\n'
    assert "audit" not in {g["capability"] for g in tc.check(gh, repo, "job")}


def test_the_audit_sink_and_its_producers_name_the_same_type():
    c = tc.contract()
    assert c["audit_event"]["type"] == "valxel.audit.v1"
    sink, gateway = "valxel-shared-services/ss-audit-sink", "valxel-shared-services/ss-llm-gateway"
    assert {sink, gateway} <= set(c["audit_event"]["repos"])
    gh = StubGitHub()
    gh.files[(sink, "main", "src/audit_sink/records.py")] = (
        'EVENT_TYPE = "valxel.audit.v1"\nEVENT_TYPES = (EVENT_TYPE, "vrixel.audit.v1")\n')
    gh.files[(gateway, "main", "src/llm_gateway/audit.py")] = 'EVENT_TYPE = "vrixel.audit.v1"\n'
    assert tc.audit_event_check(gh, sink) == []  # still accepting the old name as well is fine
    assert tc.audit_event_check(gh, gateway)[0]["problem"] == "src/llm_gateway/audit.py doesn't name valxel.audit.v1"
    gh.files[(gateway, "main", "src/llm_gateway/audit.py")] = 'EVENT_TYPE = "xvalxel.audit.v1"\n'
    assert tc.audit_event_check(gh, gateway)  # a longer name that merely contains it doesn't count
    assert tc.audit_event_check(gh, "valxel-shared-services/ss-governance") == []  # not listed: not asked
    del gh.files[(sink, "main", "src/audit_sink/records.py")]
    assert "doesn't exist" in tc.audit_event_check(gh, sink)[0]["problem"]
    drift = capability_drift(gh, Target(gateway, "lane", "SS", {"role": "provider"}))
    assert [d.item for d in drift] == ["audit-event-type"] and drift[0].fix == "manual"


def test_every_producer_keeps_the_sinks_schema_byte_for_byte():
    c = tc.contract()
    s = c["audit_event"]["schema"]
    sink, agent = s["repo"], "valxel-platform/template-agent"
    assert agent in c["audit_event"]["repos"] and "valxel-platform/template-service" in c["audit_event"]["repos"]
    gh = StubGitHub()
    gh.files[(agent, "main", "src/agent/audit.py")] = 'EVENT_TYPE = "valxel.audit.v1"\n'
    canonical = '{"x-version": "1.0.0"}\n'
    gh.files[(sink, "main", s["path"])] = canonical
    assert tc.audit_event_check(gh, agent)[0]["problem"] == f"{s['copy']} doesn't exist"
    gh.files[(agent, "main", s["copy"])] = '{"x-version": "1.0.0", "edited": true}\n'
    [gap] = tc.audit_event_check(gh, agent)
    assert gap["capability"] == "audit-event-schema" and "differs from" in gap["problem"]
    gh.files[(agent, "main", s["copy"])] = canonical
    assert tc.audit_event_check(gh, agent) == []
    gh.files[(sink, "main", "src/audit_sink/records.py")] = 'EVENT_TYPE = "valxel.audit.v1"\n'
    assert tc.audit_event_check(gh, sink) == []  # the owner keeps no copy of its own


def test_the_schema_entry_needs_all_its_parts():
    with pytest.raises(tc.ContractError, match="audit_event.schema"):
        tc.parse("capabilities: {}\naudit_event: {type: valxel.audit.v1, schema: {repo: a/b}, repos: {a/b: f.py}}")

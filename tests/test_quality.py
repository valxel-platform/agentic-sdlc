"""Tests and security scans: tool-neutral policy, sources, and the gates in the pipeline."""

import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from vrixel_sdlc.config import load_settings
from vrixel_sdlc.engine import FeatureEngine
from vrixel_sdlc.governance import GovernanceError
from vrixel_sdlc.graph import InvalidDecision
from vrixel_sdlc.quality import Finding, SuiteResult, effective_policy, evaluate, load_quality
from vrixel_sdlc.quality.sources import (
    GitHubChecks, GitHubCodeScanning, GitHubDependencyReview, GitHubSecretScanning,
)
from vrixel_sdlc.services import build_services

ROOT = Path(__file__).resolve().parents[1]
CFG = load_quality(ROOT / "config" / "quality.yaml")


@pytest.fixture
def gov_root(tmp_path):
    root = tmp_path / "governance"
    shutil.copytree(ROOT / "examples" / "governance", root)
    return root


def engine_for(tmp_path, gov_root):
    s = load_settings(mode="stub", database_url="", artifact_dir=tmp_path / "a",
                      governance_source="local", governance_root=gov_root)
    svc = build_services(s)
    return FeatureEngine(svc, InMemorySaver()), svc


def add_quality(gov_root, org, block):
    prefix = {"vrixel-emr": "emr", "vrixel-agtech": "ag", "vrixel-pos": "pos", "vrixel-medspa": "ms",
              "vrixel-apx": "apx", "vrixel-shared-services": "ss"}[org]
    path = gov_root / org / f"{prefix}-governance" / "product.yaml"
    path.write_text(path.read_text() + "\nquality:\n" + block)


# --------------------------------------------------------------------------- policy


def test_data_class_floors_tighten_the_recommended_profile():
    general = effective_policy(CFG, "general", {})
    phi = effective_policy(CFG, "phi", {})
    assert general["unit"]["min_coverage"] == 90 and phi["unit"]["min_coverage"] == 90  # VX-11
    assert general["dast"]["required"] is False and phi["dast"]["required"] is True
    assert general["dependencies"]["block_on"] == "high"  # the floor (critical) only limits loosening
    assert phi["sast"]["block_on"] == "high" and phi["secrets"]["block_on"] == "low"


def test_products_change_tools_and_tighten_but_never_loosen():
    problems = []
    p = effective_policy(CFG, "phi", {
        "sast": {"match": {"tools": ["Semgrep", "CodeQL"]}, "block_on": "medium"},
        "unit": {"match": {"checks": ["vitest*"]}, "min_coverage": 95},
        "license": {"kind": "security", "stage": "pr", "source": "github-checks",
                    "match": {"checks": ["license*"]}, "required": True, "block_on": "high"},
    }, problems)
    assert problems == []
    assert p["sast"]["match"]["tools"] == ["Semgrep", "CodeQL"] and p["sast"]["block_on"] == "medium"
    assert p["unit"]["min_coverage"] == 95 and p["license"]["required"] is True
    effective_policy(CFG, "general", {"unit": {"min_coverage": 85}}, problems)
    assert problems == ["quality.unit.min_coverage 85 is below the platform's 90%"]

    problems = []
    effective_policy(CFG, "phi", {
        "sast": {"block_on": "critical"}, "secrets": {"required": False}, "unit": {"min_coverage": 60},
        "fuzz": {"kind": "test"}, "e2e": {"source": "jenkins"}}, problems)
    text = " | ".join(problems)
    for expected in ("sast.block_on critical is looser", "secrets is required by the platform",
                     "unit.min_coverage 60 is below", "fuzz is a new suite and needs",
                     "e2e.source 'jenkins' is not registered"):
        assert expected in text


def test_governance_rejects_a_looser_quality_block(tmp_path, gov_root):
    add_quality(gov_root, "vrixel-emr", "  sast:\n    block_on: critical\n")
    eng, svc = engine_for(tmp_path, gov_root)
    with pytest.raises(GovernanceError, match="sast.block_on critical is looser"):
        svc.snapshot("EMR")


# --------------------------------------------------------------------------- gate


def test_gate_outcomes_and_waivers():
    policy = effective_policy(CFG, "general", {})
    ok_unit = SuiteResult(suite="unit", kind="test", stage="pr", state="passed", repo="o/a", coverage=92)
    clean = [ok_unit] + [SuiteResult(suite=s, kind="security", stage="pr", state="passed", repo="o/a")
                         for s in ("sast", "dependencies", "secrets")]
    assert evaluate("pr", policy, clean, [])["status"] == "passed"

    low_cov = [SuiteResult(suite="unit", kind="test", stage="pr", state="passed", repo="o/a", coverage=61)] + clean[1:]
    res = evaluate("pr", policy, low_cov, [])
    assert res["status"] == "failed" and "unit coverage 61.0% is below 90%" in res["test_failures"][0]

    no_unit = evaluate("pr", policy, clean[1:], [])
    assert no_unit["status"] == "failed" and "unit did not report" in no_unit["test_failures"][0]

    finding = Finding(suite="sast", tool="CodeQL", rule="js/sql-injection", severity="high", title="SQL injection",
                      repo="o/a")
    medium = Finding(suite="sast", tool="CodeQL", rule="js/weak", severity="medium", title="Weak hash", repo="o/a")
    hot = clean[:1] + [SuiteResult(suite="sast", kind="security", stage="pr", state="passed", repo="o/a",
                                   findings=[finding, medium])] + clean[2:]
    res = evaluate("pr", policy, hot, [])
    assert res["status"] == "blocked" and [b["rule"] for b in res["blocking"]] == ["js/sql-injection"]

    later = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
    past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    assert evaluate("pr", policy, hot, [{"fingerprint": finding.fingerprint, "expires": later}])["status"] == "passed"
    assert evaluate("pr", policy, hot, [{"fingerprint": finding.fingerprint, "expires": past}])["status"] == "blocked"

    pending = [SuiteResult(suite="unit", kind="test", stage="pr", state="pending", repo="o/a")] + clean[1:]
    assert evaluate("pr", policy, pending, [])["status"] == "pending"


# --------------------------------------------------------------------------- sources


class FakeGitHub:
    def checks(self, repo, sha):
        return {"state": "success", "checks": [
            {"name": "Unit tests", "state": "success", "url": "u1", "summary": "128 passed, 2 skipped. Coverage: 83.5%"},
            {"name": "lint", "state": "success", "url": "u2", "summary": ""},
            {"name": "ZAP baseline", "state": "failure", "url": "u3", "summary": "2 alerts"}]}

    def code_scanning_alerts(self, repo, pr=None, ref=None):
        return [
            {"number": 7, "html_url": "a7", "tool": {"name": "CodeQL"},
             "rule": {"id": "js/sql-injection", "security_severity_level": "high", "description": "SQL injection"}},
            {"number": 8, "html_url": "a8", "tool": {"name": "Semgrep"},
             "rule": {"id": "x", "severity": "warning", "description": "Something"}}]

    def dependency_diff(self, repo, base, head):
        return [{"change_type": "added", "name": "lodash", "version": "4.17.4", "vulnerabilities": [
                    {"severity": "critical", "advisory_ghsa_id": "GHSA-1", "advisory_summary": "Prototype pollution",
                     "advisory_url": "https://github.com/advisories/GHSA-1"}]},
                {"change_type": "removed", "name": "old", "version": "1", "vulnerabilities": [
                    {"severity": "high", "advisory_ghsa_id": "GHSA-2"}]}]

    def secret_alerts(self, repo):
        return None  # secret scanning not enabled


class Svc:
    github = FakeGitHub()


def test_sources_read_github_results_into_one_shape():
    t = [{"repo": "o/a", "sha": "abc", "number": 3, "base": "main"}]
    (unit,) = GitHubChecks(Svc).collect("unit", {"kind": "test", "match": {"checks": ["unit*"]}}, t, "pr")
    assert (unit.state, unit.passed, unit.skipped, unit.coverage) == ("passed", 128, 2, 83.5)
    (dast,) = GitHubChecks(Svc).collect("dast", {"kind": "security", "match": {"checks": ["zap*"]}}, t, "staging")
    assert dast.state == "failed" and dast.message == "ZAP baseline"

    (sast,) = GitHubCodeScanning(Svc).collect("sast", {"kind": "security", "match": {"tools": ["CodeQL"]}}, t, "pr")
    assert [(f.rule, f.severity) for f in sast.findings] == [("js/sql-injection", "high")]
    (anytool,) = GitHubCodeScanning(Svc).collect("sast", {"kind": "security", "match": {}}, t, "pr")
    assert [f.severity for f in anytool.findings] == ["high", "medium"]  # SARIF "warning" maps to medium

    (deps,) = GitHubDependencyReview(Svc).collect("dependencies", {"kind": "security"}, t, "pr")
    assert [(f.rule, f.severity) for f in deps.findings] == [("GHSA-1", "critical")]  # only what the PR adds

    (sec,) = GitHubSecretScanning(Svc).collect("secrets", {"kind": "security"}, t, "pr")
    assert sec.state == "missing"  # a required suite that cannot report blocks, by design


# --------------------------------------------------------------------------- pipeline


def drive_gates(eng, fid):
    while True:
        p = eng.pending(fid)
        gates = [x for x in p if x["type"] == "gate"]
        if not gates or any(x["type"] != "gate" for x in p):
            return eng.view(fid)
        eng.decide(fid, gates[0]["interrupt_id"], {"decision": "approve", "approver": "maria"})


def sql_injection(suite, repo):
    return SuiteResult(suite="sast", kind="security", stage="pr", state="passed", repo=repo, findings=[
        Finding(suite="sast", tool="CodeQL", rule="js/sql-injection", severity="high",
                title="SQL injection in search", repo=repo, url=f"https://github.com/{repo}/security/code-scanning/7")])


def test_blocking_finding_goes_to_security_who_can_accept_the_risk(tmp_path, gov_root):
    eng, svc = engine_for(tmp_path, gov_root)
    svc.quality.sources["stub"].results[("sast", "vrixel-agtech/agtech-field-api")] = sql_injection
    v = drive_gates(eng, eng.start(product_id="AG", title="Offline crop log", description="x")["feature_id"])
    (esc,) = eng.pending(v["feature_id"])
    assert esc["phase"] == "security" and esc["stage"] == "pr"
    assert esc["blocking"][0]["rule"] == "js/sql-injection" and "HIGH SQL injection" in esc["findings"][0]
    with pytest.raises(InvalidDecision, match="needs a reason"):
        eng.decide(v["feature_id"], esc["interrupt_id"], {"decision": "accept_risk", "approver": "sec"})
    eng.decide(v["feature_id"], esc["interrupt_id"], {"decision": "accept_risk", "approver": "sec",
                                                      "feedback": "input is an enum, not user text",
                                                      "expires_days": 400})
    v = drive_gates(eng, v["feature_id"])
    assert v["status"] == "done"
    (w,) = v["waivers"]
    days = (datetime.fromisoformat(w["expires"]) - datetime.now(timezone.utc)).days
    assert w["approver"] == "sec" and 88 <= days <= 90  # capped at the platform's 90 days
    release = eng.artifact(v["feature_id"], "REL")["content"]
    assert "## Accepted risks" in release and "input is an enum" in release
    assert "## Quality evidence" in release


def test_fix_sends_findings_back_to_the_coders(tmp_path, gov_root):
    eng, svc = engine_for(tmp_path, gov_root)
    stub = svc.quality.sources["stub"]
    stub.results[("sast", "vrixel-agtech/agtech-field-api")] = sql_injection
    fid = drive_gates(eng, eng.start(product_id="AG", title="Offline crop log", description="x")["feature_id"])["feature_id"]
    (esc,) = eng.pending(fid)
    stub.results.clear()  # the agents' next attempt fixes it
    eng.decide(fid, esc["interrupt_id"], {"decision": "retry", "approver": "sec"})
    v = drive_gates(eng, fid)
    assert v["status"] == "done"
    assert any(e["node"] == "security" and e["kind"] == "retry" for e in v["events"])


def test_failing_tests_go_back_to_the_build_escalation(tmp_path, gov_root):
    eng, svc = engine_for(tmp_path, gov_root)
    svc.quality.sources["stub"].results[("unit", "*")] = lambda s, repo: SuiteResult(
        suite="unit", kind="test", stage="pr", state="passed", repo=repo, passed=40, failed=0, coverage=55.0)
    v = drive_gates(eng, eng.start(product_id="AG", title="Offline crop log", description="x")["feature_id"])
    (esc,) = eng.pending(v["feature_id"])
    assert esc["phase"] == "build" and "unit coverage 55.0% is below 90%" in esc["reason"]


def test_e2e_failure_in_staging_asks_release_managers(tmp_path, gov_root):
    eng, svc = engine_for(tmp_path, gov_root)
    svc.quality.sources["stub"].results[("e2e", "vrixel-agtech/agtech-mobile")] = SuiteResult(
        suite="e2e", kind="test", stage="staging", state="failed", repo="vrixel-agtech/agtech-mobile",
        message="checkout.spec.ts")
    v = drive_gates(eng, eng.start(product_id="AG", title="Offline crop log", description="x")["feature_id"])
    (esc,) = eng.pending(v["feature_id"])
    assert esc["phase"] == "delivery" and esc["wait"] == "staging"
    assert "e2e tests failed in vrixel-agtech/agtech-mobile: checkout.spec.ts" in esc["reason"]


def test_g5_and_g6_see_the_evidence(tmp_path, gov_root):
    eng, svc = engine_for(tmp_path, gov_root)
    fid = eng.start(product_id="POS", title="Split tender", description="x")["feature_id"]
    seen = {}
    while (p := [x for x in eng.pending(fid) if x["type"] == "gate"]):
        if p[0]["gate"] in ("g5", "g6"):
            seen[p[0]["gate"]] = p[0]["quality"]
        eng.decide(fid, p[0]["interrupt_id"], {"decision": "approve", "approver": "lead"})
    assert {s["suite"] for s in seen["g5"]["suites"]} >= {"unit", "sast", "dependencies", "secrets"}
    assert {s["stage"] for s in seen["g6"]["suites"]} >= {"pr", "build", "dev", "staging"}

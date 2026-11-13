"""A second opinion before the tool acts alone: OpenChoreo's SRE agent investigates each incident
(only where the product's data may go to its AI provider), triage reads its report, and the tool
fixes things itself only when a high-confidence root cause agrees with the release it suspects.
The tool's track record per action suspends an action that keeps failing. Budget alerts get
OpenChoreo's FinOps agent's right-sizing."""

import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from valxel_sdlc.config import load_settings
from valxel_sdlc.engine import FeatureEngine
from valxel_sdlc.integrations.openchoreo_agents import HttpAgents, StubAgents, default_rca
from valxel_sdlc.ops import paging, remediation
from valxel_sdlc.ops.signals import from_alertmanager, from_openchoreo
from valxel_sdlc.services import build_services

EXAMPLES = Path(__file__).resolve().parents[1] / "examples" / "governance"
POLICY = EXAMPLES / "valxel-tools" / "sdlc-governance" / "platform" / "remediation.yaml"


@pytest.fixture
def root(tmp_path):
    r = tmp_path / "governance"
    shutil.copytree(EXAMPLES, r)
    return r


def make(tmp_path, root, agents=None):
    s = load_settings(mode="stub", database_url="", artifact_dir=tmp_path / "a", governance_source="local",
                      governance_root=root)
    svc = build_services(s)
    svc.agents = agents or StubAgents()
    svc.pager = paging.RecordingPager({"CLN": "k", "FARM": "k"}, "https://sdlc")
    eng = FeatureEngine(svc, InMemorySaver())
    fid = eng.create(product_id="CLN", title="Consent capture at intake")
    eng.auto_drive(fid)
    return eng, svc, fid


def alert(component="cln-intake-api", fp="fp1", severity="error"):
    return from_alertmanager({"alerts": [{"status": "firing", "fingerprint": fp, "startsAt": "2026-11-06T21:00:00Z",
                                          "labels": {"alertname": "HighErrorRate", "severity": severity,
                                                     "component": component}}]})


def gate(eng, iid):
    return next(p for p in eng.pending(iid) if p.get("gate") == "incident")


def urgent(svc):
    return [s for s in svc.pager.sent if s["dedup_key"].endswith(":urgent") and s["event_action"] == "trigger"]


def test_the_policy_gates_analysis_by_data_class_and_region():
    pol = remediation.load_policy(POLICY.read_text())
    classes = {"phi": ["anthropic", "selfhosted"], "general": ["*"], "secret": ["selfhosted"]}
    assert remediation.for_product(pol, {}, "phi", classes)["analysis"]["rca"]["allowed"]
    assert remediation.for_product(pol, {}, "general", classes)["analysis"]["rca"]["allowed"]
    no = remediation.for_product(pol, {}, "secret", classes)["analysis"]["rca"]
    assert not no["allowed"] and "may not go to anthropic" in no["why_not"]
    india = remediation.for_product(pol, {"aws": {"region": "ap-south-1"}}, "phi", classes)["analysis"]["rca"]
    assert not india["allowed"] and "stays in ap-south-1" in india["why_not"]
    opted_out = remediation.for_product(pol, {"remediation": {"analysis": False}}, "phi", classes)["analysis"]["cost"]
    assert not opted_out["allowed"]
    with pytest.raises(remediation.PolicyError, match="fix_agent must be false"):
        remediation.load_policy("autonomous: {}\nanalysis: {fix_agent: true}")
    with pytest.raises(remediation.PolicyError, match="provider"):
        remediation.load_policy("autonomous: {}\nanalysis: {rca: {}}")


def test_agreement_needs_a_confident_cause_in_the_same_component_and_a_change():
    ok, why = remediation.agreement({"status": "completed", "report": default_rca("cln-intake-api", "x")},
                                    "acme-clinic/cln-intake-api")
    assert ok and "agrees" in why
    body = default_rca("cln-intake-api", "x")
    body["result"]["root_causes"][0]["confidence"] = "medium"
    assert remediation.agreement({"status": "completed", "report": body}, "cln-intake-api") == (
        False, "OpenChoreo's analysis isn't confident of any root cause")
    elsewhere = default_rca("cln-db", "x")
    assert remediation.agreement({"status": "completed", "report": elsewhere}, "cln-intake-api")[1].startswith(
        "OpenChoreo's analysis points elsewhere")
    nochange = default_rca("cln-intake-api", "x")
    nochange["result"]["root_causes"][0] |= {"summary": "cln-intake-api ran out of memory", "analysis": "Heap exhausted."}
    nochange["result"]["timeline"] = [{"component": "cln-intake-api", "event": "OOMKilled"}]
    assert "not a change to it" in remediation.agreement({"status": "completed", "report": nochange}, "cln-intake-api")[1]
    none = {"status": "completed", "report": {"result": {"type": "no_root_cause_identified", "explanation": "no logs"}}}
    assert "found no root cause" in remediation.agreement(none, "cln-intake-api")[1]
    assert remediation.agreement({"status": "timed_out"}, "x") == (False, "OpenChoreo's analysis didn't finish in time")


def test_the_tool_acts_when_the_analysis_agrees_and_triage_reads_it(tmp_path, root):
    eng, svc, fid = make(tmp_path, root)
    [iid] = eng.ingest(alert())
    [req] = svc.agents.requests
    assert (req["kind"], req["component"], req["environment"], req["project"], req["alert_id"]) == (
        "rca", "cln-intake-api", "production", "cln", iid)
    v = eng.view(iid)
    assert v["analysis"]["status"] == "completed" and v["taken"][0]["by"] == "agent:remediate"
    kinds = [(e["node"], e["kind"]) for e in v["events"]]
    assert ("analyze", "requested") in kinds and ("analyze", "completed") in kinds and ("auto", "agreed") in kinds
    assert "OpenChoreo's root-cause analysis" in v["triage"] or "OpenChoreo" in str(v["events"])
    assert urgent(svc) == []


def test_a_disagreeing_or_missing_analysis_pages_instead(tmp_path, root):
    agents = StubAgents()
    agents.rca["cln-intake-api"] = {"status": "completed", "report": default_rca("cln-db", "x")}
    eng, svc, fid = make(tmp_path, root, agents)
    [iid] = eng.ingest(alert())
    assert gate(eng, iid) and eng.view(iid)["taken"] == []
    [page] = urgent(svc)
    assert "points elsewhere" in page["payload"]["custom_details"]["why on-call"]
    assert svc.flags.get("CLN", eng.view(fid)["release"]["flag"]) is True  # nothing undone


def test_the_analysis_is_waited_for_then_given_up_on(tmp_path, root, monkeypatch):
    agents = StubAgents()
    agents.rca["cln-intake-api"] = None  # never finishes
    eng, svc, fid = make(tmp_path, root, agents)
    [iid] = eng.ingest(alert())
    [w] = eng.pending(iid)
    assert w["wait"] == "analysis_report" and w["minutes"] == 8  # SEV2
    real = datetime

    class Later(datetime):
        @classmethod
        def now(cls, tz=None):
            return real.now(tz) + timedelta(minutes=9)

    monkeypatch.setattr("valxel_sdlc.engine.datetime", Later)
    eng.poke_now(iid)
    assert gate(eng, iid)
    assert "didn't finish in time" in urgent(svc)[0]["payload"]["custom_details"]["why on-call"]


def test_products_whose_data_may_not_reach_the_provider_get_no_analysis(tmp_path, root):
    gov = root / "acme-clinic" / "cln-governance" / "product.yaml"
    gov.write_text(gov.read_text().replace("runtime:\n", "runtime:\n  remediation: {analysis: false}\n", 1))
    eng, svc, fid = make(tmp_path, root)
    [iid] = eng.ingest(alert())
    assert svc.agents.requests == []
    assert any(e["kind"] == "not_analyzed" for e in eng.view(iid)["events"])
    assert "no second opinion" in urgent(svc)[0]["payload"]["custom_details"]["why on-call"]


def test_the_fix_agent_applying_something_is_a_change_outside_the_tool(tmp_path, root):
    body = default_rca("cln-intake-api", "x")
    body["result"]["recommendations"]["recommended_actions"] = [
        {"description": "Raised LOG_LEVEL", "status": "applied", "change": {"release_binding": "api-production"}}]
    agents = StubAgents()
    agents.rca["cln-intake-api"] = {"status": "completed", "report": body}
    eng, svc, fid = make(tmp_path, root, agents)
    [iid] = eng.ingest(alert())
    assert "OpenChoreo's fix agent changed production" in urgent(svc)[0]["payload"]["summary"]
    ext = next(r for r in svc.changes.list("CLN") if r["kind"] == "external")
    assert ext["requested_by"] == "openchoreo:sre-agent" and ext["evidence"]["actions"] == ["Raised LOG_LEVEL"]


def test_an_action_that_keeps_failing_is_suspended(tmp_path, root):
    eng, svc, fid = make(tmp_path, root)
    svc.remediation_record = lambda pid, days: {"flag_off": {"worked": 1, "failed": 4}, "rollback": {"worked": 0, "failed": 0}}
    [iid] = eng.ingest(alert())
    v = eng.view(iid)
    assert not any(t["type"] == "flag_off" and t["by"] == "agent:remediate" for t in v["taken"])
    assert any(e["kind"] == "suspended" and "worked 1 of 5" in e["detail"] for e in v["events"])
    pol = next(r for r in svc.changes.list("CLN") if r["kind"] == "policy")
    assert pol["action"] == "suspend_autonomy" and "below 60%" in pol["detail"]


def test_the_track_record():
    now = datetime(2026, 11, 6, tzinfo=timezone.utc)

    def inc(ok=True, escalated=False, recovered=True, days=1):
        return {"escalated": escalated, "events": [{"kind": "recovered"}] if recovered else [],
                "taken": [{"type": "rollback", "by": "agent:remediate", "ok": ok, "at": (now - timedelta(days=days)).isoformat()}]}

    rec = remediation.track_record([inc(), inc(), inc(escalated=True), inc(ok=False), inc(days=200),
                                    inc(recovered=False)], 90, now)
    assert rec["rollback"] == {"worked": 2, "failed": 2}
    cal = {"window_days": 90, "min_samples": 4, "suspend_below": 0.6}
    assert "rollback" in remediation.suspended(rec, cal)
    assert remediation.suspended(rec, {**cal, "min_samples": 5}) == {}


def test_a_budget_alert_gets_the_finops_agents_right_sizing(tmp_path, root):
    eng, svc, fid = make(tmp_path, root)
    budget = {"alertName": "farm-field-api-budget", "alertSeverity": "warning", "alertType": "budget",
              "alertValue": 212.4, "alertThreshold": 150, "project": "farm", "component": "farm-field-api",
              "environment": "production", "namespace": "default"}
    [iid] = eng.alarm_changes(from_openchoreo(budget))["incidents"]
    [req] = [r for r in svc.agents.requests if r["kind"] == "cost"]
    assert (req["component"], req["budget"], req["actual"]) == ("farm-field-api", 150.0, 212.4)
    g = gate(eng, iid)
    [a] = [x for x in g["actions"] if x["type"] == "intent_change"]
    assert "cpu 250m/500m" in a["intent"] and "saving about 42.0" in a["intent"]
    assert svc.pager.sent == []  # spend never pages


class FakeHttp:
    def __init__(self, answers):
        self.answers, self.calls = answers, []

    def open(self, req, timeout=None):
        import io
        import json

        self.calls.append((req.get_method(), req.full_url, json.loads(req.data) if req.data else None,
                           dict(req.header_items())))
        body = io.BytesIO(json.dumps(self.answers.pop(0)).encode())
        return type("R", (), {"__enter__": lambda s: body, "__exit__": lambda s, *a: None})()


def test_the_http_client_asks_and_reads_reports():
    http = FakeHttp([{"report_id": "CLN-INC-1_17", "status": "pending"}, {"status": "completed", "report": {"summary": "s"}},
                     {"reportId": "f-1", "status": "pending"}])
    a = HttpAgents("https://rca.oc/", "https://finops.oc", auth=lambda: {"Authorization": "Bearer t"}, opener=http)
    rid = a.request_rca(namespace="default", project="cln", component="api", environment="production",
                        alert_id="CLN-INC-1", alert_name="HighErrorRate", description="d", value=37, at="2026-11-06T21:00:00Z")
    assert rid == "CLN-INC-1_17"
    method, url, body, headers = http.calls[0]
    assert (method, url) == ("POST", "https://rca.oc/api/v1alpha1/rca-agent/analyze")
    assert body["alert"] == {"id": "CLN-INC-1", "value": 37, "timestamp": "2026-11-06T21:00:00Z",
                             "rule": {"name": "HighErrorRate", "description": "d"}}
    assert headers["Authorization"] == "Bearer t"
    assert a.rca_report(rid) == {"status": "completed", "report": {"summary": "s"}}
    assert http.calls[1][1] == "https://rca.oc/api/v1/rca-agent/reports/CLN-INC-1_17"
    assert a.rca_report("../../etc") is None
    assert a.request_cost(namespace="default", project="farm", component="api", environment="production", budget=150,
                          actual=212.4, period="30d", at="t") == "f-1"
    assert http.calls[2][2]["budgetedCost"] == {"amount": 150.0, "period": "30d", "currency": "USD"}

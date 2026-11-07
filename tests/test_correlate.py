"""One incident per failure: a front-end error and the backend error behind it join by trace, call
graph or release; the root cause is the furthest-downstream component; on-call can merge and split."""

import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import InMemorySaver

from vrixel_sdlc.api.app import create_app
from vrixel_sdlc.config import load_settings
from vrixel_sdlc.engine import FeatureEngine
from vrixel_sdlc.graph import InvalidDecision
from vrixel_sdlc.ops.correlate import CallGraph, trace_ids, traces_in_logs
from vrixel_sdlc.ops.signals import from_alertmanager, from_openchoreo, from_sentry
from vrixel_sdlc.services import build_services
from vrixel_sdlc.web.auth import AuthConfig

EXAMPLES = Path(__file__).resolve().parents[1] / "examples" / "governance"
API, WEB = "acme-clinic/cln-intake-api", "acme-clinic/cln-web"
TRACE = "4bf92f3577b34da6a3ce929d0e0e4736"


@pytest.fixture
def eng(tmp_path):
    root = tmp_path / "governance"
    shutil.copytree(EXAMPLES, root)
    s = load_settings(mode="stub", database_url="", artifact_dir=tmp_path / "a", governance_source="local",
                      governance_root=root)
    return FeatureEngine(build_services(s), InMemorySaver())


def web_error(trace=TRACE, issue="9001", at="2026-11-06T18:00:00Z"):
    """A Sentry issue alert from the web app, tagged with the failed call's trace."""
    return {"action": "triggered", "data": {"event": {
        "issue_id": issue, "title": "ApiError: API 503", "level": "error", "culprit": "App.submit",
        "project": {"slug": "cln-web"}, "lastSeen": at, "environment": "production",
        "tags": [["vrixel_product", "CLN"], ["trace_id", trace], ["endpoint", "GET /api/v1/greetings"],
                 ["status", "503"]]}}}


def api_error(trace=TRACE, issue="9100", at="2026-11-06T18:01:00Z"):
    return {"action": "triggered", "data": {"event": {
        "issue_id": issue, "title": "DataAccessResourceFailureException", "level": "error",
        "project": {"slug": "cln-intake-api"}, "lastSeen": at, "environment": "production",
        "tags": [["vrixel_product", "CLN"], ["trace_id", trace]]}}}


def test_traces_and_the_call_graph():
    assert trace_ids({"labels": {"trace_id": TRACE.upper()}}) == {TRACE}
    assert trace_ids({"labels": {"trace_ids": f"{TRACE},{'1' * 32}"}}) == {TRACE, "1" * 32}
    lines = ['{"log.level":"ERROR","trace.id":"%s","error.stack_trace":"x"}' % TRACE,
             '{"log.level":"INFO","trace.id":"%s"}' % ("2" * 32), 'garbage "ERROR" "trace.id":"%s"' % ("3" * 32)]
    assert traces_in_logs(lines) == [TRACE, "3" * 32]
    g = CallGraph([("web", "bff"), ("bff", "api"), ("api", "db-worker")])
    assert g.downstream("web") == {"bff", "api", "db-worker"} and g.related("web", "api")
    assert not g.related("api", "api") and not g.related("web", "")
    assert g.root(["org/web", "api", "bff"]) == "api"
    assert g.root(["x", "y"]) == "x"  # unrelated: the first


def test_a_web_error_and_the_api_error_behind_it_are_one_incident(eng):
    [iid] = eng.ingest(from_sentry(web_error()))
    assert eng.ingest(from_sentry(api_error())) == [iid]  # the same request (trace)
    sigs = eng.services.signals.for_incident(iid)
    assert sigs[-1]["labels"]["joined_by"].startswith(f"trace: the same request ({TRACE})")
    # The API is downstream of the web app: it's where the failure starts.
    g = next(p for p in eng.pending(iid) if p.get("gate") == "incident")
    assert g["root"]["component"] == API and "cln-web" in g["root"]["why"]
    # Triage ran again around the new root (an agent's revise), and the record says why.
    events = eng._state(iid)["events"]
    assert any(e["kind"] == "revise" and "root cause candidate is now" in e.get("detail", "") for e in events)



def test_an_openchoreo_log_alert_finds_its_traces_in_the_logs(eng):
    oc = eng.services.openchoreo
    oc.observe = lambda **kw: {"available": True, "logs": [
        '{"log.level":"ERROR","trace.id":"%s","error.stack_trace":"java.lang..."}' % TRACE]}
    [iid] = eng.ingest(from_sentry(web_error(at="2026-11-06T09:00:00Z")))
    alert = {"alertName": "cln-intake-api-exceptions", "alertSeverity": "warning", "alertType": "log",
             "alertDescription": "More than 10 exceptions", "project": "cln", "component": "cln-intake-api",
             "environment": "production", "alertTimestamp": "2026-11-06T15:00:00Z"}  # hours later
    assert eng.alarm_changes(from_openchoreo(alert))["incidents"] == [iid]
    assert eng.services.signals.for_incident(iid)[-1]["labels"]["trace_ids"] == TRACE


def test_a_signal_tying_two_incidents_merges_them(eng):
    [a] = eng.ingest(from_alertmanager({"alerts": [{"status": "firing", "fingerprint": "w1",
                                                     "startsAt": "2026-11-06T10:00:00Z",
                                                     "labels": {"alertname": "WebErrors", "severity": "warning",
                                                                "component": "cln-web", "vrixel_product": "CLN"}}]}))
    [b] = eng.ingest(from_sentry(api_error(at="2026-11-06T12:00:00Z")))  # 2 hours later: separate
    assert a != b
    # A web error (joining the web alert's incident) that names the API error's request ties them.
    [c] = eng.ingest(from_sentry(web_error(at="2026-11-06T10:10:00Z")))
    assert c == a
    assert eng._state(b)["status"] == "merged" and eng._state(b)["links"] == {a: "merged into"}
    assert "sentry:9100" in [s["fingerprint"] for s in eng.services.signals.for_incident(a)]
    assert b not in eng.services.signals.open_incidents("CLN")


def test_on_call_merges_and_splits(eng, monkeypatch):
    [a] = eng.ingest(from_sentry(web_error(trace="6" * 32, issue="1", at="2026-11-06T10:00:00Z")))
    [b] = eng.ingest(from_sentry(api_error(trace="7" * 32, issue="2", at="2026-11-06T16:00:00Z")))
    with pytest.raises(InvalidDecision):
        eng.decide(a, next(p for p in eng.pending(a) if p.get("gate") == "incident")["interrupt_id"],
                   {"decision": "merge", "approver": "oncall-1"})
    client = TestClient(create_app(eng, auth=AuthConfig(mode="dev", secret="x" * 40), web_dist=None))
    client.post("/api/auth/dev-login", json={"login": "dev", "orgs": ["acme-clinic"]})
    assert client.post(f"/api/incidents/{a}/merge", json={"into": b}).status_code == 403  # not on-call
    client.post("/api/auth/dev-login", json={"login": "oncall-1", "orgs": ["acme-clinic"], "teams": ["acme-clinic/release-managers"]})
    r = client.post(f"/api/incidents/{a}/merge", json={"into": b, "reason": "same outage"})
    assert r.status_code == 200 and r.json()["signals"] == 1 and r.json()["ended"] is True
    assert eng._state(a)["status"] == "merged"
    assert client.post(f"/api/incidents/{a}/merge", json={"into": "CLN-INC-99"}).status_code == 400

    r = client.post(f"/api/incidents/{b}/split", json={"fingerprint": "sentry:1"})
    assert r.status_code == 200
    new = r.json()["new_incident"]
    assert new and new not in (a, b)
    assert [s["fingerprint"] for s in eng.services.signals.for_incident(b)] == ["sentry:2"]
    assert eng.services.signals.for_incident(new)[0]["labels"]["split_from"] == b
    assert client.post(f"/api/incidents/{b}/split", json={"fingerprint": "nope"}).status_code == 404

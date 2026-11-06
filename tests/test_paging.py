"""Paging: every incident pages the product's on-call through PagerDuty as it opens, before triage;
on-call's first decision acknowledges it; mitigation, resolution or a merge resolves it."""

import io
import json
import shutil
import time
from pathlib import Path
from urllib.error import HTTPError

import pytest
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import InMemorySaver

from vrixel_sdlc.api.app import create_app
from vrixel_sdlc.config import load_settings
from vrixel_sdlc.engine import FeatureEngine
from vrixel_sdlc.ops import paging
from vrixel_sdlc.ops.signals import from_alertmanager, from_sentry
from vrixel_sdlc.services import build_services
from vrixel_sdlc.web.auth import AuthConfig

EXAMPLES = Path(__file__).resolve().parents[1] / "examples" / "governance"
KEYS = {"EMR": "emr-key", "AG": "ag-key"}


def alert(status="firing", fingerprint="fp1", component="emr-intake-api", severity="critical", product=None,
          at="2026-11-06T21:00:00Z"):
    labels = {"alertname": "HighErrorRate", "severity": severity, "component": component}
    if product:
        labels["vrixel_product"] = product
    return {"alerts": [{"status": status, "fingerprint": fingerprint, "startsAt": at, "labels": labels,
                        "annotations": {"summary": f"Errors on {component}: patient 4411 lookup failed"}}]}


@pytest.fixture
def eng(tmp_path):
    root = tmp_path / "governance"
    shutil.copytree(EXAMPLES, root)
    s = load_settings(mode="stub", database_url="", artifact_dir=tmp_path / "a", governance_source="local",
                      governance_root=root)
    svc = build_services(s)
    svc.pager = paging.RecordingPager(KEYS, "https://sdlc.vrixel.dev")
    return FeatureEngine(svc, InMemorySaver())


def gate(eng, iid, name):
    return next(p for p in eng.pending(iid) if p.get("gate") == name)


def test_the_page_says_how_bad_and_where_and_nothing_private():
    inc = {"incident_id": "EMR-INC-4", "product_id": "EMR", "title": "Patient 4411: NullPointerException",
           "severity": "sev1", "component": "vrixel-emr/emr-intake-api", "env": "production", "source": "sentry",
           "data_class": "phi"}
    body = paging.page_payload(inc, "k", "https://sdlc.vrixel.dev/")
    assert (body["routing_key"], body["event_action"], body["dedup_key"]) == ("k", "trigger", "EMR-INC-4")
    p = body["payload"]
    assert p["severity"] == "critical" and p["component"] == "emr-intake-api" and p["group"] == "EMR"
    assert "4411" not in json.dumps(body) and "NullPointer" not in json.dumps(body)  # PHI: no title
    assert p["summary"] == "EMR-INC-4 SEV1 on emr-intake-api: open the incident in the SDLC tool"
    assert body["links"] == [{"href": "https://sdlc.vrixel.dev/incidents/EMR-INC-4", "text": "EMR-INC-4 in the SDLC tool"}]
    general = paging.page_payload({**inc, "data_class": "general", "severity": "sev3"}, "k", "u")
    assert general["payload"]["severity"] == "warning" and "NullPointerException" in general["payload"]["summary"]
    cost = paging.page_payload({**inc, "data_class": "general", "severity": "sev2", "category": "cost"}, "k", "u")
    assert cost["payload"]["severity"] == "info" and cost["payload"]["class"] == "cost"  # spend never wakes anyone
    assert paging.page_payload({**inc, "severity": "sev4"}, "k", "u")["payload"]["severity"] == "info"


def test_an_incident_pages_on_call_as_it_opens(eng):
    [iid] = eng.ingest(from_alertmanager(alert()))
    [sent] = eng.services.pager.sent
    assert sent["dedup_key"] == iid and sent["routing_key"] == "emr-key" and sent["payload"]["severity"] == "critical"
    assert "4411" not in json.dumps(sent)
    v = eng.view(iid)
    assert v["events"][0]["kind"] == "paged" and v["events"][0]["node"] == "page"
    assert [h["action"] for h in v["paging"]] == ["trigger"]
    # More signals for the same incident don't page again.
    eng.ingest(from_alertmanager(alert(fingerprint="fp2")))
    assert len(eng.services.pager.sent) == 1


def test_a_product_without_a_pagerduty_service_says_so(eng):
    [iid] = eng.ingest(from_alertmanager(alert(component="pos-terminal-api", product="POS")))
    assert eng.services.pager.sent == []
    ev = eng.view(iid)["events"][0]
    assert ev["kind"] == "not_paged" and "POS has no PagerDuty service" in ev["detail"]


def test_the_first_decision_acknowledges_and_mitigation_resolves(eng):
    [iid] = eng.ingest(from_alertmanager(alert()))
    eng.decide(iid, gate(eng, iid, "incident")["interrupt_id"],
               {"decision": "revise", "approver": "oncall-1", "feedback": "Look at the database"})
    eng.decide(iid, gate(eng, iid, "incident")["interrupt_id"], {"decision": "approve", "approver": "oncall-1"})
    actions = [s["event_action"] for s in eng.services.pager.sent]
    assert actions == ["trigger", "acknowledge"]  # once, though on-call decided twice
    eng.ingest(from_alertmanager(alert(status="resolved")))  # recovered: postmortem
    pm = gate(eng, iid, "postmortem")
    assert [s["event_action"] for s in eng.services.pager.sent] == ["trigger", "acknowledge", "resolve"]
    eng.decide(iid, pm["interrupt_id"], {"decision": "revise", "approver": "oncall-1", "feedback": "more detail"})
    assert len(eng.services.pager.sent) == 3  # resolved once


def test_an_agents_decision_doesnt_acknowledge_and_a_merge_resolves(eng):
    trace = "4bf92f3577b34da6a3ce929d0e0e4736"

    def issue(n, slug, at):
        return {"action": "triggered", "data": {"event": {
            "issue_id": n, "title": "ApiError", "level": "error", "project": {"slug": slug}, "lastSeen": at,
            "environment": "production", "tags": [["vrixel_product", "EMR"], ["trace_id", trace if n == "3" else n * 32]]}}}

    [a] = eng.ingest(from_sentry(issue("1", "emr-web", "2026-11-06T10:00:00Z")))
    [b] = eng.ingest(from_sentry(issue("2", "emr-intake-api", "2026-11-06T16:00:00Z")))
    assert a != b
    eng.merge_incidents(b, a, approver="agent:correlate", reason="same request")
    sent = [(s["dedup_key"], s["event_action"]) for s in eng.services.pager.sent]
    assert (b, "resolve") in sent and (b, "acknowledge") not in sent and (a, "resolve") not in sent


class FakePD:
    def __init__(self, codes):
        self.codes, self.bodies = list(codes), []

    def open(self, req, timeout=None):
        assert req.full_url == "https://events.pagerduty.com/v2/enqueue" and timeout == 10
        self.bodies.append(json.loads(req.data))
        code = self.codes.pop(0) if self.codes else 202
        if code == "down":
            raise OSError("connection reset")
        if code >= 300:
            raise HTTPError(req.full_url, code, "no", {}, io.BytesIO(b'{"status":"invalid event"}'))
        resp = io.BytesIO(b'{"status":"success"}')
        resp.status = code
        resp.__enter__ = lambda *a: resp
        return type("R", (), {"__enter__": lambda s: resp, "__exit__": lambda s, *a: None})()


def test_pagerduty_is_retried_in_order_and_failures_are_kept():
    pd = FakePD([500, "down", 429, 202, 400])
    pager = paging.PagerDutyPager({"EMR": "k"}, "https://sdlc", opener=pd, delays=(0, 0, 0, 0))
    inc = {"incident_id": "EMR-INC-1", "product_id": "EMR", "title": "t", "severity": "sev2", "data_class": "phi"}
    t0 = time.time()
    assert pager.trigger(inc).startswith("paging EMR's on-call")
    assert time.time() - t0 < 0.5  # never waits on PagerDuty
    pager.resolve("EMR-INC-1", "EMR")
    pager.flush()
    assert [b["event_action"] for b in pd.bodies] == ["trigger"] * 4 + ["resolve"]  # retried, then in order
    h = pager.history("EMR-INC-1")
    assert h[0]["ok"] and "attempt 4" in h[0]["detail"]
    assert not h[1]["ok"] and "HTTP 400" in h[1]["detail"]  # a refused event isn't retried


def test_the_pager_comes_from_the_environment(monkeypatch):
    monkeypatch.delenv("VRIXEL_PAGERDUTY_ROUTING_KEYS", raising=False)
    assert type(paging.from_env("u")) is paging.RecordingPager
    monkeypatch.setenv("VRIXEL_PAGERDUTY_ROUTING_KEYS", '{"EMR": "k", "AG": ""}')
    p = paging.from_env("u")
    assert isinstance(p, paging.PagerDutyPager) and p.routing_keys == {"EMR": "k"}
    monkeypatch.setenv("VRIXEL_PAGERDUTY_REGION", "EU")
    assert paging.from_env("u").url == "https://events.eu.pagerduty.com/v2/enqueue"
    monkeypatch.setenv("VRIXEL_PAGERDUTY_REGION", "mars")
    with pytest.raises(ValueError):
        paging.from_env("u")
    monkeypatch.setenv("VRIXEL_PAGERDUTY_ROUTING_KEYS", "not json")
    with pytest.raises(ValueError):
        paging.from_env("u")


def test_a_webhook_pages_and_answers_before_triage_runs(eng, monkeypatch):
    import threading

    from vrixel_sdlc.ops import incident as incident_mod

    release = threading.Event()
    real = incident_mod.stub_triage

    def slow_triage(ctx):
        release.wait(10)
        return real(ctx)

    monkeypatch.setattr(incident_mod, "stub_triage", slow_triage)
    monkeypatch.setenv("VRIXEL_SIGNALS_TOKEN", "t0k")
    client = TestClient(create_app(eng, auth=AuthConfig(mode="dev", secret="x" * 40), web_dist=None))
    r = client.post("/api/signals/alertmanager", content=json.dumps(alert()), headers={"authorization": "Bearer t0k"})
    assert r.status_code == 202 and r.json()["incidents"] == ["EMR-INC-1"]
    assert eng.services.pager.sent[0]["dedup_key"] == "EMR-INC-1"  # paged; triage still running
    assert "EMR-INC-1" in eng.running
    release.set()
    end = time.time() + 10
    while "EMR-INC-1" in eng.running and time.time() < end:
        time.sleep(0.02)
    assert gate(eng, "EMR-INC-1", "incident")
    client.post("/api/auth/dev-login", json={"login": "oncall-1", "orgs": ["vrixel-emr"]})
    assert [h["action"] for h in client.get("/api/features/EMR-INC-1").json()["paging"]] == ["trigger"]

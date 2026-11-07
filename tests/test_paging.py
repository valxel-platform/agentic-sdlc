"""Paging: a SEV1 tells on-call (low urgency) as it opens; an incident the tool can't fix pages them
urgently; on-call's first decision acknowledges; mitigation, resolution or a merge resolves."""

import io
import json
import shutil
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError

import pytest
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import InMemorySaver

from vrixel_sdlc.api.app import create_app
from vrixel_sdlc.config import load_settings
from vrixel_sdlc.engine import FeatureEngine
from vrixel_sdlc.ops import incident as incident_mod
from vrixel_sdlc.ops import paging
from vrixel_sdlc.ops.signals import from_alertmanager, from_sentry
from vrixel_sdlc.services import build_services
from vrixel_sdlc.web.auth import AuthConfig

EXAMPLES = Path(__file__).resolve().parents[1] / "examples" / "governance"
KEYS = {"CLN": "cln-key", "FARM": "farm-key"}


def alert(status="firing", fingerprint="fp1", component="cln-intake-api", severity="critical", product=None,
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


def no_autonomy(root, product="acme-clinic/cln-governance"):
    gov = root / product / "product.yaml"
    gov.write_text(gov.read_text().replace("runtime:\n", "runtime:\n  remediation: {autonomous: false}\n", 1))


def test_the_page_says_how_bad_and_where_and_nothing_private():
    inc = {"incident_id": "CLN-INC-4", "product_id": "CLN", "title": "Patient 4411: NullPointerException",
           "severity": "sev1", "component": "acme-clinic/cln-intake-api", "env": "production", "source": "sentry",
           "data_class": "phi", "why": "the tool's rollback didn't fix it"}
    body = paging.page_payload(inc, "k", "https://sdlc.vrixel.dev/", urgent=True)
    assert (body["routing_key"], body["event_action"], body["dedup_key"]) == ("k", "trigger", "CLN-INC-4:urgent")
    p = body["payload"]
    assert p["severity"] == "critical" and p["component"] == "cln-intake-api" and p["group"] == "CLN"
    assert "4411" not in json.dumps(body) and "NullPointer" not in json.dumps(body)  # PHI: no title
    assert p["summary"] == "CLN-INC-4 SEV1 on cln-intake-api needs on-call (the tool's rollback didn't fix it)"
    assert p["custom_details"]["why on-call"] == "the tool's rollback didn't fix it"
    assert body["links"] == [{"href": "https://sdlc.vrixel.dev/incidents/CLN-INC-4", "text": "CLN-INC-4 in the SDLC tool"}]
    heads_up = paging.page_payload(inc, "k", "u")
    assert heads_up["dedup_key"] == "CLN-INC-4" and heads_up["payload"]["severity"] == "info"
    assert "the SDLC tool is mitigating it (no action needed)" in heads_up["payload"]["summary"]
    general = paging.page_payload({**inc, "data_class": "general", "severity": "sev3"}, "k", "u", urgent=True)
    assert general["payload"]["severity"] == "error" and "NullPointerException" in general["payload"]["summary"]


def released(eng):
    fid = eng.create(product_id="CLN", title="Consent capture at intake")
    eng.auto_drive(fid)
    return fid


def test_a_sev1_tells_on_call_as_it_opens_while_the_tool_fixes_it(eng):
    released(eng)
    [iid] = eng.ingest(from_alertmanager(alert()))  # critical: SEV1
    [sent] = eng.services.pager.sent
    assert sent["dedup_key"] == iid and sent["routing_key"] == "cln-key" and sent["payload"]["severity"] == "info"
    assert "4411" not in json.dumps(sent)
    v = eng.view(iid)
    assert v["events"][0]["kind"] == "paged" and v["events"][0]["node"] == "page"
    assert [h["action"] for h in v["paging"]] == ["heads_up"]
    assert v["taken"][0]["by"] == "agent:remediate"  # the tool is on it
    eng.ingest(from_alertmanager(alert(fingerprint="fp2")))  # more signals: no second page
    assert len(eng.services.pager.sent) == 1


def test_a_sev2_the_tool_fixes_never_pages(eng):
    released(eng)
    [iid] = eng.ingest(from_alertmanager(alert(severity="error")))
    assert eng.services.pager.sent == []
    v = eng.view(iid)
    assert v["events"][0]["kind"] == "not_paged" and v["taken"][0]["by"] == "agent:remediate"


def test_what_the_tool_cant_fix_pages_urgently(tmp_path, eng):
    # No recent change to the component: no action the tool may take fits.
    [iid] = eng.ingest(from_alertmanager(alert(severity="error")))  # SEV2: no heads-up
    [sent] = eng.services.pager.sent
    assert sent["dedup_key"] == f"{iid}:urgent" and sent["payload"]["severity"] == "error"
    assert "needs on-call (no action the tool may take by itself fits" in sent["payload"]["summary"]
    g = gate(eng, iid, "incident")
    assert any(e["node"] == "escalate" and e["kind"] == "paged" for e in eng.view(iid)["events"])
    eng.decide(iid, g["interrupt_id"], {"decision": "resolve", "approver": "oncall-1", "feedback": "noise"})
    assert [(s["dedup_key"], s["event_action"]) for s in eng.services.pager.sent] == [
        (f"{iid}:urgent", "trigger"), (f"{iid}:urgent", "acknowledge"), (f"{iid}:urgent", "resolve")]


def test_a_product_without_a_pagerduty_service_says_so(eng):
    [iid] = eng.ingest(from_alertmanager(alert(component="shop-terminal-api", product="SHOP")))
    assert eng.services.pager.sent == []
    ev = eng.view(iid)["events"][0]
    assert ev["kind"] == "not_paged" and "SHOP has no PagerDuty service" in ev["detail"]


def test_the_first_decision_acknowledges_and_mitigation_resolves(tmp_path, monkeypatch):
    root = tmp_path / "gov"
    shutil.copytree(EXAMPLES, root)
    no_autonomy(root)
    s = load_settings(mode="stub", database_url="", artifact_dir=tmp_path / "a", governance_source="local",
                      governance_root=root)
    svc = build_services(s)
    svc.pager = paging.RecordingPager(KEYS, "https://sdlc.vrixel.dev")
    eng = FeatureEngine(svc, InMemorySaver())
    released(eng)
    [iid] = eng.ingest(from_alertmanager(alert()))
    keys = lambda: [(x["dedup_key"], x["event_action"]) for x in svc.pager.sent]  # noqa: E731
    assert keys() == [(iid, "trigger"), (f"{iid}:urgent", "trigger")]  # heads-up, then the tool may not act
    eng.decide(iid, gate(eng, iid, "incident")["interrupt_id"],
               {"decision": "revise", "approver": "oncall-1", "feedback": "Look at the database"})
    eng.decide(iid, gate(eng, iid, "incident")["interrupt_id"], {"decision": "approve", "approver": "oncall-1"})
    assert keys()[2:] == [(iid, "acknowledge"), (f"{iid}:urgent", "acknowledge")]  # once, though on-call decided twice
    eng.ingest(from_alertmanager(alert(status="resolved")))
    monkeypatch.setattr(incident_mod, "_clock", lambda: datetime.now(timezone.utc) + timedelta(minutes=11))
    eng.poke_now(iid)
    pm = gate(eng, iid, "postmortem")
    assert keys()[4:] == [(iid, "resolve"), (f"{iid}:urgent", "resolve")]
    eng.decide(iid, pm["interrupt_id"], {"decision": "revise", "approver": "oncall-1", "feedback": "more detail"})
    assert len(keys()) == 6  # resolved once


def test_an_agents_merge_resolves_without_acknowledging(eng):
    def issue(n, slug, at):
        return {"action": "triggered", "data": {"event": {
            "issue_id": n, "title": "ApiError", "level": "fatal", "project": {"slug": slug}, "lastSeen": at,
            "environment": "production", "tags": [["vrixel_product", "CLN"], ["trace_id", n * 32]]}}}

    [a] = eng.ingest(from_sentry(issue("1", "cln-web", "2026-11-06T10:00:00Z")))
    [b] = eng.ingest(from_sentry(issue("2", "cln-intake-api", "2026-11-06T16:00:00Z")))
    assert a != b
    eng.merge_incidents(b, a, approver="agent:correlate", reason="same request")
    sent = [(s["dedup_key"], s["event_action"]) for s in eng.services.pager.sent]
    assert (b, "resolve") in sent and (b, "acknowledge") not in sent and (a, "resolve") not in sent
    assert eng._state(b)["status"] == "merged"


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
    pager = paging.PagerDutyPager({"CLN": "k"}, "https://sdlc", opener=pd, delays=(0, 0, 0, 0))
    inc = {"incident_id": "CLN-INC-1", "product_id": "CLN", "title": "t", "severity": "sev2", "data_class": "phi"}
    t0 = time.time()
    assert pager.trigger(inc, urgent=True).startswith("paged CLN's on-call urgently")
    assert time.time() - t0 < 0.5  # never waits on PagerDuty
    pager.resolve("CLN-INC-1", "CLN")
    pager.flush()
    assert [b["event_action"] for b in pd.bodies] == ["trigger"] * 4 + ["resolve"]  # retried, then in order
    assert {b["dedup_key"] for b in pd.bodies} == {"CLN-INC-1:urgent"}
    h = pager.history("CLN-INC-1")
    assert h[0]["ok"] and "attempt 4" in h[0]["detail"]
    assert not h[1]["ok"] and "HTTP 400" in h[1]["detail"]  # a refused event isn't retried


def test_the_pager_comes_from_the_environment(monkeypatch):
    monkeypatch.delenv("VRIXEL_PAGERDUTY_ROUTING_KEYS", raising=False)
    assert type(paging.from_env("u")) is paging.RecordingPager
    monkeypatch.setenv("VRIXEL_PAGERDUTY_ROUTING_KEYS", '{"CLN": "k", "FARM": ""}')
    p = paging.from_env("u")
    assert isinstance(p, paging.PagerDutyPager) and p.routing_keys == {"CLN": "k"}
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
    assert r.status_code == 202 and r.json()["incidents"] == ["CLN-INC-1"]
    assert eng.services.pager.sent[0]["dedup_key"] == "CLN-INC-1"  # told on-call; triage still running
    assert "CLN-INC-1" in eng.running
    release.set()
    end = time.time() + 10
    while "CLN-INC-1" in eng.running and time.time() < end:
        time.sleep(0.02)
    assert gate(eng, "CLN-INC-1", "incident")  # nothing recent to undo: on-call is needed
    client.post("/api/auth/dev-login", json={"login": "oncall-1", "orgs": ["acme-clinic"]})
    assert [h["action"] for h in client.get("/api/features/CLN-INC-1").json()["paging"]] == ["heads_up", "urgent"]

"""Production issues: signals in (Prometheus, Sentry, tickets), incidents triaged by agents and
decided by on-call, mitigations (flag off, rollback), expedited hotfixes, intent changes,
recovery, postmortems."""

import hashlib
import hmac
import json
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import InMemorySaver

from vrixel_sdlc.api.app import create_app
from vrixel_sdlc.config import load_settings
from vrixel_sdlc.engine import FeatureEngine
from vrixel_sdlc.knowledge import render
from vrixel_sdlc.ops import incident as incident_mod
from vrixel_sdlc.ops.signals import from_alertmanager, from_sentry, from_ticket, resolve_product
from vrixel_sdlc.services import build_services
from vrixel_sdlc.web.auth import AuthConfig

ROOT = Path(__file__).resolve().parents[1]
API = "vrixel-emr/emr-intake-api"


@pytest.fixture
def gov_root(tmp_path):
    root = tmp_path / "governance"
    shutil.copytree(ROOT / "examples" / "governance", root)
    return root


def make(tmp_path, gov_root):
    s = load_settings(mode="stub", database_url="", artifact_dir=tmp_path / "a",
                      governance_source="local", governance_root=gov_root)
    svc = build_services(s)
    return FeatureEngine(svc, InMemorySaver()), svc


def alert(status="firing", fingerprint="fp1", component="emr-intake-api", severity="critical", name="HighErrorRate"):
    return {"alerts": [{"status": status, "fingerprint": fingerprint, "startsAt": "2026-11-03T21:00:00Z",
                        "labels": {"alertname": name, "severity": severity, "component": component},
                        "annotations": {"summary": f"{name} on {component}"}}]}


def gate(eng, tid, name):
    return next(p for p in eng.pending(tid) if p.get("gate") == name)


def released_feature(eng, title="Consent capture at intake"):
    fid = eng.create(product_id="EMR", title=title)
    assert eng.auto_drive(fid)["status"] == "done"
    return fid


# --------------------------------------------------------------------------- signals


def test_signals_are_normalized():
    [a] = from_alertmanager(alert(severity="warning"))
    assert (a["source"], a["status"], a["severity"], a["component"]) == ("prometheus", "firing", "sev3", "emr-intake-api")
    assert from_alertmanager(alert(status="resolved"))[0]["status"] == "resolved"
    [s] = from_sentry({"action": "created", "data": {"issue": {
        "id": "42", "title": "TypeError in consent form", "level": "error", "project": {"slug": "emr-web"},
        "web_url": "https://sentry.io/i/42", "tags": [["vrixel_product", "EMR"]]}}}, "issue")
    assert (s["severity"], s["product_id"], s["component"], s["fingerprint"]) == ("sev2", "EMR", "emr-web", "sentry:42")
    [t] = from_ticket({"product_id": "EMR", "title": "Can't edit consent", "description": "x", "reporter": "fd-1"})
    assert t["source"] == "ticket" and t["severity"] == "sev3" and t["labels"]["reporter"] == "fd-1"
    assert from_ticket({"product_id": "EMR"}) == []


def test_signals_map_to_a_product_and_component(tmp_path, gov_root):
    _, svc = make(tmp_path, gov_root)
    [a] = from_alertmanager(alert())
    assert resolve_product(svc, a) == ("EMR", API)
    [b] = from_alertmanager(alert(component="nobody-knows-me"))
    assert resolve_product(svc, b) == ("", "")


# --------------------------------------------------------------------------- incidents


def test_an_alert_opens_an_incident_and_on_call_turns_the_new_feature_off_and_starts_a_hotfix(tmp_path, gov_root):
    eng, svc = make(tmp_path, gov_root)
    fid = released_feature(eng)
    key = eng.view(fid)["release"]["flag"]
    assert svc.flags.get("EMR", key) is True

    [iid] = eng.ingest(from_alertmanager(alert()))
    assert iid == "EMR-INC-1"
    g = gate(eng, iid, "incident")
    assert g["approvers"] == ["vrixel-emr/release-managers"]  # on-call falls back to G6's approvers
    assert g["severity"] == "sev1" and g["suspects"][0]["feature_id"] == fid
    assert [a["type"] for a in g["actions"]] == ["flag_off", "hotfix"]
    assert svc.flags.get("EMR", key) is True  # nothing happens before a person decides

    eng.decide(iid, g["interrupt_id"], {"decision": "approve", "approver": "oncall-1"})
    v = eng.view(iid)
    assert svc.flags.get("EMR", key) is False
    assert [t["ok"] for t in v["taken"]] == [True, True] and v["status"] == "monitoring"
    [hotfix] = [k for k, kind in v["links"].items() if kind == "hotfix"]
    eng._pool.shutdown(wait=True)  # the hotfix runs in the background
    h = eng.view(hotfix)
    assert {s["key"]: s["state"] for s in h["rail"]}["g4"] == "skipped"
    assert eng._state(hotfix)["track"] == "hotfix" and "Hotfix design" in eng.artifact(hotfix, "HLD")["content"]


def test_a_hotfix_starts_at_planning_and_on_call_approves_merge_and_release(tmp_path, gov_root):
    eng, svc = make(tmp_path, gov_root)
    gov = gov_root / "vrixel-emr" / "governance" / "product.yaml"
    gov.write_text(gov.read_text().replace("approvers:\n", "approvers:\n  oncall:\n  - vrixel-emr/oncall\n", 1))
    incident = {"incident_id": "EMR-INC-9", "product_id": "EMR", "title": "5xx on intake", "severity": "sev2"}
    hid = eng.create(product_id="EMR", title="Hotfix: guard null consent", track="hotfix",
                     hotfix={**incident, "repo": API, "change": "guard null consent", "approved_by": "oncall-1"})
    seen = {}
    for _ in range(10):
        p = [x for x in eng.pending(hid) if x["type"] == "gate"]
        if not p:
            break
        seen[p[0]["gate"]] = p[0]["approvers"]
        eng.decide(hid, p[0]["interrupt_id"], {"decision": "approve", "approver": "oncall-1"})
    assert set(seen) <= {"g5", "g6"} and seen["g6"] == ["vrixel-emr/oncall"]
    v = eng.auto_drive(hid)
    assert v["status"] == "done" and v["release"]["flag_on"] is True
    assert not [c for c in svc.catalog.lookup("EMR") if c.get("feature") == hid]  # a fix is not a capability


def test_signals_join_their_open_incident(tmp_path, gov_root):
    eng, svc = make(tmp_path, gov_root)
    [a] = eng.ingest(from_alertmanager(alert()))
    assert eng.ingest(from_alertmanager(alert())) == [a]  # same alert
    assert eng.ingest(from_alertmanager(alert(fingerprint="fp2", name="HighLatency"))) == [a]  # same component
    [b] = eng.ingest(from_alertmanager(alert(fingerprint="fp3", component="emr-web")))
    assert b != a
    assert eng.ingest(from_alertmanager(alert(component="unknown-svc"))) == []
    assert len(svc.signals.for_incident(a)) == 3


def test_recovery_leads_to_a_postmortem_recorded_in_git(tmp_path, gov_root):
    eng, svc = make(tmp_path, gov_root)
    released_feature(eng)
    [iid] = eng.ingest(from_alertmanager(alert()))
    eng.decide(iid, gate(eng, iid, "incident")["interrupt_id"], {"decision": "approve", "approver": "oncall-1"})
    assert [p.get("wait") for p in eng.pending(iid)] == ["incident_recovery"]
    eng.ingest(from_alertmanager(alert(status="resolved")))
    pm = gate(eng, iid, "postmortem")
    assert "## Timeline" in pm["preview"] and "flag_off" in pm["preview"]
    eng.decide(iid, pm["interrupt_id"], {"decision": "approve", "approver": "oncall-1"})
    v = eng.view(iid)
    assert v["status"] == "closed"
    assert ("vrixel-emr/governance", f"vrixel/{iid}", f"incidents/{iid}.md") in svc.github.files
    # closed: a new firing of the same alert opens a new incident
    [again] = eng.ingest(from_alertmanager(alert()))
    assert again != iid
    items = list(svc.trackers["openproject"].items.values())
    bug = next(i for i in items if i["kind"] == "incident" and i["marker"] == iid)
    assert bug["status"] == "done" and any("flag_off by oncall-1" in c for c in bug["comments"])


def test_still_firing_after_the_window_goes_back_to_on_call(tmp_path, gov_root, monkeypatch):
    monkeypatch.setitem(incident_mod.RECOVERY_MINUTES, "sev1", -1)
    eng, _ = make(tmp_path, gov_root)
    released_feature(eng)
    [iid] = eng.ingest(from_alertmanager(alert()))
    eng.decide(iid, gate(eng, iid, "incident")["interrupt_id"], {"decision": "approve", "approver": "oncall-1"})
    eng.poke_now(iid)
    g = gate(eng, iid, "incident")
    assert any(e["kind"] == "still_firing" for e in eng.view(iid)["events"]) and g["firing"] == 1


def test_on_call_can_pick_actions_ask_again_or_close(tmp_path, gov_root):
    eng, svc = make(tmp_path, gov_root)
    fid = released_feature(eng)
    [iid] = eng.ingest(from_alertmanager(alert()))
    eng.decide(iid, gate(eng, iid, "incident")["interrupt_id"],
               {"decision": "revise", "approver": "oncall-1", "feedback": "Check the database first"})
    g = gate(eng, iid, "incident")
    assert "Check the database first" in g["preview"]
    eng.decide(iid, g["interrupt_id"], {"decision": "approve", "approver": "oncall-1", "action_ids": ["A1"]})
    v = eng.view(iid)
    assert [t["type"] for t in v["taken"]] == ["flag_off"] and not v["links"]  # no hotfix started
    assert svc.flags.get("EMR", eng.view(fid)["release"]["flag"]) is False

    [other] = eng.ingest(from_alertmanager(alert(fingerprint="z", component="emr-web")))
    eng.decide(other, gate(eng, other, "incident")["interrupt_id"],
               {"decision": "resolve", "approver": "oncall-1", "feedback": "False alarm: synthetic check"})
    assert gate(eng, other, "postmortem")


def test_with_no_recent_change_triage_proposes_a_rollback_by_a_person(tmp_path, gov_root):
    eng, svc = make(tmp_path, gov_root)
    [iid] = eng.ingest(from_alertmanager(alert(component="emr-web", fingerprint="w")))
    g = gate(eng, iid, "incident")
    assert [a["type"] for a in g["actions"]] == ["rollback"]
    eng.decide(iid, g["interrupt_id"], {"decision": "approve", "approver": "oncall-1"})
    assert svc.openchoreo.rollbacks[-1]["actor"] == "oncall-1"
    with pytest.raises(PermissionError):
        svc.openchoreo.rollback(component="emr-web", env="production", actor="agent", actor_type="agent")


def test_a_user_report_becomes_an_intent_change(tmp_path, gov_root):
    eng, _ = make(tmp_path, gov_root)
    released_feature(eng, "Consent form at registration")
    [iid] = eng.ingest(from_ticket({"product_id": "EMR", "title": "Front desk can't edit consent after saving",
                                    "description": "Staff expected to correct mistakes in the consent form"}))
    g = gate(eng, iid, "incident")
    assert [(a["type"], a["scope"]) for a in g["actions"]] == [("intent_change", "feature")]
    eng.decide(iid, g["interrupt_id"], {"decision": "approve", "approver": "po-1"})
    v = eng.view(iid)
    [(fid, kind)] = v["links"].items()
    assert kind == "intent change (feature)" and v["status"] == "postmortem"  # nothing to watch recover
    eng._pool.shutdown(wait=True)
    f = eng.view(fid)
    assert f["created_by"] == "po-1" and f"incident {iid}" in f["description"]
    assert [p["gate"] for p in f["pending"]] == ["g1"]


def test_agents_see_recent_incidents(tmp_path, gov_root):
    eng, svc = make(tmp_path, gov_root)
    released_feature(eng)
    eng.ingest(from_alertmanager(alert()))
    text = render(svc.knowledge.collect(svc.snapshot("EMR")))
    assert "## Recent production incidents" in text and "EMR-INC-1 (sev1" in text


# --------------------------------------------------------------------------- API


def test_webhooks_and_reporting_a_problem(tmp_path, gov_root, monkeypatch):
    eng, _ = make(tmp_path, gov_root)
    client = TestClient(create_app(eng, auth=AuthConfig(mode="dev", secret="x" * 40), web_dist=None))
    body = json.dumps(alert())
    assert client.post("/api/signals/alertmanager", content=body).status_code == 404  # not configured
    monkeypatch.setenv("VRIXEL_SIGNALS_TOKEN", "t0k")
    monkeypatch.setenv("VRIXEL_SENTRY_CLIENT_SECRET", "s3c")
    assert client.post("/api/signals/alertmanager", content=body, headers={"authorization": "Bearer no"}).status_code == 401
    r = client.post("/api/signals/alertmanager", content=body, headers={"authorization": "Bearer t0k"})
    assert r.status_code == 202 and r.json()["incidents"] == ["EMR-INC-1"]

    sentry = json.dumps({"action": "created", "data": {"issue": {"id": "7", "title": "Crash", "level": "fatal",
                                                                 "project": {"slug": "emr-web"}}}}).encode()
    sig = hmac.new(b"s3c", sentry, hashlib.sha256).hexdigest()
    assert client.post("/api/signals/sentry", content=sentry, headers={"sentry-hook-signature": "bad"}).status_code == 401
    r = client.post("/api/signals/sentry", content=sentry, headers={"sentry-hook-signature": sig})
    assert r.json()["incidents"] == ["EMR-INC-2"]

    client.post("/api/auth/dev-login", json={"login": "fd-1", "orgs": ["vrixel-emr"]})
    r = client.post("/api/incidents", json={"product_id": "EMR", "title": "Printer labels cut off", "description": "x"})
    assert r.status_code == 201 and r.json()["feature_id"] == "EMR-INC-3"
    rows = client.get("/api/incidents").json()
    assert [x["feature_id"] for x in rows] == ["EMR-INC-3", "EMR-INC-2", "EMR-INC-1"]
    assert all(x["feature_id"].startswith("EMR-FEAT") for x in client.get("/api/features").json())

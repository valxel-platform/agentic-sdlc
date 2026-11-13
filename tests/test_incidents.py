"""Production issues: signals in (Prometheus, Sentry, tickets), incidents triaged by agents,
mitigated by the tool where the remediation policy allows and otherwise decided by on-call, mitigations (flag off, rollback), expedited hotfixes, intent changes,
recovery, postmortems."""

import hashlib
import hmac
import json
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import InMemorySaver

from valxel_sdlc.api.app import create_app
from valxel_sdlc.config import load_settings
from valxel_sdlc.engine import FeatureEngine
from valxel_sdlc.knowledge import render
from valxel_sdlc.ops import incident as incident_mod
from valxel_sdlc.ops.signals import from_alertmanager, from_sentry, from_ticket, resolve_product
from valxel_sdlc.services import build_services
from valxel_sdlc.web.auth import AuthConfig

ROOT = Path(__file__).resolve().parents[1]
API = "acme-clinic/cln-intake-api"


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


def alert(status="firing", fingerprint="fp1", component="cln-intake-api", severity="critical", name="HighErrorRate",
          at="2026-11-03T21:00:00Z"):
    return {"alerts": [{"status": status, "fingerprint": fingerprint, "startsAt": at,
                        "labels": {"alertname": name, "severity": severity, "component": component},
                        "annotations": {"summary": f"{name} on {component}"}}]}


def gate(eng, tid, name):
    return next(p for p in eng.pending(tid) if p.get("gate") == name)


def released_feature(eng, title="Consent capture at intake"):
    fid = eng.create(product_id="CLN", title=title)
    assert eng.auto_drive(fid)["status"] == "done"
    return fid


# --------------------------------------------------------------------------- signals


def test_signals_are_normalized():
    [a] = from_alertmanager(alert(severity="warning"))
    assert (a["source"], a["status"], a["severity"], a["component"]) == ("prometheus", "firing", "sev3", "cln-intake-api")
    assert from_alertmanager(alert(status="resolved"))[0]["status"] == "resolved"
    [s] = from_sentry({"action": "created", "data": {"issue": {
        "id": "42", "title": "TypeError in consent form", "level": "error", "project": {"slug": "cln-web"},
        "web_url": "https://sentry.io/i/42", "tags": [["valxel_product", "CLN"]]}}}, "issue")
    assert (s["severity"], s["product_id"], s["component"], s["fingerprint"]) == ("sev2", "CLN", "cln-web", "sentry:42")
    [t] = from_ticket({"product_id": "CLN", "title": "Can't edit consent", "description": "x", "reporter": "fd-1"})
    assert t["source"] == "ticket" and t["severity"] == "sev3" and t["labels"]["reporter"] == "fd-1"
    assert from_ticket({"product_id": "CLN"}) == []


def test_signals_map_to_a_product_and_component(tmp_path, gov_root):
    _, svc = make(tmp_path, gov_root)
    [a] = from_alertmanager(alert())
    assert resolve_product(svc, a) == ("CLN", API)
    [b] = from_alertmanager(alert(component="nobody-knows-me"))
    assert resolve_product(svc, b) == ("", "")


# --------------------------------------------------------------------------- incidents


def test_an_alert_opens_an_incident_and_the_tool_turns_the_new_feature_off_and_starts_a_hotfix(tmp_path, gov_root):
    eng, svc = make(tmp_path, gov_root)
    fid = released_feature(eng)
    key = eng.view(fid)["release"]["flag"]
    assert svc.flags.get("CLN", key) is True

    [iid] = eng.ingest(from_alertmanager(alert()))
    assert iid == "CLN-INC-1"
    v = eng.view(iid)
    # The feature released an hour ago is the likely cause: the tool turned its flag off by itself
    # (platform/remediation.yaml), started the fix, and is watching it recover. Nobody was asked.
    assert svc.flags.get("CLN", key) is False
    assert [(t["type"], t["ok"], t["by"]) for t in v["taken"]] == [("flag_off", True, "agent:remediate"),
                                                                    ("hotfix", True, "agent:remediate")]
    assert v["status"] == "monitoring" and not [p for p in eng.pending(iid) if p["type"] == "gate"]
    assert [p.get("wait") for p in eng.pending(iid)] == ["incident_recovery"]
    assert any(e["kind"] == "mitigating" and fid in e["detail"] for e in v["events"])
    [hotfix] = [k for k, kind in v["links"].items() if kind == "hotfix"]
    eng._pool.shutdown(wait=True)  # the hotfix runs in the background
    h = eng.view(hotfix)
    assert {s["key"]: s["state"] for s in h["rail"]}["g4"] == "skipped"
    assert eng._state(hotfix)["track"] == "hotfix" and "Hotfix design" in eng.artifact(hotfix, "HLD")["content"]


def no_autonomy(gov_root, product="acme-clinic/cln-governance"):
    """The product turns the tool's own production actions off: on-call decides everything."""
    gov = gov_root / product / "product.yaml"
    gov.write_text(gov.read_text().replace("runtime:\n", "runtime:\n  remediation: {autonomous: false}\n", 1))


def test_with_autonomy_off_on_call_decides_before_anything_happens(tmp_path, gov_root):
    no_autonomy(gov_root)
    eng, svc = make(tmp_path, gov_root)
    fid = released_feature(eng)
    key = eng.view(fid)["release"]["flag"]
    [iid] = eng.ingest(from_alertmanager(alert()))
    g = gate(eng, iid, "incident")
    assert g["approvers"] == ["acme-clinic/release-managers"]  # on-call falls back to G6's approvers
    assert g["severity"] == "sev1" and g["suspects"][0]["feature_id"] == fid
    assert [a["type"] for a in g["actions"]] == ["flag_off", "hotfix"]
    assert svc.flags.get("CLN", key) is True  # nothing happens before a person decides
    assert any(e["kind"] == "not_paged" and e["node"] == "escalate" for e in eng.view(iid)["events"])
    eng.decide(iid, g["interrupt_id"], {"decision": "approve", "approver": "oncall-1"})
    v = eng.view(iid)
    assert svc.flags.get("CLN", key) is False and v["taken"][0]["by"] == "oncall-1"
    assert [t["ok"] for t in v["taken"]] == [True, True] and v["status"] == "monitoring"


def test_a_hotfix_starts_at_planning_and_on_call_approves_merge_and_release(tmp_path, gov_root):
    eng, svc = make(tmp_path, gov_root)
    gov = gov_root / "acme-clinic" / "cln-governance" / "product.yaml"
    gov.write_text(gov.read_text().replace("approvers:\n", "approvers:\n  oncall:\n  - acme-clinic/oncall\n", 1))
    incident = {"incident_id": "CLN-INC-9", "product_id": "CLN", "title": "5xx on intake", "severity": "sev2"}
    hid = eng.create(product_id="CLN", title="Hotfix: guard null consent", track="hotfix",
                     hotfix={**incident, "repo": API, "change": "guard null consent", "approved_by": "oncall-1"})
    seen = {}
    for _ in range(10):
        p = [x for x in eng.pending(hid) if x["type"] == "gate"]
        if not p:
            break
        seen[p[0]["gate"]] = p[0]["approvers"]
        eng.decide(hid, p[0]["interrupt_id"], {"decision": "approve", "approver": "oncall-1"})
    assert set(seen) <= {"g5", "g6"} and seen["g6"] == ["acme-clinic/oncall"]
    v = eng.auto_drive(hid)
    assert v["status"] == "done" and v["release"]["flag"] == "" and v["release"]["flag_on"] is None  # a fix has no flag of its own
    assert not [c for c in svc.catalog.lookup("CLN") if c.get("feature") == hid]  # a fix is not a capability


def test_signals_join_their_open_incident(tmp_path, gov_root):
    eng, svc = make(tmp_path, gov_root)
    [a] = eng.ingest(from_alertmanager(alert()))
    assert eng.ingest(from_alertmanager(alert())) == [a]  # same alert
    assert eng.ingest(from_alertmanager(alert(fingerprint="fp2", name="HighLatency"))) == [a]  # same component
    # The web app calls the API (consumer and provider): failing together, it's one incident.
    assert eng.ingest(from_alertmanager(alert(fingerprint="fp3", component="cln-web"))) == [a]
    joined = svc.signals.for_incident(a)[-1]
    assert joined["labels"]["joined_by"].startswith("calls: cln-web calls cln-intake-api")
    assert eng.ingest(from_alertmanager(alert(component="unknown-svc"))) == []
    assert len(svc.signals.for_incident(a)) == 4


def later(monkeypatch, minutes):
    """The tool's clock, `minutes` from now."""
    from datetime import datetime, timedelta, timezone

    monkeypatch.setattr(incident_mod, "_clock", lambda: datetime.now(timezone.utc) + timedelta(minutes=minutes))


def test_recovery_leads_to_a_postmortem_recorded_in_git(tmp_path, gov_root, monkeypatch):
    eng, svc = make(tmp_path, gov_root)
    released_feature(eng)
    [iid] = eng.ingest(from_alertmanager(alert()))  # the tool turns the feature off
    assert [p.get("wait") for p in eng.pending(iid)] == ["incident_recovery"]
    eng.ingest(from_alertmanager(alert(status="resolved")))
    assert [p.get("wait") for p in eng.pending(iid)] == ["incident_recovery"]  # resolved, but not quiet long enough
    later(monkeypatch, 11)  # SEV1: quiet for 10 minutes
    eng.poke_now(iid)
    pm = gate(eng, iid, "postmortem")
    assert "## Timeline" in pm["preview"] and "flag_off" in pm["preview"]
    assert any(e["kind"] == "recovered" and "quiet for 10 minutes" in e["detail"] for e in eng.view(iid)["events"])
    eng.decide(iid, pm["interrupt_id"], {"decision": "approve", "approver": "oncall-1"})
    v = eng.view(iid)
    assert v["status"] == "closed"
    assert ("acme-clinic/cln-governance", f"valxel/{iid}", f"incidents/{iid}.md") in svc.github.files
    # closed: a new firing of the same alert opens a new incident
    [again] = eng.ingest(from_alertmanager(alert()))
    assert again != iid
    items = list(svc.trackers["openproject"].items.values())
    bug = next(i for i in items if i["kind"] == "incident" and i["marker"] == iid)
    assert bug["status"] == "done" and any("flag_off by agent:remediate" in c for c in bug["comments"])


def test_still_firing_after_the_window_goes_back_to_on_call(tmp_path, gov_root, monkeypatch):
    no_autonomy(gov_root)
    monkeypatch.setitem(incident_mod.RECOVERY_MINUTES, "sev1", -1)
    eng, _ = make(tmp_path, gov_root)
    released_feature(eng)
    [iid] = eng.ingest(from_alertmanager(alert()))
    eng.decide(iid, gate(eng, iid, "incident")["interrupt_id"], {"decision": "approve", "approver": "oncall-1"})
    eng.poke_now(iid)
    g = gate(eng, iid, "incident")
    assert any(e["kind"] == "still_firing" for e in eng.view(iid)["events"]) and g["firing"] == 1


def test_on_call_can_pick_actions_ask_again_or_close(tmp_path, gov_root):
    no_autonomy(gov_root)
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
    assert svc.flags.get("CLN", eng.view(fid)["release"]["flag"]) is False

    # Hours later, the web app failing on its own is another incident.
    [other] = eng.ingest(from_alertmanager(alert(fingerprint="z", component="cln-web", at="2026-11-04T02:00:00Z")))
    assert other != iid
    eng.decide(other, gate(eng, other, "incident")["interrupt_id"],
               {"decision": "resolve", "approver": "oncall-1", "feedback": "False alarm: synthetic check"})
    assert gate(eng, other, "postmortem")


def test_with_no_recent_change_triage_proposes_a_rollback_by_a_person(tmp_path, gov_root):
    eng, svc = make(tmp_path, gov_root)
    [iid] = eng.ingest(from_alertmanager(alert(component="cln-web", fingerprint="w")))
    g = gate(eng, iid, "incident")
    assert [a["type"] for a in g["actions"]] == ["rollback"]
    eng.decide(iid, g["interrupt_id"], {"decision": "approve", "approver": "oncall-1"})
    assert svc.openchoreo.rollbacks[-1]["actor"] == "oncall-1"
    with pytest.raises(PermissionError):
        svc.openchoreo.rollback(component="cln-web", env="production", actor="agent", actor_type="agent")


def test_a_user_report_becomes_an_intent_change(tmp_path, gov_root):
    eng, _ = make(tmp_path, gov_root)
    released_feature(eng, "Consent form at registration")
    [iid] = eng.ingest(from_ticket({"product_id": "CLN", "title": "Front desk can't edit consent after saving",
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
    text = render(svc.knowledge.collect(svc.snapshot("CLN")))
    assert "## Recent production incidents" in text and "CLN-INC-1 (sev1" in text


# --------------------------------------------------------------------------- API


def test_webhooks_and_reporting_a_problem(tmp_path, gov_root, monkeypatch):
    eng, _ = make(tmp_path, gov_root)
    client = TestClient(create_app(eng, auth=AuthConfig(mode="dev", secret="x" * 40), web_dist=None))
    body = json.dumps(alert())
    assert client.post("/api/signals/alertmanager", content=body).status_code == 404  # not configured
    monkeypatch.setenv("VALXEL_SIGNALS_TOKEN", "t0k")
    monkeypatch.setenv("VALXEL_SENTRY_CLIENT_SECRET", "s3c")
    assert client.post("/api/signals/alertmanager", content=body, headers={"authorization": "Bearer no"}).status_code == 401
    r = client.post("/api/signals/alertmanager", content=body, headers={"authorization": "Bearer t0k"})
    assert r.status_code == 202 and r.json()["incidents"] == ["CLN-INC-1"]

    sentry = json.dumps({"action": "created", "data": {"issue": {"id": "7", "title": "Crash", "level": "fatal",
                                                                 "project": {"slug": "cln-web"}}}}).encode()
    sig = hmac.new(b"s3c", sentry, hashlib.sha256).hexdigest()
    assert client.post("/api/signals/sentry", content=sentry, headers={"sentry-hook-signature": "bad"}).status_code == 401
    r = client.post("/api/signals/sentry", content=sentry, headers={"sentry-hook-signature": sig})
    assert r.json()["incidents"] == ["CLN-INC-2"]

    client.post("/api/auth/dev-login", json={"login": "fd-1", "orgs": ["acme-clinic"]})
    r = client.post("/api/incidents", json={"product_id": "CLN", "title": "Printer labels cut off", "description": "x"})
    assert r.status_code == 201 and r.json()["feature_id"] == "CLN-INC-3"
    rows = client.get("/api/incidents").json()
    assert [x["feature_id"] for x in rows] == ["CLN-INC-3", "CLN-INC-2", "CLN-INC-1"]
    assert all(x["feature_id"].startswith("CLN-FEAT") for x in client.get("/api/features").json())


def test_a_signal_for_no_product_is_kept_with_why_for_the_platform_team(tmp_path, gov_root, monkeypatch):
    """Not dropped: a mislabelled alert or an unregistered component shows up where the platform
    team can fix its source."""
    from valxel_sdlc.ops.signals import from_openchoreo

    eng, _ = make(tmp_path, gov_root)
    assert eng.ingest(from_alertmanager(alert(component="billing-cron"))) == []
    assert eng.ingest(from_alertmanager({"alerts": [{"status": "firing", "fingerprint": "x", "labels": {
        "alertname": "Down", "valxel_product": "NOPE"}}]})) == []
    assert eng.ingest(from_openchoreo({"alertName": "errors", "namespace": "default", "project": "ghost",
                                       "component": "", "environment": "production"})) == []
    assert eng.ingest(from_ticket({"title": "Something broke"})) == []
    got = eng.unmatched_signals()
    assert [s["reason"] for s in got] == [
        "it names no product, component or OpenChoreo project",
        "OpenChoreo project ghost belongs to no product",
        "it names product NOPE, which isn't in the registry",
        "component billing-cron matches no product's repos or OpenChoreo components",
    ]
    assert got[-1]["title"] == "HighErrorRate on billing-cron" and got[-1]["received_at"]
    # A signal that does match still opens its incident, and isn't listed.
    assert eng.ingest(from_alertmanager(alert())) == ["CLN-INC-1"]
    assert len(eng.unmatched_signals()) == 4

    client = TestClient(create_app(eng, auth=AuthConfig(mode="dev", secret="x" * 40), web_dist=None))
    assert client.get("/api/signals/unmatched").status_code == 401
    client.post("/api/auth/dev-login", json={"login": "maria", "orgs": ["acme-clinic"]})
    assert client.get("/api/signals/unmatched").status_code == 403  # could be any product's: platform team only
    client.post("/api/auth/dev-login", json={"login": "platform-owner", "orgs": ["valxel-platform"]})
    r = client.get("/api/signals/unmatched?limit=2").json()
    assert r["count"] == 2 and r["signals"][0]["source"] == "ticket"
    assert set(r["signals"][1]) >= {"title", "reason", "received_at", "fingerprint"}

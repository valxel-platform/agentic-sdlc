"""The tool resolves incidents by itself where the platform's remediation policy allows it: it turns
off a recent feature's flag or rolls back a recent release, checks it has gone quiet, starts the fix,
and pages on-call urgently when it can't."""

import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from valxel_sdlc.config import load_settings
from valxel_sdlc.engine import FeatureEngine
from valxel_sdlc.governance.loader import GovernanceError
from valxel_sdlc.ops import incident as incident_mod
from valxel_sdlc.ops import paging, remediation
from valxel_sdlc.ops.signals import from_alertmanager, from_openchoreo
from valxel_sdlc.services import build_services

EXAMPLES = Path(__file__).resolve().parents[1] / "examples" / "governance"
POLICY = EXAMPLES / "valxel-platform" / "sdlc-governance" / "platform" / "remediation.yaml"
CLN = "acme-clinic/cln-governance"
NOW = datetime(2026, 11, 6, 22, 0, tzinfo=timezone.utc)


def ago(hours):
    return (NOW - timedelta(hours=hours)).isoformat()


# --------------------------------------------------------------------------- the policy

def test_the_platform_policy_and_a_products_narrowing():
    pol = remediation.load_policy(POLICY.read_text())
    assert pol["autonomous"] == {"flag_off": {"released_within_hours": 72.0}, "rollback": {"released_within_hours": 24.0}}
    assert pol["attempts"] == 1 and pol["quiet_minutes"]["sev1"] == 10 and pol["page"]["heads_up"] == ["sev1"]
    assert remediation.for_product(pol, {"remediation": {"autonomous": False}})["autonomous"] == {}
    assert set(remediation.for_product(pol, {"remediation": {"autonomous": ["flag_off"]}})["autonomous"]) == {"flag_off"}
    assert set(remediation.for_product(pol, None)["autonomous"]) == {"flag_off", "rollback"}
    assert remediation.for_product({}, None)["autonomous"] == {}  # no platform file: no autonomy
    assert remediation.check({"remediation": {"autonomous": ["deploy"]}})
    assert remediation.check({"remediation": {"autonomous": ["rollback"]}}) == []


@pytest.mark.parametrize("bad", [
    "autonomous: {deploy: {}}",
    "autonomous: {rollback: {released_within_hours: 0}}",
    "autonomous: {}\nattempts: 9",
    "autonomous: {}\nquiet_minutes: {sev1: 1}",
    "autonomous: {}\npage: {heads_up: [p1]}",
    "- a list",
])
def test_a_bad_policy_is_refused(bad):
    with pytest.raises(remediation.PolicyError):
        remediation.load_policy(bad)


def test_the_tool_only_undoes_recent_changes():
    pol = remediation.load_policy(POLICY.read_text())
    flagged = [{"feature_id": "F1", "flag": "f1", "flag_on": True, "released_at": ago(2)}]
    flag_off = {"id": "A1", "type": "flag_off", "feature_id": "F1"}
    rollback = {"id": "A2", "type": "rollback", "component": "org/api"}
    hotfix = {"id": "A3", "type": "hotfix", "repo": "org/api", "change": "x"}
    acts = [flag_off, rollback, hotfix]
    assert remediation.choose(acts, lambda _: flagged, "org/api", pol, NOW) == ([flag_off, hotfix], "")
    # The flag is already off, or the feature is too old to blame: roll back the recent release instead.
    off = [{**flagged[0], "flag_on": False}]
    assert remediation.choose(acts, lambda _: off, "org/api", pol, NOW)[0][0]["type"] == "rollback"
    assert remediation.choose(acts, lambda _: [{**flagged[0], "released_at": ago(30)}], "org/api", pol,
                              NOW)[0][0] == flag_off  # a flag can go off for 3 days
    assert remediation.choose(acts, lambda _: [{**off[0], "released_at": ago(30)}], "org/api", pol,
                              NOW)[0] == []  # a release is rolled back only within a day
    old = [{**flagged[0], "released_at": ago(100)}]
    chosen, why = remediation.choose(acts, lambda _: old, "org/api", pol, NOW)
    assert chosen == [] and "only for a change released recently" in why
    assert remediation.choose([hotfix], lambda _: flagged, "org/api", pol, NOW)[0] == []  # a fix is a person's call
    no_hotfix = {**pol, "start_hotfix": False}
    assert remediation.choose(acts, lambda _: flagged, "org/api", no_hotfix, NOW)[0] == [flag_off]
    off_policy = remediation.for_product(pol, {"remediation": {"autonomous": False}})
    assert "no action by itself" in remediation.choose(acts, lambda _: flagged, "org/api", off_policy, NOW)[1]


def test_what_never_pages():
    cost = {"source": "aws-budgets", "category": "cost"}
    ticket = {"source": "ticket"}
    alert = {"source": "alertmanager"}
    assert "cost finding" in remediation.no_page_reason([cost], [])
    assert "users asked for a change" in remediation.no_page_reason([ticket], [{"type": "intent_change"}])
    assert remediation.no_page_reason([ticket], [{"type": "rollback"}]) == ""
    assert remediation.no_page_reason([cost, alert], [{"type": "monitor"}]) == ""


# --------------------------------------------------------------------------- recovery

def sig(at, source="openchoreo", status="firing", resolves=False, title="exceptions"):
    return {"source": source, "status": status, "resolves": resolves, "fingerprint": f"{source}:{title}",
            "title": title, "at": at.isoformat()}


def test_quiet_means_no_new_alerts_and_errors_back_down(monkeypatch):
    since = NOW
    before = sig(since - timedelta(minutes=3))
    clock = {"t": since + timedelta(minutes=4)}
    monkeypatch.setattr(incident_mod, "_clock", lambda: clock["t"])
    r = incident_mod.recovery
    assert r([before], since.isoformat(), 30, quiet_minutes=10) is None  # too early to say
    clock["t"] = since + timedelta(minutes=11)
    assert r([before], since.isoformat(), 30, quiet_minutes=10)["status"] == "success"
    # Re-fired within the alert rule's 5-minute look-back: old errors, not a new failure.
    echo = sig(since + timedelta(minutes=2))
    assert r([before, echo], since.isoformat(), 30, quiet_minutes=10)["status"] == "success"
    again = sig(since + timedelta(minutes=8), title="exception surge")
    out = r([before, again], since.isoformat(), 30, quiet_minutes=10)
    assert out["status"] == "failure" and "exception surge" in out["reason"]
    out = r([before], since.isoformat(), 30, quiet_minutes=10, errors=lambda: 25)
    assert out["status"] == "failure" and "25 errors" in out["reason"]
    assert r([before], since.isoformat(), 30, quiet_minutes=10, errors=lambda: 3)["status"] == "success"
    assert r([before], since.isoformat(), 30, quiet_minutes=10, errors=lambda: None)["status"] == "success"
    # An alert that reports its own resolution must have resolved too.
    am = sig(since - timedelta(minutes=3), source="alertmanager", resolves=True, title="HighErrorRate")
    assert r([before, am], since.isoformat(), 30, quiet_minutes=10) is None
    clock["t"] = since + timedelta(minutes=31)
    assert r([before, am], since.isoformat(), 30, quiet_minutes=10)["status"] == "failure"


# --------------------------------------------------------------------------- the flow

@pytest.fixture
def root(tmp_path):
    r = tmp_path / "governance"
    shutil.copytree(EXAMPLES, r)
    return r


def make(tmp_path, root):
    s = load_settings(mode="stub", database_url="", artifact_dir=tmp_path / "a", governance_source="local",
                      governance_root=root)
    svc = build_services(s)
    svc.pager = paging.RecordingPager({"CLN": "cln-key", "FARM": "farm-key"}, "https://sdlc")
    return FeatureEngine(svc, InMemorySaver()), svc


def alert(status="firing", component="cln-intake-api", severity="error", fp="fp1", at="2026-11-06T21:00:00Z"):
    return from_alertmanager({"alerts": [{"status": status, "fingerprint": fp, "startsAt": at,
                                          "labels": {"alertname": "HighErrorRate", "severity": severity,
                                                     "component": component}}]})


def gate(eng, iid, name="incident"):
    return next(p for p in eng.pending(iid) if p.get("gate") == name)


def urgent(svc):
    return [s for s in svc.pager.sent if s["dedup_key"].endswith(":urgent") and s["event_action"] == "trigger"]


def test_a_failed_mitigation_pages_on_call(tmp_path, root):
    eng, svc = make(tmp_path, root)
    fid = eng.create(product_id="CLN", title="Consent capture at intake")
    eng.auto_drive(fid)

    def broken(*a, **k):
        raise RuntimeError("Flipt is unreachable")

    svc.flags.set = broken
    [iid] = eng.ingest(alert())
    v = eng.view(iid)
    assert [(t["type"], t["ok"]) for t in v["taken"]][:1] == [("flag_off", False)]
    [page] = urgent(svc)
    assert "the tool's flag off failed" in page["payload"]["summary"]
    assert gate(eng, iid)  # on-call decides now


def test_still_failing_after_the_tools_rollback_pages_on_call(tmp_path, root, monkeypatch):
    gov = root / CLN / "product.yaml"
    gov.write_text(gov.read_text().replace("runtime:\n", "runtime:\n  remediation: {autonomous: [rollback]}\n", 1))
    eng, svc = make(tmp_path, root)
    fid = eng.create(product_id="CLN", title="Consent capture at intake")
    eng.auto_drive(fid)
    oc = {"alertName": "cln-intake-api-exceptions", "alertSeverity": "warning", "alertType": "log",
          "project": "cln", "component": "cln-intake-api", "environment": "production",
          "alertTimestamp": datetime.now(timezone.utc).isoformat()}
    [iid] = eng.alarm_changes(from_openchoreo(oc))["incidents"]
    v = eng.view(iid)
    assert v["taken"][0]["type"] == "rollback" and v["taken"][0]["by"] == "agent:remediate"
    [rb] = svc.openchoreo.rollbacks
    assert rb["actor"] == "agent:remediate" and rb["env"] == "production"
    assert svc.flags.get("CLN", eng.view(fid)["release"]["flag"]) is True  # flag_off isn't this product's
    # The rule fires again 20 minutes later; at 31 (SEV3 needs 30 quiet minutes) the tool gives up.
    refired = datetime.now(timezone.utc) + timedelta(minutes=20)
    monkeypatch.setattr(incident_mod, "_clock", lambda: refired)
    eng.alarm_changes(from_openchoreo({**oc, "alertTimestamp": refired.isoformat()}))
    assert urgent(svc) == []  # not quiet yet, but too early to say
    monkeypatch.setattr(incident_mod, "_clock", lambda: refired + timedelta(minutes=11))
    eng.poke_now(iid)
    [page] = urgent(svc)
    assert page["payload"]["severity"] == "error" and "still failing after the tool's mitigation" in page["payload"]["summary"]
    g = gate(eng, iid)
    assert len(svc.openchoreo.rollbacks) == 1  # one try, then a person
    eng.decide(iid, g["interrupt_id"], {"decision": "approve", "approver": "oncall-1", "action_ids": []})
    assert ("CLN-INC-1:urgent", "acknowledge") in [(s["dedup_key"], s["event_action"]) for s in svc.pager.sent]


def test_a_quiet_incident_after_the_tools_fix_reaches_the_postmortem_without_a_page(tmp_path, root, monkeypatch):
    eng, svc = make(tmp_path, root)
    fid = eng.create(product_id="CLN", title="Consent capture at intake")
    eng.auto_drive(fid)
    [iid] = eng.ingest(alert())  # SEV2
    monkeypatch.setattr(incident_mod, "_clock", lambda: datetime.now(timezone.utc) + timedelta(minutes=16))
    eng.ingest(alert(status="resolved"))
    pm = gate(eng, iid, "postmortem")
    assert "flag_off" in pm["preview"] and svc.pager.sent == []


def test_a_merge_ends_an_incident_the_tool_is_watching(tmp_path, root):
    eng, svc = make(tmp_path, root)
    fid = eng.create(product_id="CLN", title="Consent capture at intake")
    eng.auto_drive(fid)
    [a] = eng.ingest(alert())
    [b] = eng.ingest(alert(component="cln-web", fp="w", at="2026-11-07T09:00:00Z"))
    assert a != b and [p.get("wait") for p in eng.pending(a)] == ["incident_recovery"]
    out = eng.merge_incidents(a, b, approver="oncall-1", reason="same outage")
    assert out["ended"] is True and eng._state(a)["status"] == "merged" and not eng.pending(a)


def test_a_cost_finding_waits_for_on_call_without_paging(tmp_path, root):
    eng, svc = make(tmp_path, root)
    budget = {"alertName": "farm-budget", "alertSeverity": "critical", "alertType": "budget", "alertValue": 212.4,
              "alertThreshold": 150, "project": "farm", "environment": "production"}
    [iid] = eng.alarm_changes(from_openchoreo(budget))["incidents"]
    assert gate(eng, iid) and svc.pager.sent == []
    ev = [e for e in eng.view(iid)["events"] if e["node"] == "escalate"]
    assert ev and ev[0]["kind"] == "not_paged" and "cost finding" in ev[0]["detail"]


def test_governance_refuses_a_bad_remediation_setting(tmp_path, root):
    gov = root / CLN / "product.yaml"
    gov.write_text(gov.read_text().replace("runtime:\n", "runtime:\n  remediation: {autonomous: [deploy]}\n", 1))
    eng, _ = make(tmp_path, root)
    with pytest.raises(GovernanceError, match="runtime.remediation.autonomous"):
        eng.services.snapshot("CLN")

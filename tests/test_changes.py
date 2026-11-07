"""Change management: every production change is one hash-chained record (standard, emergency or
automatic), an incident's mitigation names the release that caused it, and the lookback counts
incidents, how they ended and the change failure rate."""

import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import InMemorySaver

from vrixel_sdlc.api.app import create_app
from vrixel_sdlc.cli import _verify_changes
from vrixel_sdlc.config import load_settings
from vrixel_sdlc.engine import FeatureEngine
from vrixel_sdlc.ops import changes, incident as incident_mod, lookback
from vrixel_sdlc.ops.signals import from_alertmanager
from vrixel_sdlc.services import build_services
from vrixel_sdlc.web.auth import AuthConfig

EXAMPLES = Path(__file__).resolve().parents[1] / "examples" / "governance"


def rec(key, product="CLN", **kw):
    return changes.new(key=key, product_id=product, kind=kw.pop("kind", "release"), type=kw.pop("type", "standard"),
                       action=kw.pop("action", "deploy"), summary=kw.pop("summary", key), targets=[], **kw)


def test_the_log_is_append_only_and_tampering_shows():
    log = changes.InMemoryChangeLog()
    a, b = log.record(rec("a")), log.record(rec("b"))
    assert (a["id"], b["id"]) == ("CHG-1", "CHG-2") and b["prev_hash"] == a["hash"]
    assert log.record(rec("a", summary="again"))["summary"] == "a"  # the same change twice: recorded once
    assert changes.verify(log.all()) == {"ok": True, "records": 2, "broken_at": None}
    log.rows[0]["approved_by"] = ["someone-else"]  # rewriting history
    assert changes.verify(log.all()) == {"ok": False, "records": 2, "broken_at": "CHG-1"}
    log.rows[0]["approved_by"] = []
    del log.rows[0]  # removing a record
    assert changes.verify(log.all())["broken_at"] == "CHG-2"
    with pytest.raises(ValueError):
        changes.new(key="x", product_id="CLN", kind="deploy", type="standard", action="x", summary="", targets=[])


def test_the_cli_says_whether_the_log_is_intact(capsys):
    log = changes.InMemoryChangeLog()
    log.record(rec("a"))
    assert _verify_changes(log) == 0 and "intact: 1 records" in capsys.readouterr().out
    log.rows[0]["summary"] = "edited"
    assert _verify_changes(log) == 1 and "breaks at CHG-1" in capsys.readouterr().err


@pytest.fixture
def eng(tmp_path):
    root = tmp_path / "governance"
    shutil.copytree(EXAMPLES, root)
    s = load_settings(mode="stub", database_url="", artifact_dir=tmp_path / "a", governance_source="local",
                      governance_root=root)
    return FeatureEngine(build_services(s), InMemorySaver())


def alert(status="firing", component="cln-intake-api", fp="fp1", at="2026-11-06T21:00:00Z", severity="error"):
    return from_alertmanager({"alerts": [{"status": status, "fingerprint": fp, "startsAt": at,
                                          "labels": {"alertname": "HighErrorRate", "severity": severity,
                                                     "component": component}}]})


def test_a_release_records_who_approved_it_and_the_evidence(eng):
    fid = eng.create(product_id="CLN", title="Consent capture at intake")
    eng.auto_drive(fid)
    rows = eng.services.changes.list("CLN")
    [r] = [x for x in rows if x["kind"] == "release"]
    assert (r["kind"], r["type"], r["action"], r["feature_id"]) == ("release", "standard", "deploy", fid)
    flag = next(x for x in r["targets"] if x["kind"] == "flag")
    assert flag["percent"] == 5.0 and r["evidence"]["gradual_release"]["steps"] == [5.0, 25.0, 50.0, 100.0]
    # G6 approved the plan: each step up is its own record, approved by G6, made by the tool.
    steps = [x for x in rows if x["action"] == "rollout_step"]
    assert [x["targets"][0]["percent"] for x in steps] == [25.0, 50.0, 100.0]
    assert all(x["approved_by"] == r["approved_by"] and x["requested_by"] == "agent:rollout" for x in steps)
    assert [a["gate"] for a in r["approvals"]] == ["g1", "g2", "g3", "g4", "g5", "g6"]
    assert {t["kind"] for t in r["targets"]} >= {"component", "flag"}
    assert r["evidence"]["pull_requests"] and r["requested_by"] == eng._state(fid).get("created_by", "")
    assert any(e["kind"] == "change_recorded" and e["detail"].startswith("CHG-1") for e in eng._state(fid)["events"])


def test_the_tools_fix_is_automatic_and_names_the_release_behind_the_incident(eng, monkeypatch):
    fid = eng.create(product_id="CLN", title="Consent capture at intake")
    eng.auto_drive(fid)
    [iid] = eng.ingest(alert())
    eng._pool.shutdown(wait=True)  # the hotfix the tool started runs in the background
    rows = eng.services.changes.list("CLN")
    flag = next(r for r in rows if r["action"] == "flag_off")
    assert (flag["type"], flag["action"], flag["incident_id"]) == ("automatic", "flag_off", iid)
    assert flag["approved_by"] == ["platform/remediation.yaml"] and flag["requested_by"] == "agent:remediate"
    [link] = eng.services.changes.links("CLN")
    assert (link["change_id"], link["incident_id"]) == ("CHG-1", iid) and "undone by" in link["why"]
    assert any(e["kind"] == "cause" and "CHG-1" in e["detail"] for e in eng.view(iid)["events"])

    # Recovered: the lookback counts it as fixed by the tool, and the release as a failed change.
    monkeypatch.setattr(incident_mod, "_clock", lambda: datetime.now(timezone.utc) + timedelta(minutes=16))
    eng.ingest(alert(status="resolved"))
    month = eng.lookback("CLN", 1)["months"][-1]
    assert month["incidents"] == 1 and month["fixed_by_tool"] == 1 and month["escalated"] == 0
    assert month["releases"] == 1 and month["releases_causing_incidents"] == 1 and month["change_failure_rate"] == 1.0
    assert month["changes"] == {"standard": 4, "automatic": 1}  # the release, its 3 steps, the flag off
    assert eng.lookback("CLN", 1)["causes"][0]["incidents"] == [iid]


def test_on_calls_rollback_is_an_emergency_change_and_a_hotfix_release_too(tmp_path):
    root = tmp_path / "gov"
    shutil.copytree(EXAMPLES, root)
    gov = root / "acme-clinic" / "cln-governance" / "product.yaml"
    gov.write_text(gov.read_text().replace("runtime:\n", "runtime:\n  remediation: {autonomous: false}\n", 1))
    s = load_settings(mode="stub", database_url="", artifact_dir=tmp_path / "a", governance_source="local",
                      governance_root=root)
    eng = FeatureEngine(build_services(s), InMemorySaver())
    eng.auto_drive(eng.create(product_id="CLN", title="Consent capture at intake"))
    [iid] = eng.ingest(alert())
    g = next(p for p in eng.pending(iid) if p.get("gate") == "incident")
    eng.decide(iid, g["interrupt_id"], {"decision": "approve", "approver": "oncall-1"})
    hotfix = next(k for k, v in eng.view(iid)["links"].items() if v == "hotfix")
    eng._pool.shutdown(wait=True)
    eng.auto_drive(hotfix)
    rows = eng.services.changes.list("CLN")
    off = next(r for r in rows if r["action"] == "flag_off")
    assert off["type"] == "emergency" and off["approved_by"] == ["oncall-1"]
    fix = next(r for r in rows if r["feature_id"] == hotfix)
    assert fix["type"] == "emergency" and fix["incident_id"] == iid
    assert changes.verify(eng.services.changes.all())["ok"]


def test_the_lookback_by_month():
    def inc(i, opened, **kw):
        return {"incident_id": f"CLN-INC-{i}", "opened_at": opened, "severity": kw.pop("sev", "sev2"),
                "component": "acme-clinic/cln-intake-api", "status": kw.pop("status", "closed"), **kw}

    recovered = [{"kind": "recovered", "at": "2026-10-03T10:30:00+00:00"}]
    incidents = [
        inc(1, "2026-10-03T10:00:00+00:00", events=recovered, taken=[{"by": "agent:remediate", "ok": True}]),
        inc(2, "2026-10-05T10:00:00+00:00", escalated=True, sev="sev1",
            events=[{"kind": "resolved", "at": "2026-10-05T12:00:00+00:00"}]),
        inc(3, "2026-11-01T10:00:00+00:00", status="monitoring"),
        inc(4, "2026-10-06T10:00:00+00:00", status="merged"),
        inc(5, "2025-01-01T10:00:00+00:00"),  # outside the window
    ]
    chg = [{"id": "CHG-1", "at": "2026-10-02T09:00:00+00:00", "kind": "release", "action": "deploy", "type": "standard",
            "outcome": "done", "summary": "CLN-FEAT-1"},
           {"id": "CHG-2", "at": "2026-10-03T10:05:00+00:00", "kind": "flag", "action": "flag_off", "type": "automatic",
            "outcome": "done", "summary": "off"},
           {"id": "CHG-3", "at": "2026-11-02T09:00:00+00:00", "kind": "release", "action": "deploy", "type": "emergency",
            "outcome": "failed", "summary": "CLN-FEAT-2"}]
    links = [{"change_id": "CHG-1", "incident_id": "CLN-INC-1"}]
    out = lookback.report(incidents, chg, links, ["2026-10", "2026-11"])
    oct_, nov = out["months"]
    assert oct_["incidents"] == 2 and oct_["by_severity"] == {"sev1": 1, "sev2": 1}
    assert (oct_["fixed_by_tool"], oct_["escalated"]) == (1, 1)
    assert oct_["recover_minutes_median"] == 75.0 and oct_["recover_minutes_max"] == 120.0
    assert oct_["change_failure_rate"] == 1.0 and oct_["changes"] == {"standard": 1, "automatic": 1}
    assert nov["open"] == 1 and nov["failed_changes"] == 1 and nov["change_failure_rate"] == 0.0
    assert out["total"]["change_failure_rate"] == 0.5 and out["causes"][0]["change_id"] == "CHG-1"
    assert lookback.months_back(3, datetime(2026, 1, 15, tzinfo=timezone.utc)) == ["2025-11", "2025-12", "2026-01"]


def test_the_api_shows_members_their_products_changes(eng):
    eng.auto_drive(eng.create(product_id="CLN", title="Consent capture at intake"))
    eng.auto_drive(eng.create(product_id="FARM", title="Field log"))
    client = TestClient(create_app(eng, auth=AuthConfig(mode="dev", secret="x" * 40), web_dist=None))
    client.post("/api/auth/dev-login", json={"login": "dev", "orgs": ["acme-clinic"]})
    rows = client.get("/api/changes").json()
    assert rows and {r["product_id"] for r in rows} == {"CLN"}  # not FARM's
    assert client.get("/api/changes?product_id=FARM").status_code == 403
    assert client.get("/api/changes?since=yesterday").status_code == 400
    csv = client.get("/api/changes/export.csv?product_id=CLN")
    assert csv.status_code == 200 and csv.text.splitlines()[0].startswith("id,at,product")
    assert "CLN-FEAT-1" in csv.text and "FARM-" not in csv.text
    assert client.get("/api/changes/verify").json()["ok"] is True
    lb = client.get("/api/reports/lookback?product_id=CLN&months=3").json()
    assert len(lb["months"]) == 3 and lb["months"][-1]["releases"] == 1
    assert client.get("/api/reports/lookback?product_id=CLN&months=99").status_code == 400

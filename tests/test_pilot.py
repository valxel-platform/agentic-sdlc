"""The pilot: people's and agents' effort per feature, the report against the 15% target, and the
shadow modes (automatic approval, a cheaper critic and reviewer) recorded beside real decisions."""

import shutil
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import InMemorySaver

from vrixel_sdlc import cli, pilot
from vrixel_sdlc.api.app import create_app, record_review_effort
from vrixel_sdlc.config import load_settings
from vrixel_sdlc.engine import FeatureEngine
from vrixel_sdlc.ops.signals import from_alertmanager
from vrixel_sdlc.services import build_services
from vrixel_sdlc.web.auth import AuthConfig

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def eng(tmp_path):
    root = tmp_path / "governance"
    shutil.copytree(ROOT / "examples" / "governance", root)
    s = load_settings(mode="stub", database_url="", artifact_dir=tmp_path / "a",
                      governance_source="local", governance_root=root)
    return FeatureEngine(build_services(s), InMemorySaver())


def run(eng, pilot_on=True, **decisions):
    fid = eng.create(product_id="FARM", title="Offline crop log", pilot=pilot_on)
    return fid, eng.auto_drive(fid, approver="maria", decisions=decisions or None)


def test_a_pilot_feature_records_why_work_came_back_and_how_long_people_were_asked(eng):
    fid, v = run(eng, g3={"decision": "revise", "feedback": "add the offline banner",
                          "reason": "missing_requirement", "active_minutes": 7})
    assert v["status"] == "done"
    g3 = [d for d in v["gate_decisions"] if d["gate"] == "g3"]
    assert g3[0]["decision"] == "revise" and g3[0]["reason"] == "missing_requirement"
    assert all(d.get("waited_minutes") is not None for d in v["gate_decisions"] if d["gate"].startswith("g"))
    asked = [e for e in eng.services.effort.events(fid) if e["kind"] == "asked"]
    assert {e["detail"]["gate"] for e in asked} >= {"g1", "g2", "g3", "g4", "g6"}


def test_the_shadow_modes_are_recorded_beside_what_people_and_agents_decided(eng):
    fid, v = run(eng, g3={"decision": "revise", "feedback": "add the offline banner", "reason": "unclear"})
    shadow = {d["gate"]: d["shadow_auto"] for d in v["gate_decisions"] if d.get("shadow_auto")}
    assert set(shadow) == {"g2", "g3", "g4"}  # G1 and G6 stay with people
    assert shadow["g2"] == {"eligible": True, "why": []}  # general data, first try, checklist met
    critics = [e["critic"] for e in v["events"] if e.get("critic")]
    assert critics and all(c["shadow"]["agree"] for c in critics)
    assert all(c["shadow"]["model"] == "anthropic/claude-sonnet-5-5" for c in critics)
    build = next(e for e in v["events"] if e["node"] == "build" and e["kind"] == "passed")
    assert build["reviews"] and all(r["shadow"]["agree"] for r in build["reviews"])
    rows = eng.services.ledger.records(fid)
    assert {r.role for r in rows} >= {"critic_shadow", "reviewer_shadow"}
    assert {r.config_version for r in rows} == {pilot.config_version(eng.services.settings)}

    rep = pilot.report(eng, "FARM")
    t = rep["totals"]
    assert rep["count"] == 1 and rep["done"] == 1
    assert t["shadow_auto_approval"]["gates"] == 4  # g2, g3 (twice: revised, then approved), g4
    assert t["shadow_auto_approval"]["would_have_missed"] == 1  # the rule would have passed what G3 sent back
    assert t["shadow_critic"]["compared"] == t["shadow_critic"]["agreed"] > 0
    assert t["shadow_reviewer"]["compared"] == t["shadow_reviewer"]["agreed"] > 0
    assert t["old_score_agreed"]["compared"] > 0
    assert t["revise_reasons"] == {"unclear": 1}
    assert rep["features"][0]["shadow_cost_usd"] > 0 and rep["features"][0]["cost_usd"] > 0


def test_a_feature_outside_the_pilot_runs_no_shadows_and_is_left_out_of_the_report(eng):
    fid, v = run(eng, pilot_on=False)
    assert not [d for d in v["gate_decisions"] if d.get("shadow_auto")]
    assert not [e for e in v["events"] if (e.get("critic") or {}).get("shadow")]
    assert not {r.role for r in eng.services.ledger.records(fid)} & {"critic_shadow", "reviewer_shadow"}
    assert pilot.report(eng, "FARM")["count"] == 0 and pilot.report(eng, "FARM", pilot_only=False)["count"] == 1


def test_human_share_counts_decisions_reviews_and_agents_time(eng, monkeypatch):
    fid, v = run(eng)
    review = {"action": "submitted", "sender": {"login": "maria", "type": "User"},
              "pull_request": {"head": {"ref": f"feature/{fid}"}, "html_url": "https://github.com/x/y/pull/1"},
              "review": {"id": 11, "state": "approved"}}
    record_review_effort(eng, "pull_request_review", review)
    record_review_effort(eng, "pull_request_review_comment", {**review, "action": "created", "comment": {"id": 12}})
    record_review_effort(eng, "pull_request_review", {**review, "sender": {"login": "vrixel[bot]", "type": "Bot"}})
    record_review_effort(eng, "pull_request_review", {**review, "pull_request": {"head": {"ref": "main"}}})
    f = pilot.feature_report(eng, fid)
    gates = sum(d["active_minutes"] for d in v["gate_decisions"])
    assert f["pr_reviews"] == 1 and f["pr_comments"] == 1
    assert f["human_minutes"] == pytest.approx(gates + 10 + 2)
    assert f["human_minutes_by_kind"]["pr_review"] == 10
    # Agents' time is what their calls took; with stub agents it is near zero, so set some.
    for r in eng.services.ledger.records(fid):
        r.duration_s = 60.0
    f = pilot.feature_report(eng, fid)
    calls = len(eng.services.ledger.records(fid))
    assert f["agent_minutes"] == pytest.approx(calls, abs=0.01)
    assert f["human_share"] == pytest.approx(f["human_minutes"] / (f["human_minutes"] + calls), abs=1e-3)
    md = pilot.markdown(pilot.report(eng, "FARM"))
    assert "Human share" in md and fid in md and "## Shadow modes" in md and "(target 15%)" in md


def test_on_call_decisions_count_in_the_product_totals(eng):
    alert = {"alerts": [{"status": "firing", "fingerprint": "p1", "startsAt": datetime.now(timezone.utc).isoformat(),
                         "labels": {"alertname": "Errors", "severity": "error", "component": "farm-field-api"}}]}
    [iid] = eng.ingest(from_alertmanager(alert))
    for p in [p for p in eng.pending(iid) if p["type"] != "external"][:1]:
        eng.decide(iid, p["interrupt_id"], {"decision": p["allowed"][0], "approver": "oncall", "active_minutes": 4,
                                            **({"action_ids": []} if "action_ids" in str(p) else {})})
    events = [e for e in eng.services.effort.events(product_id="FARM") if e["kind"] == "incident_decision"]
    assert events and events[0]["minutes"] == 4
    assert pilot.report(eng, "FARM", pilot_only=False)["totals"]["incident_decisions"] >= 1


def test_the_report_is_served_to_the_products_members_and_printed_by_the_cli(eng, capsys):
    run(eng)
    client = TestClient(create_app(eng, auth=AuthConfig(mode="dev", secret="x" * 40), web_dist=None))
    client.post("/api/auth/dev-login", json={"login": "maria", "orgs": ["acme-farm"]})
    r = client.get("/api/reports/pilot?product_id=FARM").json()
    assert r["count"] == 1 and r["totals"]["target_human_share"] == 0.15
    md = client.get("/api/reports/pilot?product_id=FARM&format=markdown")
    assert md.headers["content-type"].startswith("text/markdown") and "# Pilot report: FARM" in md.text
    assert client.get("/api/reports/pilot?product_id=CLN").status_code == 403

    class Args:
        product, all, since, json = "FARM", False, "", False

    assert cli._pilot_report(Args(), engine=eng) == 0
    assert "# Pilot report: FARM" in capsys.readouterr().out


def test_intake_can_submit_a_pilot_feature(eng):
    client = TestClient(create_app(eng, auth=AuthConfig(mode="dev", secret="x" * 40), web_dist=None))
    client.post("/api/auth/dev-login", json={"login": "maria", "orgs": ["acme-farm"]})
    s = client.post("/api/intake", json={}).json()
    for text in ("Agronomists retype their field notes into spreadsheets.", "It's for Farm app",
                 "Agronomists visiting farms", "Half of field notes exported within a month"):
        s = client.post(f"/api/intake/{s['id']}/messages", json={"text": text}).json()
    assert s["ready"], s["missing"]
    r = client.post(f"/api/intake/{s['id']}/submit", json={"pilot": True})
    assert r.status_code == 201, r.text
    assert eng._state(r.json()["feature_id"])["pilot"] is True

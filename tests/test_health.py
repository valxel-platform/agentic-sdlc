"""The tool's own health (valxel_sdlc.health): measured from what the pipeline records, checked
against the baseline, alerted once and again when it clears."""

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from test_api import SECRET, sign_in

from valxel_sdlc import health
from valxel_sdlc.api.app import create_app
from valxel_sdlc.web.auth import AuthConfig

SINCE, UNTIL = "2026-11-12T00:00:00+00:00", "2026-11-19T00:00:00+00:00"
AT = "2026-11-15T10:00:00+00:00"
RULES = health.Rules.load(__import__("pathlib").Path(__file__).resolve().parents[1] / "config").rules


def ev(node, kind, at=AT, **kw):
    return {"node": node, "kind": kind, "at": at, "detail": kw.pop("detail", ""), **kw}


def gate(g, decision, at=AT, phase="spec"):
    return {"gate": g, "phase": phase, "decision": decision, "at": at}


def test_runs_loops_stalls_and_escalation_reasons():
    s = {"feature_id": "F1", "events": [
        ev("spec", "passed", attempts=1),
        ev("spec", "escalated", attempts=2, detail="No progress after 2 tries (repeating, writer and critic): x",
           stall={"pattern": "repeating", "between": "writer and critic"}),
        ev("build", "escalated", attempts=1, detail="The architecture needs changing (coder on T1): y"),
        ev("escalate_build", "reopened", to="architecture"),
        ev("plan", "escalated", detail="token budget exhausted (VX-10)"),
        ev("spec", "passed", attempts=3, at="2026-11-01T00:00:00+00:00"),  # outside the window
    ]}
    m = health.measure([s], [], since=SINCE, until=UNTIL)
    assert m["phases"]["spec"] == {"runs": 2, "escalation_rate": 0.5, "stall_rate": 0.5, "stalls": 1,
                                   "loops_mean": 1.5, "loops_p90": 2}
    assert m["stalls"] == {"spec:repeating:writer and critic": 1}
    assert m["escalation_reasons"] == {"spec:stall:repeating": 1, "build:design_issue": 1, "plan:budget": 1}
    assert m["reopened"] == {"architecture": 1}


def test_critic_verdicts_against_people_and_first_time_approvals():
    def feature(fid, then):
        return {"feature_id": fid,
                "events": [ev("spec", "passed", attempts=1, at="2026-11-15T10:00:00+00:00",
                              critic={"items": [{"id": "x", "met": True}], "passed": True})],
                "gate_decisions": [gate("g2", then, at="2026-11-15T11:00:00+00:00"),
                                   gate("g2", "approve", at="2026-11-15T12:00:00+00:00")]}

    m = health.measure([feature("F1", "revise"), feature("F2", "approve")], [], since=SINCE, until=UNTIL)
    assert m["critic"]["spec"] == {"passed": 2, "false_pass": 0.5, "failed": 0, "false_fail": None}
    assert m["gates"]["g2"] == {"decided": 2, "first_time_approval": 0.5}  # only each feature's first G2


def test_cost_and_time_per_phase_and_role_leave_shadows_out():
    def rec(fid, phase, role, cost, mins, ts=AT):
        return SimpleNamespace(ts=ts, feature_id=fid, phase=phase, role=role, est_cost_usd=cost, duration_s=mins * 60)

    m = health.measure([], [rec("F1", "spec", "writer", 1.0, 2), rec("F1", "spec", "critic", 0.5, 1),
                            rec("F2", "build", "coder", 3.0, 10), rec("F2", "spec", "critic_shadow", 9.0, 9),
                            rec("F3", "build", "coder", 99.0, 1, ts="2026-10-01T00:00:00+00:00")],
                       since=SINCE, until=UNTIL)
    assert m["cost_by_phase_role"] == {"build/coder": 3.0, "spec/critic": 0.5, "spec/writer": 1.0}
    assert m["agent_minutes_by_phase_role"]["build/coder"] == 10.0
    assert m["cost_per_feature"] == {"features": 2, "mean": 2.25, "max": 3.0}


def _critic(passed, false_pass):
    return {"critic": {"spec": {"passed": passed, "false_pass": false_pass, "failed": 0, "false_fail": None}}}


def test_a_critic_passing_what_people_send_back_trips_against_its_baseline():
    [t] = health.check(_critic(8, 0.5), _critic(20, 0.1), RULES)
    assert t["rule"] == "critic_false_pass" and t["key"] == "critic_false_pass:spec"
    assert "passed 50% of documents people then sent back" in t["says"]
    assert health.check(_critic(8, 0.3), _critic(20, 0.25), RULES) == []  # high, but no worse than usual
    assert health.check(_critic(3, 0.67), _critic(20, 0.1), RULES) == []  # too few to say
    assert health.check(_critic(8, 0.5), {}, RULES)  # no baseline yet: the threshold alone


def test_gates_phases_cost_queue_and_waits():
    cur = {"gates": {"g4": {"decided": 6, "first_time_approval": 0.33}},
           "phases": {"build": {"runs": 10, "escalation_rate": 0.5, "stall_rate": 0.3}},
           "cost_per_feature": {"features": 4, "mean": 30.0}}
    base = {"gates": {"g4": {"decided": 30, "first_time_approval": 0.8}},
            "phases": {"build": {"runs": 40, "escalation_rate": 0.1, "stall_rate": 0.05}},
            "cost_per_feature": {"features": 20, "mean": 12.0}}
    rules = {t["rule"] for t in health.check(cur, base, RULES, queue={"queued": 3, "running": 0, "oldest_s": 1800},
                                             waiting=[{"feature_id": "F9", "wait": "ci", "hours": 30.0},
                                                      {"feature_id": "F8", "wait": "ci", "hours": 2.0}])}
    assert rules == {"first_time_approval", "escalation_rate", "stall_rate", "cost_per_feature",
                     "queue_oldest_minutes", "external_wait_hours"}


def test_alerts_once_then_daily_and_once_more_when_it_clears():
    notes, said, t = {}, [], [1000.0]
    cluster = SimpleNamespace(get=notes.get, put=notes.__setitem__)
    tripped = health.check(_critic(8, 0.5), _critic(20, 0.1), RULES)
    for _ in range(3):
        health._alert(cluster, tripped, alert=said.append, clock=lambda: t[0])
    assert len(said) == 1 and said[0].startswith("Valxel tool health: The spec critic passed 50%")
    t[0] += health.REALERT_S
    health._alert(cluster, tripped, alert=said.append, clock=lambda: t[0])
    assert len(said) == 2
    health._alert(cluster, [], alert=said.append, clock=lambda: t[0])
    assert said[-1] == "Valxel tool health: back to normal: critic_false_pass for spec"
    health._alert(cluster, [], alert=said.append, clock=lambda: t[0])
    assert len(said) == 3


def test_a_real_run_measures_the_pipeline_and_keeps_the_result(engine):
    engine.start(product_id="FARM", title="Soil sensor import", simulate_faults={"spec": 9})
    said = []
    result = health.run(engine, alert=said.append)
    assert engine.cluster.get(health.NOTE)["at"] == result["at"]
    assert result["current"]["features"] >= 1 and "discovery" in result["current"]["phases"]


@pytest.fixture
def client(engine, monkeypatch):
    monkeypatch.setenv("VALXEL_EVAL_APPROVERS", "maya")
    with TestClient(create_app(engine, auth=AuthConfig(mode="dev", secret=SECRET), web_dist=None)) as c:
        yield c


def test_only_the_tools_owners_see_its_health(client):
    sign_in(client, "maya")
    r = client.get("/api/reports/health?fresh=true")
    assert r.status_code == 200 and {"current", "baseline", "alerts"} <= set(r.json())
    assert r.json()["dead_letters"] == {"count": 0, "recent": []}
    assert client.get("/api/me").json()["tool_owner"] is True
    sign_in(client, "someone-else")
    assert client.get("/api/reports/health").status_code == 403
    assert client.get("/api/me").json()["tool_owner"] is False


def test_a_products_people_see_its_own_health(client):
    sign_in(client, "priya", teams=["acme-farm/product-owners"], orgs=["acme-farm"])
    r = client.get("/api/reports/health?product_id=FARM")
    assert r.status_code == 200 and r.json()["product_id"] == "FARM"
    assert client.get("/api/reports/health").status_code == 403  # not across products
    sign_in(client, "someone-else", orgs=["acme-clinic"])
    assert client.get("/api/reports/health?product_id=FARM").status_code == 403


def test_runs_the_queue_gave_up_on_alert():
    dead = [{"at": "2026-11-19T00:00:00+00:00", "thread_id": "FARM-FEAT-9", "run_id": "r1", "attempts": 3}]
    [t] = health.check({}, {}, {"dead_letters": 1}, dead=dead)
    assert t["rule"] == "dead_letters" and "FARM-FEAT-9" in t["says"]
    assert health.check({}, {}, {"dead_letters": 1}, dead=[]) == []


def test_one_product_sees_its_own_health(engine):
    engine.start(product_id="FARM", title="Soil sensor import")
    got = health.for_product(engine, "FARM")
    assert got["product_id"] == "FARM" and got["safe_mode"] is None
    assert {"phases", "critic", "gates"} <= set(got["current"])
    assert all(a["rule"] not in ("queue_oldest_minutes", "external_wait_hours", "dead_letters") for a in got["alerts"])

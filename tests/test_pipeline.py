import pytest

from conftest import approve
from vrixel_sdlc.graph import InvalidDecision

ALL_KINDS = {"OPP", "PRD", "UX", "HLD", "PLAN", "BUILD", "REL", "OPS"}


def test_low_risk_feature_runs_end_to_end_without_g5(engine, services):
    v = engine.start(product_id="AG", title="Offline crop log", description="Log crop observations offline")
    assert [p["gate"] for p in v["pending"]] == ["g1"]

    v = engine.auto_drive(v["feature_id"])

    assert v["status"] == "done"
    assert set(v["artifacts"]) == ALL_KINDS
    assert v["risk_tier"] == "low"
    gates = [g["gate"] for g in v["gate_decisions"]]
    assert gates == ["g1", "g2", "g3", "g4", "g6"] or sorted(gates) == ["g1", "g2", "g3", "g4", "g6"]
    assert "g5" not in gates  # low-risk PRs auto-merge
    assert all(pr["state"] == "merged" and pr["merged_by"] == "agent:merge-train" for pr in v["pull_requests"].values())
    assert v["release"]["production"] is True and v["release"]["promoted_by"] == "auto-approver"
    caps = {c["id"] for c in services.catalog.lookup("AG")}
    assert any(c.startswith("CAP-AG-OFFLINE_CROP_LOG") for c in caps)  # knowledge write-back
    assert v["usage"]["calls"] > 10 and v["cost_usd"] > 0


def test_trace_links_connect_the_artifact_chain(engine):
    v = engine.auto_drive(engine.start(product_id="AG", title="Field notes")["feature_id"])
    snap = engine.graph.get_state(engine._config(v["feature_id"])).values
    links = {tuple(x) for x in snap["trace_links"]}
    fid = v["feature_id"]
    for parent, child in [("OPP", "PRD"), ("PRD", "UX"), ("PRD", "HLD"), ("HLD", "PLAN"), ("PLAN", "BUILD"),
                          ("BUILD", "REL"), ("REL", "OPS")]:
        assert (f"{fid}.{parent}.v1", f"{fid}.{child}.v1") in links


def test_regulated_product_requires_g5_and_merges_as_the_approver(engine):
    fid = engine.start(product_id="EMR", title="Consent check on intake")["feature_id"]
    for gate in ("g1", "g2"):
        approve(engine, fid, gate)
    approve(engine, fid, "g3")
    v = approve(engine, fid, "g4")
    assert [p["gate"] for p in v["pending"]] == ["g5"]
    assert v["risk_tier"] == "regulated"
    assert v["pending"][0]["pull_requests"], "G5 packet lists the PRs"

    v = approve(engine, fid, "g5")
    assert all(pr["merged_by"] == "g5-owner" for pr in v["pull_requests"].values())
    v = approve(engine, fid, "g6")
    assert v["status"] == "done"


def test_auth_paths_make_a_general_product_high_risk(engine):
    v = engine.start(product_id="AG", title="Farmer login", description="Add auth for farmer accounts")
    v = engine.auto_drive(v["feature_id"])
    assert v["risk_tier"] == "high"
    assert "g5" in [g["gate"] for g in v["gate_decisions"]]


def test_design_and_architecture_gates_are_pending_in_parallel(engine):
    fid = engine.start(product_id="AG", title="Yield chart")["feature_id"]
    approve(engine, fid, "g1")
    v = approve(engine, fid, "g2")
    assert sorted(p["gate"] for p in v["pending"]) == ["g3", "g4"]
    assert {"UX", "HLD"} <= set(v["artifacts"])
    v = approve(engine, fid, "g4")
    assert [p["gate"] for p in v["pending"]] == ["g3"], "plan waits for both gates"
    v = approve(engine, fid, "g3")
    assert v["phase_status"].get("plan") == "passed"


def test_revise_at_g3_reruns_design_with_feedback(engine, services):
    fid = engine.start(product_id="AG", title="Yield chart")["feature_id"]
    approve(engine, fid, "g1")
    approve(engine, fid, "g2")
    [g3] = [p for p in engine.pending(fid) if p["gate"] == "g3"]
    v = engine.decide(fid, g3["interrupt_id"], {"decision": "revise", "approver": "design-lead",
                                                 "feedback": "Add an empty state"})
    # G3 and G4 share a step: the revise takes effect once G4 is decided too.
    assert [p["gate"] for p in v["pending"]] == ["g4"]
    v = approve(engine, fid, "g4")
    assert v["artifacts"]["UX"].endswith(".UX.v2")
    snap = engine.graph.get_state(engine._config(fid)).values
    assert "Add an empty state" in services.artifacts.load(snap["artifacts"]["UX"])
    assert [p["gate"] for p in v["pending"]] == ["g3"]
    v = approve(engine, fid, "g3")
    assert v["phase_status"].get("plan") == "passed"  # G4's earlier approval still counts


def test_evaluator_loop_fixes_a_defective_prd(engine):
    v = engine.start(product_id="AG", title="Soil sensor import", simulate_faults={"spec": 1})
    v = approve(engine, v["feature_id"], "g1")
    assert v["loop_count"]["spec"] == 2
    assert v["phase_status"]["spec"] == "passed"
    assert [p["gate"] for p in v["pending"]] == ["g2"]


def test_persistent_failure_escalates_after_the_loop_cap(engine, services):
    fid = engine.start(product_id="AG", title="Soil sensor import", simulate_faults={"spec": 9})["feature_id"]
    v = approve(engine, fid, "g1")
    [esc] = v["pending"]
    assert esc["type"] == "escalation" and esc["phase"] == "spec"
    assert esc["reason"] == "3 evaluator loops failed"
    assert any("not testable" in f for f in esc["findings"])

    fixed = (
        "# PRD: Soil sensor import\n\n## Goals\n- G1: Import sensor data\n\n## Non-goals\n- Hardware\n\n"
        "## Acceptance criteria\n- AC-1: Given a sensor file, when it is uploaded, then readings are stored\n\n"
        "## Constitution articles\n- VX-5\n"
    )
    v = engine.decide(fid, esc["interrupt_id"], {"decision": "resolved", "approver": "pm",
                                                  "artifact_content": fixed, "active_minutes": 25})
    assert v["artifacts"]["PRD"].endswith(".PRD.v2")  # the agents' last draft, then the human's
    assert v["phase_status"]["spec"] == "resolved"
    assert sorted(p["gate"] for p in v["pending"]) == ["g3", "g4"]  # resolution stands in for G2
    v = engine.auto_drive(fid)
    assert v["status"] == "done"
    assert v["human_active_minutes"] >= 25


def test_budget_exhaustion_escalates_and_can_be_raised(engine):
    v = engine.start(product_id="AG", title="Weather alerts", budget_usd=0.000001)
    v = approve(engine, v["feature_id"], "g1")  # the supervisor checks the budget before each phase
    [esc] = v["pending"]
    assert esc["type"] == "escalation" and esc["phase"] == "spec" and "budget" in esc["reason"]
    v = engine.decide(v["feature_id"], esc["interrupt_id"],
                      {"decision": "retry", "approver": "pm", "budget_usd": 50})
    assert v["budget_usd"] == 50
    assert [p["gate"] for p in v["pending"]] == ["g2"]


def test_build_retries_only_failing_tasks(engine, services):
    v = engine.start(product_id="AG", title="Harvest export", simulate_faults={"build": 1})
    v = engine.auto_drive(v["feature_id"])
    assert v["status"] == "done"
    assert v["loop_count"]["build"] == 2
    coder_calls = [r for r in services.ledger.records(v["feature_id"]) if r.role == "coder"]
    tasks = len(v["pull_requests"])
    assert len(coder_calls) == tasks + 1  # one retry, for the failing task only


def test_takeover_waits_for_handback(engine):
    fid = engine.start(product_id="AG", title="Pest report")["feature_id"]
    approve(engine, fid, "g1")
    approve(engine, fid, "g2")
    [g4] = [p for p in engine.pending(fid) if p["gate"] == "g4"]
    v = engine.decide(fid, g4["interrupt_id"], {"decision": "takeover", "approver": "architect"})
    hb = [p for p in v["pending"] if p["type"] == "handback"]
    assert hb and hb[0]["gate"] == "g4"
    v = engine.decide(fid, hb[0]["interrupt_id"], {"decision": "handback", "approver": "architect"})
    assert [p["gate"] for p in v["pending"]] == ["g3"]


def test_abort_stops_the_thread(engine):
    v = engine.start(product_id="POS", title="Tip jar")
    [g1] = v["pending"]
    v = engine.decide(v["feature_id"], g1["interrupt_id"], {"decision": "abort", "approver": "po"})
    assert v["status"] == "aborted" and v["pending"] == []


def test_invalid_decisions_are_rejected_before_resuming(engine):
    v = engine.start(product_id="AG", title="Irrigation plan")
    [g1] = v["pending"]
    with pytest.raises(InvalidDecision):
        engine.decide(v["feature_id"], g1["interrupt_id"], {"decision": "resolved", "approver": "x"})
    assert [p["gate"] for p in engine.pending(v["feature_id"])] == ["g1"]


def test_agents_cannot_promote_to_production(services):
    oc = services.openchoreo
    oc.run_workflow(component="svc", ref="main", kind="main")
    oc.promote(component="svc", env="staging", actor="agent:release", actor_type="agent")
    with pytest.raises(PermissionError):
        oc.promote(component="svc", env="production", actor="agent:release", actor_type="agent")


def test_apx_merges_library_before_provider_before_consumer(engine):
    v = engine.auto_drive(engine.start(product_id="APX", title="Idempotent refunds")["feature_id"])
    assert v["status"] == "done"
    assert v["change_set"]["merge_order"] == ["apx-contracts", "apx-ledger", "apx-gateway"]

"""How far the critic can be trusted, and what each config version's work led to."""

from valxel_sdlc import pilot

from conftest import approve


def ev(node, kind, at, critic=True):
    return {"node": node, "kind": kind, "at": at, **({"critic": {"items": [{"id": "x", "met": True}], "passed": True}} if critic else {})}


def dec(gate, phase, decision, at, **kw):
    return {"gate": gate, "phase": phase, "decision": decision, "at": at, "approver": "p", "feedback": "", **kw}


def test_each_verdict_is_judged_by_what_people_did_next():
    state = {
        "events": [ev("spec", "passed", "t1"), ev("spec", "passed", "t3"), ev("plan", "escalated", "t5", critic=False),
                   ev("design", "escalated", "t7", critic=False), ev("architecture", "passed", "t9")],
        "gate_decisions": [dec("g2", "spec", "revise", "t2", reason="missing_requirement"), dec("g2", "spec", "approve", "t4"),
                           dec("escalation:plan", "plan", "resolved", "t6", changed=False),
                           dec("escalation:design", "design", "resolved", "t8", changed=True)]}
    got = [(v["phase"], v["outcome"], v["reason"]) for v in pilot.critic_outcomes(state)]
    assert got == [("spec", "passed_then_revised", "missing_requirement"), ("spec", "passed_then_approved", ""),
                   ("plan", "failed_then_vouched", ""), ("design", "failed_then_fixed", "")]  # G4 not decided yet
    acc = pilot.critic_accuracy([{"critic_outcomes": pilot.critic_outcomes(state)}])
    assert acc["spec"]["false_pass_rate"] == 0.5 and acc["spec"]["missed_because"] == {"missing_requirement": 1}
    assert acc["plan"]["false_fail_rate"] == 1.0 and acc["design"]["false_fail_rate"] == 0.0


def test_the_report_scores_the_critic_and_each_config(engine):
    fid = engine.create(product_id="FARM", title="Offline crop log", pilot=True)
    v = approve(engine, fid, "g1")
    [g2] = [p for p in engine.pending(fid) if p.get("gate") == "g2"]
    engine.decide(fid, g2["interrupt_id"], {"decision": "revise", "approver": "pm", "feedback": "add the sync limit",
                                            "reason": "missing_requirement"})
    v = engine.auto_drive(fid)
    assert v["status"] == "done"
    rep = pilot.report(engine, "FARM")
    spec = rep["critic_accuracy"]["spec"]
    assert spec["passed_then_revised"] == 1 and spec["passed_then_approved"] == 1 and spec["false_pass_rate"] == 0.5
    [(cfg, row)] = rep["by_config"].items()
    assert cfg == pilot.config_version(engine.services.settings) and row["features"] == 1 and row["done"] == 1
    assert row["critic_false_pass_rate"] > 0 and row["post_merge_fixes"] == 0 and row["incidents"] == 0
    f = rep["features"][0]
    assert f["outcomes"]["pre_merge_fixes"] == 0 and f["outcomes"]["traceability_overridden"] is False
    md = pilot.markdown(rep)
    assert "## How far the critic can be trusted" in md and "| spec | 1 | 1 | 0 | 0 | 50% | n/a |" in md
    assert "## By configuration" in md and cfg in md


def test_an_escalation_people_accept_as_is_counts_against_the_critic(engine):
    fid = engine.start(product_id="FARM", title="Soil sensor import", simulate_faults={"spec": 9})["feature_id"]
    approve(engine, fid, "g1")
    [esc] = engine.pending(fid)
    engine.decide(fid, esc["interrupt_id"], {"decision": "resolved", "approver": "pm"})  # the draft is fine as it is
    s = engine._state(fid)
    assert s["gate_decisions"][-1]["changed"] is False
    assert ("spec", "failed_then_vouched") in [(v["phase"], v["outcome"]) for v in pilot.critic_outcomes(s)]


def test_fixes_before_and_after_merge_are_counted(engine, services):
    services.github.ci_fail_once = {"acme-farm/farm-mobile"}
    v = engine.auto_drive(engine.start(product_id="FARM", title="Offline crop log")["feature_id"])
    o = pilot.outcomes(engine, v["feature_id"], engine._state(v["feature_id"]))
    assert o["pre_merge_fixes"] == 0 and o["post_merge_fixes"] == 0  # red CI is a retry, not an auto_fix
    assert pilot.feature_report(engine, v["feature_id"])["ci_retries"] == 1

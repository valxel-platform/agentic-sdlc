"""Triage earns autonomy: in shadow mode the tool records what it would have done and pages on-call,
until its choices have matched what fixed incidents often enough; each triage is also an eval case,
labeled with what fixed the incident, so a config change that makes triage worse fails."""

from datetime import datetime, timedelta, timezone

import pytest

from valxel_sdlc import evals
from valxel_sdlc.ops import incident as incident_mod
from valxel_sdlc.ops import remediation

from test_remediation import POLICY, alert, gate, make, root, urgent  # noqa: F401 - root is a fixture

SHADOW = "shadow: {min_samples: 2, agree_at_least: 0.5, window_days: 30}\n"


def _with_shadow(root):
    p = root / "valxel-platform" / "sdlc-governance" / "platform" / "remediation.yaml"
    p.write_text(p.read_text() + SHADOW)


def _settled(would, taken, recovered=True, status="closed", at=None):
    return {"status": status, "shadow": {"actions": would, "at": at or datetime.now(timezone.utc).isoformat()},
            "taken": taken, "events": [{"kind": "recovered"}] if recovered else [], "component": "cln-api"}


FLAG = {"type": "flag_off", "target": "CLN-FEAT-1"}


def test_the_policy_takes_a_shadow_period():
    pol = remediation.load_policy(POLICY.read_text() + SHADOW)
    assert pol["shadow"] == {"min_samples": 2, "agree_at_least": 0.5, "window_days": 30}
    assert remediation.for_product(pol, None)["shadow"] == pol["shadow"]  # a product can't opt out of earning it
    assert remediation.load_policy(POLICY.read_text())["shadow"] is None
    assert remediation.load_policy("autonomous: {}\nshadow: true")["shadow"]["min_samples"] == 10
    for bad in ("shadow: {min_samples: 0}", "shadow: {agree_at_least: 0}", "shadow: {agree_at_least: 2}",
                "shadow: {window_days: 1.5}", "shadow: {min_sample: 3}"):
        with pytest.raises(remediation.PolicyError):
            remediation.load_policy("autonomous: {}\n" + bad)


def test_the_gradual_release_comparison_is_kept():
    """A second, shorter definition of _gradual used to drop `compare` from the parsed policy."""
    pol = remediation.load_policy(POLICY.read_text())
    assert pol["gradual_release"]["compare"]["z"] == 3.0 and pol["gradual_release"]["compare"]["min_requests"] == 200


def test_a_shadow_choice_agrees_only_when_people_took_it_and_it_recovered():
    took_flag = [{"type": "flag_off", "feature_id": "CLN-FEAT-1", "ok": True, "by": "oncall-1"}]
    assert remediation.shadow_outcome(_settled([FLAG], took_flag)) is True
    assert remediation.shadow_outcome(_settled([FLAG], took_flag, recovered=False)) is False
    rolled = [{"type": "rollback", "component": "cln-api", "ok": True, "by": "oncall-1"}]
    assert remediation.shadow_outcome(_settled([FLAG], rolled)) is False  # people undid something else
    assert remediation.shadow_outcome(_settled([FLAG], [])) is False  # nothing was needed
    failed = [{**took_flag[0], "ok": False}]
    assert remediation.shadow_outcome(_settled([FLAG], failed)) is False
    assert remediation.shadow_outcome(_settled([FLAG], took_flag, status="monitoring")) is None  # not settled yet
    assert remediation.shadow_outcome(_settled([], took_flag)) is None
    old = (datetime.now(timezone.utc) - timedelta(days=40)).isoformat()
    incs = [_settled([FLAG], took_flag), _settled([FLAG], rolled), _settled([FLAG], took_flag, at=old)]
    assert remediation.shadow_record(incs, 30) == {"agreed": 1, "disagreed": 1}


def test_triage_acts_alone_only_once_it_has_earned_it():
    cfg = {"min_samples": 2, "agree_at_least": 0.75, "window_days": 30}
    assert remediation.shadow_open({"agreed": 1, "disagreed": 0}, cfg)[0] is False  # too few
    ok, why = remediation.shadow_open({"agreed": 2, "disagreed": 1}, cfg)
    assert not ok and "2 of 3" in why and "75%" in why
    assert remediation.shadow_open({"agreed": 3, "disagreed": 1}, cfg)[0] is True
    assert remediation.shadow_open({}, None)[0] is True  # no shadow period


def _incident_through_close(eng, svc, monkeypatch):
    fid = eng.create(product_id="CLN", title="Consent capture at intake")
    eng.auto_drive(fid)
    [iid] = eng.ingest(alert())  # SEV2 on the component the feature just reached
    return fid, iid


def test_in_shadow_mode_the_tool_pages_with_what_it_would_have_done(tmp_path, root, monkeypatch):
    _with_shadow(root)
    eng, svc = make(tmp_path, root)
    fid, iid = _incident_through_close(eng, svc, monkeypatch)
    flag = eng.view(fid)["release"]["flag"]
    v = eng.view(iid)
    assert v["taken"] == [] and svc.flags.get("CLN", flag) is True  # it didn't act
    [page] = urgent(svc)
    assert "shadow mode" in page["payload"]["summary"] and f"flag off {fid}" in page["payload"]["summary"]
    st = eng._state(iid)
    assert st["shadow"]["actions"] == [{"type": "flag_off", "target": fid}]
    # On-call takes that same action, it goes quiet, and the postmortem is approved.
    g = gate(eng, iid)
    eng.decide(iid, g["interrupt_id"], {"decision": "approve", "approver": "oncall-1",
                                         "action_ids": [a["id"] for a in g["actions"] if a["type"] == "flag_off"]})
    monkeypatch.setattr(incident_mod, "_clock", lambda: datetime.now(timezone.utc) + timedelta(minutes=16))
    eng.ingest(alert(status="resolved"))
    pm = gate(eng, iid, "postmortem")
    eng.decide(iid, pm["interrupt_id"], {"decision": "approve", "approver": "oncall-1"})
    assert eng.shadow_record(30) == {"agreed": 1, "disagreed": 0}
    # The triage is an eval case, labeled with what fixed the incident.
    [case] = svc.evals.cases(phases=("triage",))
    assert case.feature_id == iid and case.label["recovered"] is True
    assert case.label["mitigations"] == [{"type": "flag_off", "target": fid}] and case.label["by_tool"] is False
    assert "Recent logs" in case.ctx["tail"] and case.ctx["tail"].count("Recent logs") == 1


def test_once_earned_the_tool_mitigates_by_itself_again(tmp_path, root):
    _with_shadow(root)
    eng, svc = make(tmp_path, root)
    svc.shadow_record = lambda days: {"agreed": 2, "disagreed": 0}
    fid = eng.create(product_id="CLN", title="Consent capture at intake")
    eng.auto_drive(fid)
    [iid] = eng.ingest(alert())
    v = eng.view(iid)
    assert [(t["type"], t["by"]) for t in v["taken"]][:1] == [("flag_off", "agent:remediate")]
    assert not eng._state(iid).get("shadow")


def _case(label, ctx_extra=None):
    return evals.EvalCase(id="CLN-INC-1:triage:1", product_id="CLN", feature_id="CLN-INC-1", phase="triage",
                          data_class="phi", at=evals.now(), config_version="", output="",
                          status="passed", label=label, ctx={"tail": "t", "stub": "", "component": "cln-api",
                                                              **(ctx_extra or {})})


def test_a_replayed_triage_is_scored_against_what_fixed_the_incident():
    flag = "## Proposed actions\n- flag_off: CLN-FEAT-1\n- hotfix: acme-clinic/cln-api: guard the null\n"
    roll = "## Proposed actions\n- rollback: cln-api\n"
    none = "## Proposed actions\n- monitor: it recovered\n"
    fixed_by_flag = {"recovered": True, "mitigations": [FLAG]}
    assert evals.triage_score(flag, fixed_by_flag) == (True, False)
    assert evals.triage_score(roll, fixed_by_flag, "cln-api") == (False, True)  # undoes the wrong thing
    assert evals.triage_score(none, fixed_by_flag) == (False, False)  # misses it, but takes nothing wrong
    quiet = {"recovered": True, "mitigations": []}
    assert evals.triage_score(none, quiet) == (True, False)
    assert evals.triage_score(flag, quiet) == (False, True)
    assert evals.triage_score(flag, {"recovered": False}) == (None, None)  # nothing says what was right


def test_a_config_whose_triage_picks_wrong_actions_fails_the_comparison(tmp_path, root, monkeypatch):
    eng, svc = make(tmp_path, root)
    cases = [_case({"recovered": True, "mitigations": [FLAG]}, {"stub": "## Proposed actions\n- flag_off: CLN-FEAT-1\n"})]
    base = evals.run(svc, cases)
    assert base["results"][0]["passed"] is True and base["summary"]["all"]["wrong_action_rate"] == 0.0
    worse = [_case(cases[0].label, {"stub": "## Proposed actions\n- rollback: cln-api\n"})]
    cand = evals.run(svc, worse)
    cmp_ = evals.compare(base, cand)
    assert not cmp_["ok"] and any("wrong flag off or rollback" in r for r in cmp_["regressions"])
    assert evals.compare(base, base)["ok"]


def test_logs_stay_out_of_eval_cases_unless_the_data_is_general(tmp_path, root, monkeypatch):
    _with_shadow(root)
    eng, svc = make(tmp_path, root)
    fid = eng.create(product_id="CLN", title="Consent capture at intake")
    eng.auto_drive(fid)
    monkeypatch.setattr(incident_mod.untrusted, "lines", lambda src, xs: (
        "<logs>ERROR patient 123 failed</logs>" if src == "logs" else "\n".join(xs)))
    [iid] = eng.ingest(alert())
    [case] = svc.evals.cases(phases=("triage",), labeled=False)
    assert "patient 123" not in case.ctx["tail"] and "logs not kept" in case.ctx["tail"]

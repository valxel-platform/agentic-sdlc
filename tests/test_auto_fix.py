"""Before production the pipeline fixes its own failures: the agents get what failed and try again
(new pull requests on a fix branch once the change set has merged); people decide only when the
tries run out or nothing the feature changed is to blame."""

from test_quality import drive_gates, engine_for, gov_root, sql_injection  # noqa: F401

from vrixel_sdlc.quality import SuiteResult

MOBILE = "acme-farm/farm-mobile"


def fails(times, suite="e2e", stage="staging", repo=MOBILE, **kw):
    left = {"n": times}

    def result(s, r):
        if r == repo and left["n"] > 0:
            left["n"] -= 1
            return SuiteResult(suite=suite, kind="test", stage=stage, state="failed", repo=r, message="checkout.spec.ts", **kw)
        return SuiteResult(suite=suite, kind="test", stage=stage, state="passed", repo=r, passed=5, failed=0)

    return result


def auto_fixes(v):
    return [e for e in v["events"] if e["kind"] == "auto_fix"]


def test_a_staging_failure_is_fixed_by_the_agents_without_asking_anyone(tmp_path, gov_root):
    eng, svc = engine_for(tmp_path, gov_root)
    svc.quality.sources["stub"].results[("e2e", "*")] = fails(1)
    fid = eng.start(product_id="FARM", title="Offline crop log", description="x")["feature_id"]
    v = drive_gates(eng, fid)
    while v["pending"] and all(p["type"] == "gate" for p in v["pending"]):
        v = drive_gates(eng, fid)
    assert v["status"] == "done", v["pending"]
    [fix] = auto_fixes(v)
    assert "Staging failed in acme-farm/farm-mobile" in fix["detail"] and "try 1 of" in fix["detail"]
    assert not any(g["gate"].startswith("escalation:") for g in v["gate_decisions"])
    fix_prs = [p for p in svc.github.prs.values() if p["branch"] == f"feature/{fid}-fix-1"]
    assert [p["repo"] for p in fix_prs] == [MOBILE] and fix_prs[0]["state"] == "merged"
    state = eng.graph.get_state(eng._config(fid)).values
    assert state["fix_retries"] == 0 and state["fix_branch"] == ""  # staging passed: a fresh count


def test_people_decide_when_the_tries_run_out(tmp_path, gov_root):
    eng, svc = engine_for(tmp_path, gov_root)
    svc.quality.sources["stub"].results[("e2e", "*")] = fails(99)
    fid = eng.start(product_id="FARM", title="Offline crop log", description="x")["feature_id"]
    v = drive_gates(eng, fid)
    (esc,) = eng.pending(fid)
    assert esc["phase"] == "delivery" and esc["wait"] == "staging"
    limit = svc.feature_snapshot(eng.graph.get_state(eng._config(fid)).values).product.max_loops
    assert len(auto_fixes(v)) == limit


def test_a_failed_build_goes_back_to_the_agents(tmp_path, gov_root):
    eng, svc = engine_for(tmp_path, gov_root)
    left = {"n": 1}
    real = svc.openchoreo.run_workflow

    def flaky(**kw):  # the first build of the mobile app fails, the fix builds
        out = real(**kw)
        svc.openchoreo.run_results["acme-farm/farm-mobile"] = "failed" if left["n"] else "succeeded"
        left["n"] = max(0, left["n"] - 1) if kw["component"].endswith("farm-mobile") else left["n"]
        return out

    svc.openchoreo.run_workflow = flaky
    fid = eng.start(product_id="FARM", title="Offline crop log", description="x")["feature_id"]
    v = drive_gates(eng, fid)
    while v["pending"] and all(p["type"] == "gate" for p in v["pending"]):
        v = drive_gates(eng, fid)
    assert v["status"] == "done"
    assert any("The OpenChoreo build failed" in e["detail"] for e in auto_fixes(v))


def test_security_findings_in_ci_go_to_the_agents_first(tmp_path, gov_root):
    eng, svc = engine_for(tmp_path, gov_root)
    left = {"n": 1}

    def once(suite, repo):
        if left["n"]:
            left["n"] -= 1
            return sql_injection(suite, repo)
        return SuiteResult(suite="sast", kind="security", stage="pr", state="passed", repo=repo)

    svc.quality.sources["stub"].results[("sast", MOBILE)] = once
    fid = eng.start(product_id="FARM", title="Offline crop log", description="x")["feature_id"]
    v = drive_gates(eng, fid)
    while v["pending"] and all(p["type"] == "gate" for p in v["pending"]):
        v = drive_gates(eng, fid)
    assert v["status"] == "done"
    assert any("Security findings in" in e["detail"] for e in auto_fixes(v))
    assert not any(g["gate"].startswith("escalation:security") for g in v["gate_decisions"])


def test_a_failure_outside_the_change_goes_straight_to_people(tmp_path, gov_root):
    from vrixel_sdlc.graph.pipeline import failing_repos

    state = {"tasks": [{"id": "TASK-1", "repo": "o/api", "acs": ["AC-1"]}]}
    assert failing_repos(state, {"results": [{"repo": "resource:orders", "state": "failure"}]}) == []
    acc = {"quality": {"suites": [{"state": "failed", "repo": "o/acceptance",
                                   "details": {"criteria": {"AC-1": "failed", "AC-2": "passed"}}}]}}
    assert failing_repos(state, acc) == ["o/api"]  # the criterion's own repo

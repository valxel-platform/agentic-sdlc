"""Performance tests: industry-standard budgets, the rules that decide which repos need which
test, the reviewer raising (never lowering) a level, and the suites reading only those repos."""

import pytest

from conftest import approve
from valxel_sdlc import performance as perf
from valxel_sdlc.quality import Quality, QualityConfig, SuiteResult, effective_policy, load_quality
from valxel_sdlc.config import load_settings
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

HLD_TRAFFIC = """# HLD: Intake
## Performance
- Expected load: 40 requests per second at peak
- New traffic: yes: a new endpoint on the intake journey
- Data growth: yes: every intake keeps a record
- Journeys: intake
"""
HLD_QUIET = HLD_TRAFFIC.replace("New traffic: yes", "New traffic: no").replace("Data growth: yes", "Data growth: no")


def test_budgets_start_from_industry_targets_and_products_tighten_them():
    s = perf.settings(None)
    assert s["budgets"]["api"] == {"p95_ms": 500, "p99_ms": 1000, "error_rate": 0.01}
    assert s["budgets"]["web"]["lcp_ms"] == 2500 and s["budgets"]["web"]["cls"] == 0.1
    assert s["staging_load_factor"] == 0.1 and s["staging_regression_pct"] == 10
    tight = perf.settings({"budgets": {"api": {"p95_ms": 300}}, "peak_rps": 200, "hot_paths": ["src/billing/**"]})
    assert tight["budgets"]["api"] == {"p95_ms": 300, "p99_ms": 1000, "error_rate": 0.01}
    assert tight["peak_rps"] == 200 and "src/billing/**" in tight["hot_paths"]


def test_a_products_performance_settings_are_checked():
    assert perf.check({"budgets": {"api": {"p95_ms": 300}}, "staging_load_factor": 0.2}) == []
    problems = perf.check({"budgets": {"api": {"p50_ms": 1}, "db": {}}, "staging_load_factor": 2, "speed": 1})
    assert any("p50_ms is not a metric" in p for p in problems)
    assert any("budgets.db is not a kind" in p for p in problems)
    assert any("staging_load_factor" in p for p in problems)
    assert any("performance.speed is not a setting" in p for p in problems)


@pytest.mark.parametrize("role,files,hld,level", [
    ("provider", ["README.md", "src/test/java/com/x/GreetingTest.java", "docs/a.md"], "", "none"),
    ("provider", ["src/main/java/com/x/greeting/GreetingController.java"], HLD_QUIET, "smoke"),
    ("provider", ["src/main/resources/db/migration/V2__index.sql"], "", "smoke"),
    ("provider", ["gradle/libs.versions.toml"], "", "smoke"),
    ("provider", ["src/main/resources/application.yaml"], "", "smoke"),
    ("provider", ["api/openapi.yaml", "src/main/java/com/x/A.java"], "", "full"),
    ("provider", ["src/main/java/com/x/greeting/GreetingController.java"], HLD_TRAFFIC, "full"),
    ("event-worker", ["src/main/java/com/valxel/worker/x/SavedHandler.java"], "", "smoke"),  # no staging test
    ("library", ["src/main/java/com/x/Ids.java", "gradle/libs.versions.toml"], HLD_TRAFFIC, "none"),
    ("consumer", ["src/app/Intake.tsx"], HLD_TRAFFIC, "full"),
    ("provider", [f"src/main/java/com/x/M{i}.java" for i in range(15)], "", "smoke"),
])
def test_the_rules_decide_a_level_per_repo(role, files, hld, level):
    assert perf.decide("o/r", role, files, hld=hld).level == level


def test_data_growth_flags_the_soak_test_and_reasons_are_kept():
    d = perf.decide("o/r", "provider", ["src/main/java/com/x/greeting/GreetingController.java"], hld=HLD_TRAFFIC)
    assert d.soak and "a hot path changes" in d.reasons and "the design says the feature adds traffic" in d.reasons
    assert perf.design_signals(HLD_TRAFFIC)["expected_load"].startswith("40 requests")
    assert perf.design_signals("# HLD\n## Contracts\n- x") == {}


def test_the_reviewer_raises_a_level_and_never_lowers_one():
    quiet = perf.decide("o/r", "provider", ["src/main/java/com/x/Util.java"])
    assert quiet.level == "none"
    raised = perf.decide("o/r", "provider", ["src/main/java/com/x/Util.java"], concerns=["an N+1 query in list()"])
    assert raised.level == "smoke" and "the reviewer: an N+1 query in list()" in raised.reasons
    full = perf.decide("o/r", "provider", ["api/openapi.yaml"], concerns=["fine"])
    assert full.level == "full"
    assert perf.label("smoke") == "valxel:perf-smoke" and perf.label("full") == "valxel:perf-full"
    assert perf.label("none") is None


class Source:
    def __init__(self):
        self.asked = []

    def collect(self, suite, spec, targets, stage):
        self.asked.append((suite, [t["repo"] for t in targets]))
        return [SuiteResult(suite=suite, kind=spec["kind"], stage=stage, state="passed", repo=t["repo"]) for t in targets]


def test_performance_suites_read_only_the_repos_that_need_them():
    cfg = load_quality(ROOT / "config" / "quality.yaml")
    policy = {k: v for k, v in effective_policy(cfg, "general", {}).items() if k in ("perf-smoke", "unit")}
    for spec in policy.values():
        spec["source"] = "s"
    src = Source()
    q = Quality(QualityConfig(sources={"s": "x"}, suites=policy, floors={}), {"s": src})
    targets = [{"repo": "o/a", "sha": "1", "perf": "none"}, {"repo": "o/b", "sha": "2", "perf": "smoke"},
               {"repo": "o/c", "sha": "3", "perf": "full"}]
    res = q.check("pr", policy, targets, [])
    assert dict(src.asked)["perf-smoke"] == ["o/b", "o/c"] and dict(src.asked)["unit"] == ["o/a", "o/b", "o/c"]
    assert res["status"] == "passed"
    src.asked.clear()
    q.check("pr", policy, [{"repo": "o/a", "sha": "1", "perf": "none"}], [])
    assert "perf-smoke" not in dict(src.asked)  # not needed: not asked, not missing


def test_a_feature_labels_its_prs_and_shows_the_tests_at_the_gates(engine, services):
    fid = engine.start(product_id="FARM", title="Farmer login", description="Add auth for farmer accounts")["feature_id"]
    for gate in ("g1", "g2", "g3", "g4"):
        approve(engine, fid, gate)
    state = engine.graph.get_state(engine._config(fid)).values
    plan = state["performance"]
    assert plan and all(d["level"] in perf.LEVELS for d in plan.values())
    labelled = {pr["repo"]: pr["labels"] for pr in services.github.prs.values()}
    for repo, d in plan.items():
        if perf.label(d["level"]):
            assert perf.label(d["level"]) in labelled[repo]
    [g5] = [p for p in engine.pending(fid) if p.get("gate") == "g5"]
    assert {d["repo"] for d in g5["performance"]} == set(plan)
    assert any(e["kind"] == "performance_plan" for e in engine.view(fid)["events"])


def test_the_staging_run_is_asked_for_with_the_scaled_load(engine, services):
    fid = engine.start(product_id="FARM", title="Field notes")["feature_id"]
    v = engine.auto_drive(fid)
    assert v["status"] == "done"
    state = engine.graph.get_state(engine._config(fid)).values
    full = {r for r, d in (state.get("performance") or {}).items() if d["level"] == "full"}
    staging = [d for d in services.github.dispatches
               if d["event_type"] == "valxel-deployed" and d["payload"]["stage"] == "staging"]
    assert staging
    for d in staging:
        assert d["payload"]["performance"] == state["performance"].get(d["repo"], {}).get("level", "none")
        if d["repo"] in full:
            s = d["payload"]["perf_settings"]
            assert s["load_factor"] == 0.1 and s["budgets"]["api"]["p95_ms"] == 500
    assert len(set().union(*(d["payload"].keys() for d in staging))) <= 10  # repository_dispatch's limit


def test_a_product_with_bad_performance_settings_is_refused(tmp_path):
    import shutil

    from valxel_sdlc.governance.errors import GovernanceError
    from valxel_sdlc.services import build_services

    gov = tmp_path / "gov"
    shutil.copytree(ROOT / "examples" / "governance", gov)
    manifest = gov / "acme-farm" / "farm-governance" / "product.yaml"
    manifest.write_text(manifest.read_text() + "\nperformance:\n  budgets:\n    api:\n      p95_ms: -1\n")
    s = load_settings(mode="stub", database_url="", artifact_dir=tmp_path / "a", governance_source="local",
                      governance_root=gov)
    with pytest.raises(GovernanceError, match="p95_ms must be a positive number"):
        build_services(s).snapshot("FARM")


def test_a_failed_smoke_test_goes_back_to_the_agents(engine, services):
    services.github.perf_result = "failure"
    fid = engine.start(product_id="FARM", title="Farmer login", description="Add auth for farmer accounts")["feature_id"]
    for gate in ("g1", "g2", "g3", "g4"):
        approve(engine, fid, gate)
    owed = [s for s in services.github.statuses if s["context"] == "perf-smoke / report"]
    assert owed and owed[0]["state"] == "pending"
    retries = [e for e in engine.view(fid)["events"] if e["node"] == "ci_wait" and e["kind"] == "retry"]
    assert retries and "perf-smoke / report" in " ".join(retries[0].get("findings") or [retries[0]["detail"]])


def test_the_acceptance_report_carries_the_journey_load_test():
    from valxel_sdlc.acceptance import parse_report
    from valxel_sdlc.quality.sources import acceptance_result

    ok = "- AC-1: passed\nDuration: 40s\nPerformance: passed (p95 210 ms (budget 500 ms), errors 0.0%)\n"
    assert parse_report(ok)["performance"] == {"passed": True, "text": "p95 210 ms (budget 500 ms), errors 0.0%"}
    assert parse_report("- AC-1: passed\n")["performance"] is None
    target = {"repo": "o/acc", "acs": ["AC-1"]}
    assert acceptance_result("acceptance", "staging", target, ok, "success", "", {}).state == "passed"
    bad = ok.replace("Performance: passed (p95 210 ms", "Performance: failed (p95_ms 640 ms is over the budget of 500 ms")
    res = acceptance_result("acceptance", "staging", target, bad, "failure", "", {})
    assert res.state == "failed" and "journey load test failed: p95_ms 640 ms" in res.message


def test_the_acceptance_run_loads_journeys_only_when_staging_needs_a_load_test(services):
    from valxel_sdlc.waits import WaitChecker

    settings = {"perf_settings": {"peak_rps": 50, "load_factor": 0.1}}
    def packet(wait, level):
        return {"feature_id": "F", "wait": wait, "since": level, "performance": settings, "quality": {"targets": [
            {"repo": "o/svc", "sha": "1", "perf": level},
            {"repo": "o/acc", "kind": "acceptance", "sha": "2", "feature_id": "F", "grep": "", "projects": [],
             "acs": ["AC-1"]}]}}
    w = WaitChecker(services)
    for wait, level in (("staging", "full"), ("staging", "smoke"), ("dev", "full")):
        w._start_post_deploy(packet(wait, level), [{"repo": "o/svc", "url": "https://svc"}])
    sent = [d for d in services.github.dispatches if d["event_type"] == "valxel-acceptance"]
    assert [("perf_settings" in d["payload"]) for d in sent] == [True, False, False]
    assert all(len(d["payload"]) <= 10 for d in services.github.dispatches)

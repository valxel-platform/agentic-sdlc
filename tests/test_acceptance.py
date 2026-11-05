"""Product acceptance tests: a feature across repos is tested end to end from the product's
acceptance repo, plans extend existing tests instead of duplicating them, and every criterion
needs a passing test before the feature moves on."""

import json
import shutil
from pathlib import Path

import pytest
import yaml
from langgraph.checkpoint.memory import InMemorySaver

from vrixel_sdlc import acceptance
from vrixel_sdlc.config import load_settings
from vrixel_sdlc.engine import FeatureEngine
from vrixel_sdlc.quality import SuiteResult
from vrixel_sdlc.quality.sources import AcceptanceRuns, acceptance_result
from vrixel_sdlc.services import build_services

EXAMPLES = Path(__file__).resolve().parents[1] / "examples" / "governance"
ACC = "vrixel-agtech/agtech-acceptance"

CATALOG = {"tests": [
    {"file": "crops/log.spec.ts", "title": "a farmer logs a crop observation", "tags": ["@AG-FEAT-0", "@AC-1"],
     "capability": "crop-log", "covers": ["agtech-field-api PUT /api/v1/observations/{id}", "agtech-field-api GET /api/v1/observations"],
     "project": "api"},
    {"file": "crops/list.spec.ts", "title": "a farmer lists observations", "tags": ["@AG-FEAT-0"],
     "capability": "crop-log", "covers": ["agtech-field-api GET /api/v1/observations"], "project": "api"},
]}


@pytest.fixture
def services(tmp_path):
    root = tmp_path / "governance"
    shutil.copytree(EXAMPLES, root)
    manifest = root / "vrixel-agtech" / "ag-governance" / "product.yaml"
    data = yaml.safe_load(manifest.read_text())
    data["repos"].append({"name": ACC, "role": "acceptance"})
    manifest.write_text(yaml.safe_dump(data, sort_keys=False))
    s = load_settings(mode="stub", database_url="", artifact_dir=tmp_path / "artifacts",
                      governance_source="local", governance_root=root)
    return build_services(s)


@pytest.fixture
def engine(services):
    return FeatureEngine(services, InMemorySaver())


def test_a_feature_across_repos_is_tested_end_to_end(engine, services):
    v = engine.auto_drive(engine.start(product_id="AG", title="Offline crop log")["feature_id"])
    assert v["status"] == "done"
    fid = v["feature_id"]
    prs = {pr["repo"]: pr for pr in v["pull_requests"].values()}
    assert prs[ACC]["state"] == "merged"  # the acceptance tests merge with the change set, last
    assert ACC not in v["runs"]  # and are not built or deployed

    runs = [d for d in services.github.dispatches if d["event_type"] == "vrixel-acceptance"]
    assert [d["payload"]["stage"] for d in runs] == ["dev", "staging"]
    dev = runs[0]["payload"]
    assert dev["check"] == f"acceptance / {fid} / dev" and dev["sha"] == prs[ACC]["merge_sha"]
    assert set(dev["env"]) == {"agtech-field-api", "agtech-mobile"}  # every component's URL
    assert f"@{fid}" in dev["grep"] and "@comp-agtech-field-api" in dev["grep"]
    assert dev["projects"] == "api,ui"  # a web app changed: browsers too
    assert "@critical" in runs[1]["payload"]["grep"] and "@critical" not in dev["grep"]

    snap = engine.graph.get_state(engine._config(fid)).values
    row = next(s for s in snap["quality"]["dev"]["suites"] if s["suite"] == "acceptance")
    assert row["state"] == "passed" and set(row["details"]["criteria"].values()) == {"passed"}
    assert row["details"]["criteria"] and all(ac.startswith("AC-") for ac in row["details"]["criteria"])


def test_a_failing_criterion_sends_the_feature_back(engine, services):
    def failing(suite, repo):
        t = {"repo": repo, "acs": ["AC-1", "AC-2"], "feature_id": "AG-FEAT-1"}
        return acceptance_result(suite, "dev", t, "1 passed, 1 failed\n- AC-1: passed\n- AC-2: failed\nDuration: 30s",
                                 "failure", "u", {"budget_s": 600})
    services.quality.sources["acceptance-runs"].results[("acceptance", ACC)] = failing
    v = engine.auto_drive(engine.start(product_id="AG", title="Offline crop log")["feature_id"], max_steps=60)
    # The dev wait failed on the criterion and asked people (the helper decided as a person would).
    snap = engine.graph.get_state(engine._config(v["feature_id"])).values
    dev = snap["quality"]["dev"]
    assert dev["status"] == "failed" and any("failing: AC-2" in f for f in dev["test_failures"])
    assert any(e["node"] == "dev_wait" and e["kind"] != "passed" for e in v["events"])


def test_a_single_repo_change_needs_no_acceptance_run(services):
    repos = [{"name": "o/api", "role": "provider"}, {"name": ACC, "role": "acceptance"}]
    assert not acceptance.needed(repos)
    assert acceptance.needed(repos + [{"name": "o/w", "role": "event-worker"}])
    assert acceptance.needed(repos, min_repos=1)


def test_backend_only_changes_run_without_browsers():
    backend = [{"name": "o/emr-intake-api", "role": "provider"}, {"name": "o/emr-consent-worker", "role": "event-worker"}]
    sel = acceptance.selection("EMR-FEAT-1", backend, "dev")
    assert sel["projects"] == ["api"]
    assert sel["tags"] == ["@EMR-FEAT-1", "@comp-emr-consent-worker", "@comp-emr-intake-api"]
    import re
    rx = re.compile(sel["grep"])
    assert rx.search("records consent @EMR-FEAT-1 @AC-2") and not rx.search("other @EMR-FEAT-12")


def test_plans_extend_what_exists_instead_of_duplicating():
    catalog = acceptance.load_catalog(json.dumps(CATALOG))
    prd = "## Acceptance criteria\n- AC-1: a farmer logs offline\n- AC-2: logs sync later\n- AC-3: sync conflicts are shown\n"
    ctx = {"repos": [{"name": "vrixel-agtech/agtech-field-api", "role": "provider"},
                     {"name": "vrixel-agtech/agtech-mobile", "role": "consumer"}, {"name": ACC, "role": "acceptance"}],
           "parent_texts": {"PRD": prd}, "acceptance_catalog": catalog}
    plan = """## Acceptance tests
- AC-1: extend crops/log.spec.ts "a farmer logs a crop observation"
- AC-2: add crops/sync.spec.ts covers: agtech-field-api GET /api/v1/observations
- AC-3: replace crops/gone.spec.ts
"""
    findings = acceptance.check_plan(plan, ctx)
    assert any("AC-2: the new test in crops/sync.spec.ts covers nothing crops/log.spec.ts" in f for f in findings)
    assert any("AC-3: replace crops/gone.spec.ts, but the acceptance catalog has no such test file" in f for f in findings)
    assert not any(f.startswith("AC-1") for f in findings)

    ok = """## Acceptance tests
- AC-1: extend crops/log.spec.ts "a farmer logs a crop observation"
- AC-2: add crops/sync.spec.ts covers: agtech-field-api POST /api/v1/sync, agtech-field-api GET /api/v1/observations
- AC-3: add crops/conflicts.spec.ts covers: agtech-field-api GET /api/v1/observations because: needs two devices offline at once
"""
    assert acceptance.check_plan(ok, ctx) == []
    assert any("AC-3 has no acceptance test" in f for f in acceptance.check_plan(ok.rsplit("- AC-3", 1)[0], ctx))
    assert "no Acceptance tests section" in acceptance.check_plan("## Tasks\n", ctx)[0]


def test_a_review_finds_tests_contained_in_others():
    pairs = acceptance.overlaps_in(acceptance.load_catalog(json.dumps(CATALOG)))
    assert pairs == [{"test": 'crops/list.spec.ts "a farmer lists observations"',
                      "within": 'crops/log.spec.ts "a farmer logs a crop observation"',
                      "covers": ["agtech-field-api GET /api/v1/observations"]}]


def test_the_run_reports_per_criterion_and_its_budget():
    class GH:
        def checks(self, *, repo, sha):
            return {"checks": [{"name": "acceptance / EMR-FEAT-1 / dev", "state": "success", "url": "u",
                                "summary": "3 passed, 0 failed\n- AC-1: passed\n- AC-2: passed\nDuration: 700s"},
                               {"name": "acceptance / EMR-FEAT-12 / dev", "state": "failure", "summary": ""}]}

    class S:
        github = GH()

    t = {"kind": "acceptance", "repo": "o/emr-acceptance", "sha": "s", "feature_id": "EMR-FEAT-1", "acs": ["AC-1", "AC-2", "AC-3"]}
    [r] = AcceptanceRuns(S()).collect("acceptance", {"budget_s": 600}, [t], "dev")
    assert r.state == "failed" and "no passing test: AC-3" in r.message and "over the 600s budget" in r.message
    assert r.details["criteria"] == {"AC-1": "passed", "AC-2": "passed", "AC-3": "missing"} and r.passed == 3

    t["acs"] = ["AC-1", "AC-2"]
    [r] = AcceptanceRuns(S()).collect("acceptance", {"budget_s": 600}, [t], "dev")
    assert r.state == "passed" and r.details["over_budget"]  # slow is reported, not blocking

    class Empty:
        class github:
            @staticmethod
            def checks(*, repo, sha):
                return {"checks": []}
    [r] = AcceptanceRuns(Empty()).collect("acceptance", {}, [t], "dev")
    assert r.state == "pending"


def test_suites_on_components_ignore_the_acceptance_repo(services):
    q = services.quality
    policy = {"e2e": {"kind": "test", "stage": "staging", "source": "github-checks", "required": True},
              "acceptance-staging": {"kind": "test", "stage": "staging", "source": "acceptance-runs",
                                     "applies_to": "acceptance", "required": True}}
    res = q.check("staging", policy, [{"repo": "o/api", "sha": "1"}], [])
    assert res["status"] == "passed" and [s["suite"] for s in res["suites"]] == ["e2e"]  # no run needed
    res = q.check("staging", policy, [{"repo": "o/api", "sha": "1"},
                                      {"kind": "acceptance", "repo": ACC, "sha": "2", "feature_id": "F", "acs": ["AC-1"]}], [])
    assert {(s["suite"], s["repo"]) for s in res["suites"]} == {("e2e", "o/api"), ("acceptance-staging", ACC)}
    assert isinstance(SuiteResult, type)


def test_a_design_across_repos_lists_the_acceptance_repo():
    from vrixel_sdlc.evaluators.checks import _acceptance
    ctx = {"repos": [{"name": ACC, "role": "acceptance"}]}
    two = [{"name": "o/api", "role": "provider"}, {"name": "o/web", "role": "consumer"}]
    assert "list vrixel-agtech/agtech-acceptance (acceptance)" in _acceptance(two, ctx)[0]
    assert _acceptance(two + [{"name": ACC, "role": "acceptance"}], ctx) == []
    assert _acceptance(two, {"repos": []}) == []


def test_the_review_command_lists_overlaps(tmp_path, capsys):
    from vrixel_sdlc.cli import main

    f = tmp_path / "catalog.json"
    f.write_text(json.dumps(CATALOG))
    assert main(["acceptance", "review", "--catalog", str(f)]) == 1
    out = capsys.readouterr().out
    assert "2 acceptance tests, 1 covered by another test" in out and "crops/list.spec.ts" in out
    f.write_text(json.dumps({"tests": CATALOG["tests"][:1]}))
    assert main(["acceptance", "review", "--catalog", str(f), "--json"]) == 0

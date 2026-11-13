"""Products' own agents: a minimum share of eval cases, and their evals with the trend at G6."""

from pathlib import Path

from valxel_sdlc.graph.pipeline import agent_evals
from valxel_sdlc.quality import SuiteResult, effective_policy, evaluate, load_quality

CFG = load_quality(Path(__file__).resolve().parents[1] / "config" / "quality.yaml")
AGENT = "acme-farm/farm-crop-agent"


def test_a_live_eval_run_may_miss_a_case_but_not_more_than_the_product_allows():
    problems: list[str] = []
    policy = effective_policy(CFG, "general", {"integration": {"min_pass_rate": 0.95}}, problems)
    assert not problems and policy["integration"]["min_pass_rate"] == 0.95
    ok = SuiteResult(suite="integration", kind="test", stage="dev", state="passed", repo=AGENT, passed=39, failed=1)
    assert evaluate("dev", {"integration": policy["integration"]}, [ok], [])["status"] == "passed"
    low = SuiteResult(suite="integration", kind="test", stage="dev", state="passed", repo=AGENT, passed=30, failed=10)
    res = evaluate("dev", {"integration": policy["integration"]}, [low], [])
    assert res["status"] == "failed" and res["test_failures"] == [
        f"integration passed 75% of its cases, below 95% in {AGENT}"]
    for bad in (1.5, "most", 0):
        problems = []
        effective_policy(CFG, "general", {"integration": {"min_pass_rate": bad}}, problems)
        assert problems == ["quality.integration.min_pass_rate must be a share between 0 and 1, e.g. 0.95"]


def quality(pr: tuple[int, int], dev: tuple[int, int]) -> dict:
    def row(suite, counts):
        return {"suite": suite, "repo": AGENT, "state": "passed", "passed": counts[0], "failed": counts[1]}

    return {"pr": {"suites": [row("evals", pr)]}, "dev": {"suites": [row("integration", dev)]}}


class Services:
    def __init__(self, states):
        self.states = states

    def suspects(self, product_id, repo):
        assert repo == AGENT
        return [{"feature_id": "FARM-FEAT-9"}, {"feature_id": "FARM-FEAT-4"}]  # this one first, then the last release

    def feature_state(self, fid):
        return self.states[fid]


def test_g6_shows_each_agent_s_evals_against_its_last_release():
    now = {"feature_id": "FARM-FEAT-9", "product_id": "FARM",
           "tasks": [{"repo": AGENT, "role": "agent"}, {"repo": "acme-farm/farm-field-api", "role": "provider"}],
           "quality": quality((12, 0), (19, 1))}
    svc = Services({"FARM-FEAT-4": {"quality": quality((10, 0), (20, 0))}})
    [row] = agent_evals(svc, now)
    assert row["repo"] == AGENT and row["previous_feature"] == "FARM-FEAT-4"
    assert row["stages"]["pr"] == {"suite": "evals", "passed": 12, "failed": 0, "rate": 1.0, "state": "passed"}
    assert row["stages"]["dev"]["rate"] == 0.95 and row["previous"]["dev"]["rate"] == 1.0
    assert row["change"] == {"pr": 0.0, "dev": -0.05}


def test_no_agent_repos_no_rows(engine):
    v = engine.auto_drive(engine.start(product_id="FARM", title="Offline crop log")["feature_id"])
    assert agent_evals(engine.services, engine._state(v["feature_id"])) == []

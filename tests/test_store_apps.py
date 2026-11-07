"""VX-18: mobile apps are released through the stores in stages, not deployed, and the production
rollout widens step by step while the release stays healthy."""

import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml
from langgraph.checkpoint.memory import InMemorySaver

from vrixel_sdlc import store, waits
from vrixel_sdlc.agents.templates import _endpoint_lines
from vrixel_sdlc.config import load_settings
from vrixel_sdlc.engine import FeatureEngine
from vrixel_sdlc.evaluators.checks import _endpoints
from vrixel_sdlc.graph.phase import security_instruction
from vrixel_sdlc.repo_roles import REPO_ORDER
from vrixel_sdlc.services import build_services

EXAMPLES = Path(__file__).resolve().parents[1] / "examples" / "governance"
APP, BFF = "acme-farm/farm-mobile", "acme-farm/farm-bff"


@pytest.fixture
def services(tmp_path):
    root = tmp_path / "governance"
    shutil.copytree(EXAMPLES, root)
    manifest = root / "acme-farm" / "farm-governance" / "product.yaml"
    data = yaml.safe_load(manifest.read_text())
    for r in data["repos"]:
        if r["name"] == APP:
            r["role"] = "mobile"
    data["repos"].append({"name": BFF, "role": "bff"})
    manifest.write_text(yaml.safe_dump(data, sort_keys=False))
    s = load_settings(mode="stub", database_url="", artifact_dir=tmp_path / "artifacts",
                      governance_source="local", governance_root=root)
    return build_services(s)


@pytest.fixture
def engine(services):
    return FeatureEngine(services, InMemorySaver())


class Clock:
    """Moves the wait checker's clock forward, as days pass for a real rollout."""

    def __init__(self, monkeypatch):
        self.offset = timedelta(0)
        clock = self

        class Later(datetime):
            @classmethod
            def now(cls, tz=None):
                return datetime.now(tz) + clock.offset

        monkeypatch.setattr(waits, "datetime", Later)

    def hours(self, h: float) -> None:
        self.offset += timedelta(hours=h)


def check_waits(engine, fid, times=4):
    for _ in range(times):
        cmd = engine._ready_wait(fid)
        if cmd is not None:
            engine._execute(fid, cmd)


def rollout_wait(engine, fid):
    return next((p for p in engine.pending(fid) if p["type"] == "external" and p["wait"] == "rollout"), None)


def released(services, kind="vrixel-release"):
    return [d for d in services.github.dispatches if d["event_type"] == kind and d["repo"] == APP]


def test_a_store_app_is_released_at_each_stage_then_rolled_out(engine, services, monkeypatch):
    v = engine.auto_drive(engine.start(product_id="FARM", title="Offline crop log")["feature_id"])
    fid = v["feature_id"]
    prs = {pr["repo"]: pr for pr in v["pull_requests"].values()}
    assert prs[APP]["state"] == "merged"
    assert APP not in v["runs"]  # no OpenChoreo build: the stores are its runtime
    stages = [(d["payload"]["stage"], d["payload"]["sha"], d["payload"]["actor"]) for d in released(services)]
    sha = prs[APP]["merge_sha"]
    assert stages == [("dev", sha, "agent:dev"), ("staging", sha, "agent:staging"), ("production", sha, "auto-approver")]
    assert not [d for d in services.github.dispatches if d["event_type"] == "vrixel-deployed" and d["repo"] == APP]

    # Production is submitted; the first step waits a day for the store's vitals.
    w = rollout_wait(engine, fid)
    assert w is not None and w["targets"][0]["context"] == "rollout / 10%"
    assert datetime.fromisoformat(w["targets"][0]["not_before"]) > datetime.now(timezone.utc) + timedelta(hours=23)
    check_waits(engine, fid)
    assert not released(services, "vrixel-rollout")  # not before its time

    clock = Clock(monkeypatch)
    for _ in store.DEFAULT_ROLLOUT["steps"]:
        clock.hours(25)
        check_waits(engine, fid)
    steps = [d["payload"] for d in released(services, "vrixel-rollout")]
    assert [p["fraction"] for p in steps] == ["0.1", "0.5", "1.0"]
    assert {p["sha"] for p in steps} == {sha}
    s = engine.graph.get_state(engine._config(fid)).values
    assert [h["fraction"] for h in s["rollout"]["history"]] == [0.1, 0.5, 1.0]
    assert s["status"] == "done"


def test_an_unhealthy_release_is_halted_and_goes_to_people(engine, services, monkeypatch):
    fid = engine.auto_drive(engine.start(product_id="FARM", title="Offline crop log")["feature_id"])["feature_id"]
    services.github.release_result = "failure"
    Clock(monkeypatch).hours(25)
    check_waits(engine, fid)
    halts = [d for d in released(services, "vrixel-rollout") if d["payload"].get("halt")]
    assert len(halts) == 1
    esc = next(p for p in engine.pending(fid) if p["type"] == "escalation")
    assert esc["wait"] == "rollout" and "crash-free" in " ".join(esc["findings"])

    # Retrying checks again right away, under a new status context; then the rollout carries on.
    services.github.release_result = "success"
    engine.decide(fid, esc["interrupt_id"], {"decision": "retry", "approver": "rm"})
    check_waits(engine, fid)
    retried = [d["payload"]["context"] for d in released(services, "vrixel-rollout") if not d["payload"].get("halt")]
    assert retried == ["rollout / 10%", "rollout / 10% (attempt 2)"]
    s = engine.graph.get_state(engine._config(fid)).values
    assert s["rollout"]["step"] == 1 and s["rollout"]["history"] == [
        {"fraction": 0.1, "at": s["rollout"]["at"], "attempt": 1}]
    assert rollout_wait(engine, fid)["targets"][0]["context"] == "rollout / 50%"


def test_a_failed_store_build_goes_to_the_release_managers(engine, services):
    services.github.release_result = "failure"
    v = engine.auto_drive(engine.start(product_id="FARM", title="Offline crop log")["feature_id"], max_steps=12,
                          decisions={"escalation:delivery": {"decision": "abort", "feedback": "store build broken"}})
    assert v["status"] == "aborted"
    assert [d["payload"]["stage"] for d in released(services)] == ["dev"]


def test_rollout_policy_is_checked():
    assert store.rollout_policy(None)["steps"] == [0.1, 0.5, 1.0]
    tight = store.rollout_policy({"roles": {"mobile": {"rollout": {"steps": [0.01, 0.2, 1], "min_hours": 48}}}})
    assert tight["steps"] == [0.01, 0.2, 1.0] and tight["min_hours"] == 48
    for bad in ({"steps": [0.5, 0.2, 1]}, {"steps": [0.1, 0.5]}, {"steps": []}, {"min_hours": 0}):
        with pytest.raises(ValueError):
            store.rollout_policy({"roles": {"mobile": {"rollout": bad}}})
    assert store.percent(0.01) == "1%" and store.percent(0.025) == "2.5%"
    assert store.rollout_context(0.5, 1) == "rollout / 50% (attempt 2)"


REPOS = [
    {"name": "o/farm-api", "role": "provider"},
    {"name": "o/farm-bff", "role": "bff"},
    {"name": "o/farm-mobile", "role": "mobile"},
]


def hld(endpoints: str) -> str:
    return f"# HLD\n\n## ADRs\n- ADR-1: Add the crop log\n\n## Endpoints\n{endpoints}\n"


def test_a_mobile_app_reaches_the_product_only_through_the_bff():
    lines = _endpoint_lines(REPOS)
    assert "- o/farm-mobile: none; calls farm-bff" in lines
    assert _endpoints(hld("\n".join(lines)), REPOS, {"repos": REPOS}) == []

    direct = hld("- o/farm-bff: external; calls farm-api\n- o/farm-api: project\n- o/farm-mobile: none; calls farm-api, farm-bff\n")
    assert any("calls farm-api directly" in f for f in _endpoints(direct, REPOS, {"repos": REPOS}))
    exposed = hld("- o/farm-bff: external; calls farm-api\n- o/farm-api: project\n- o/farm-mobile: external; calls farm-bff\n")
    assert any("visibility is none" in f for f in _endpoints(exposed, REPOS, {"repos": REPOS}))
    no_bff = [r for r in REPOS if r["role"] != "bff"]
    lonely = hld("- o/farm-api: project\n- o/farm-mobile: none\n")
    assert any("has no BFF" in f for f in _endpoints(lonely, no_bff, {"repos": no_bff}))


def test_mobile_coders_are_told_their_rules():
    rules = security_instruction({}, {"role": "mobile"})
    assert "outbox" in rules and "/mobile/api" in rules and "VX-18" in rules
    assert "BFF_MOBILE_AUDIENCE" in security_instruction({}, {"role": "bff"})
    assert REPO_ORDER["bff"] < REPO_ORDER["mobile"] < REPO_ORDER["acceptance"]


def test_store_waits_read_the_release_status():
    class GH:
        def __init__(self):
            self.statuses, self.sent = {}, []

        def checks(self, *, repo, sha):
            return {"checks": [{"name": k, "state": v, "summary": "s", "url": ""} for k, v in self.statuses.items()]}

        def dispatch(self, *, repo, event_type, payload):
            self.sent.append(payload)

    class S:
        github = GH()

    w = waits.WaitChecker(S())
    t = {"repo": "o/app", "sha": "abc", "kind": "store"}
    assert w._one("staging", t)["state"] == "pending"
    S.github.statuses["release / staging"] = "failure"
    assert w._one("staging", t) == {"state": "failure", "url": "", "message": "s"}

    step = {"repo": "o/app", "sha": "abc", "fraction": 0.1, "context": "rollout / 10%", "recheck_hours": 6,
            "not_before": (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()}
    assert w._one("rollout", step)["state"] == "pending"
    assert w._one("rollout", step)["state"] == "pending"
    assert len(S.github.sent) == 1  # asked once, again only after recheck_hours
    S.github.statuses["rollout / 10%"] = "success"
    assert w._one("rollout", step)["state"] == "success"

"""Delivery: GitHub pull requests and CI, OpenChoreo builds and deployments, and the waits that
pause the pipeline on them. Adapters are exercised against a fake GitHub REST API and a fake
OpenChoreo control-plane MCP server."""

import base64
import hashlib
import hmac
import json
import re
import shutil
import time
from io import BytesIO
from pathlib import Path
from urllib.error import HTTPError

import pytest
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import InMemorySaver
from mcp.server import MCPServer

from vrixel_sdlc.api.app import create_app
from vrixel_sdlc.config import load_settings
from vrixel_sdlc.engine import FeatureEngine
from vrixel_sdlc.governance import GovernanceError
from vrixel_sdlc.integrations.github import GitHubRest
from vrixel_sdlc.integrations.mcp_client import McpHttp
from vrixel_sdlc.integrations.openchoreo import OpenChoreoMcp
from vrixel_sdlc.services import DeliveryConfigError, build_services, default_delivery
from vrixel_sdlc.waits import WaitChecker
from vrixel_sdlc.web.auth import AuthConfig

EXAMPLES = Path(__file__).resolve().parents[1] / "examples" / "governance"


@pytest.fixture
def gov_root(tmp_path):
    root = tmp_path / "governance"
    shutil.copytree(EXAMPLES, root)
    return root


@pytest.fixture
def services(tmp_path, gov_root):
    s = load_settings(mode="stub", database_url="", artifact_dir=tmp_path / "artifacts",
                      governance_source="local", governance_root=gov_root)
    return build_services(s)


@pytest.fixture
def engine(services):
    return FeatureEngine(services, InMemorySaver())


def wait_idle(engine, fid, timeout=20.0):
    end = time.time() + timeout
    time.sleep(0.05)
    while fid in engine.running and time.time() < end:
        time.sleep(0.05)
    assert fid not in engine.running


def external(engine, fid):
    return [p for p in engine.pending(fid) if p["type"] == "external"]


def to_build(engine, product="AG", title="Offline crop log"):
    """Start a feature and approve gates until it waits on something other than a gate."""
    v = engine.start(product_id=product, title=title, description="Record observations offline")
    fid = v["feature_id"]
    while True:
        gates = [p for p in engine.pending(fid) if p["type"] == "gate"]
        if not gates or any(p["type"] != "gate" for p in engine.pending(fid)):
            return engine.view(fid)
        engine.decide(fid, gates[0]["interrupt_id"], {"decision": "approve", "approver": "maria"})


# --------------------------------------------------------------------------- waits in the pipeline


def test_red_ci_holds_the_feature_without_bothering_people(engine, services):
    services.github.ci_default = "pending"
    v = to_build(engine)
    fid = v["feature_id"]
    (wait,) = external(engine, fid)
    assert wait["wait"] == "ci" and {t["repo"] for t in wait["targets"]} == {
        "vrixel-agtech/agtech-field-api", "vrixel-agtech/agtech-mobile"}
    assert engine.inbox() == []  # nothing for a person to decide
    assert next(s for s in v["rail"] if s["key"] == "build")["state"] == "current"
    with pytest.raises(RuntimeError, match="waiting on CI checks"):
        engine.decide(fid, wait["interrupt_id"], {"decision": "approve"})
    assert engine.poke(fid) is False  # still pending

    services.github.ci_default = "success"
    assert engine.poke(fid) is True
    wait_idle(engine, fid)
    v = engine.auto_drive(fid, approver="maria")
    assert v["status"] == "done" and v["delivery"]["ci"]["status"] == "success"


def test_failed_ci_goes_back_to_people_who_can_let_agents_retry(engine, services):
    services.github.ci_default = "failure"
    v = to_build(engine)
    fid = v["feature_id"]
    (esc,) = engine.pending(fid)
    assert esc["type"] == "escalation" and esc["phase"] == "build" and "CI checks failed" in esc["reason"]
    services.github.ci_default = "success"  # the agents' next attempt fixes it
    engine.decide(fid, esc["interrupt_id"], {"decision": "retry", "approver": "lead", "feedback": "fix the test"})
    v = engine.auto_drive(fid)
    assert v["status"] == "done"


def test_a_person_can_vouch_for_ci(engine, services):
    services.github.ci_default = "failure"
    v = to_build(engine)
    (esc,) = engine.pending(v["feature_id"])
    engine.decide(v["feature_id"], esc["interrupt_id"], {"decision": "resolved", "approver": "lead",
                                                         "feedback": "flaky check, known issue"})
    v = engine.auto_drive(v["feature_id"])
    assert v["status"] == "done"


def test_builds_run_on_the_merge_commit_and_report_back_to_github(engine, services):
    v = engine.auto_drive(to_build(engine)["feature_id"])
    runs = v["runs"]
    assert set(runs) == {"vrixel-agtech/agtech-field-api", "vrixel-agtech/agtech-mobile"}
    for repo, run in runs.items():
        pr = next(p for p in v["pull_requests"].values() if p["repo"] == repo)
        assert run["sha"] == pr["merge_sha"] and run["project"] == "agtech" and run["state"] == "success"
    contexts = {(s["repo"], s["context"]) for s in services.github.statuses}
    assert ("vrixel-agtech/agtech-mobile", "openchoreo/build") in contexts
    assert ("vrixel-agtech/agtech-mobile", "openchoreo/production") in contexts
    files = [f[2] for f in services.github.files]
    assert ".vrixel/changes/AG-FEAT-1/TASK-1.md" in files


def test_failed_build_asks_release_managers_and_retry_rebuilds(engine, services, tmp_path):
    services.openchoreo.run_results["vrixel-agtech/agtech-mobile"] = "failed"
    v = to_build(engine)
    fid = v["feature_id"]
    (esc,) = engine.pending(fid)
    assert esc["type"] == "escalation" and esc["phase"] == "delivery" and esc["wait"] == "build"
    assert "agtech-mobile" in esc["reason"]
    runs_before = len(services.openchoreo.runs)
    services.openchoreo.run_results.clear()
    engine.decide(fid, esc["interrupt_id"], {"decision": "retry", "approver": "rm"})
    assert len(services.openchoreo.runs) == runs_before + 1  # only the failed component rebuilt
    v = engine.auto_drive(fid)
    assert v["status"] == "done"
    assert any(g["gate"] == "escalation:delivery:build" for g in v["gate_decisions"])


def test_staging_rollout_waits_until_ready(engine, services):
    services.openchoreo.deploy_results[("vrixel-agtech/agtech-field-api", "staging")] = "progressing"
    v = to_build(engine)
    fid = v["feature_id"]
    (wait,) = external(engine, fid)
    assert wait["wait"] == "staging"
    assert next(s for s in v["rail"] if s["key"] == "release")["state"] == "current"
    services.openchoreo.deploy_results.clear()
    assert engine.poke(fid)
    wait_idle(engine, fid)
    v = engine.view(fid)
    assert v["pending"][0]["gate"] == "g6"
    assert {d["env"] for d in v["deployments"].values()} == {"staging"}
    release = engine.artifact(fid, "REL")["content"]
    assert "vrixel-agtech/agtech-mobile: staging, release agtech-mobile-r1" in release


def test_production_is_bound_for_the_human_approver(engine, services):
    v = to_build(engine)  # maria approves every gate, G6 included
    prod = [p for p in services.openchoreo.promotions if p["env"] == "production"]
    assert {p["actor"] for p in prod} == {"maria"} and {p["actor_type"] for p in prod} == {"human"}
    assert {p["actor_type"] for p in services.openchoreo.promotions if p["env"] != "production"} == {"agent"}
    assert {d["env"] for d in v["deployments"].values()} == {"production"}


def test_waits_time_out_into_a_failure():
    class Slow:
        class github:
            @staticmethod
            def checks(repo, sha):
                return {"state": "pending", "checks": []}

    res = WaitChecker(Slow()).check({"wait": "ci", "since": "2020-01-01T00:00:00+00:00", "timeout_s": 60,
                                     "targets": [{"repo": "o/r", "number": 1, "sha": "abc"}]})
    assert res["status"] == "failure" and "not done after 1 minutes" in res["reason"]


def test_shared_lane_publishes_packages_and_is_not_deployed(engine, services):
    v = engine.start(product_id="AG", title="Audit trail for field edits",
                     description="Record who changed a field, with the shared audit framework")
    v = engine.auto_drive(v["feature_id"])
    assert v["status"] == "done"
    assert "vrixel-shared-services/ss-audit-framework" not in v["runs"]
    assert "vrixel-shared-services/ss-audit-framework" not in v["deployments"]


# --------------------------------------------------------------------------- GitHub REST adapter


class FakeResp(BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeGitHubApi:
    """Just enough of the GitHub REST API for the delivery adapter."""

    def __init__(self):
        self.refs = {("o/r", "main"): "base000"}
        self.files: dict[tuple[str, str, str], tuple[str, str]] = {}
        self.pulls: dict[int, dict] = {}
        self.statuses: list[dict] = []
        self.check_runs: dict[str, list[dict]] = {}
        self.calls: list[tuple[str, str]] = []

    def open(self, req, timeout=None):
        method, url = req.get_method(), req.full_url
        path = url.split("https://api.github.com", 1)[1]
        body = json.loads(req.data.decode()) if req.data else None
        self.calls.append((method, path))
        status, out = self.route(method, path, body)
        if status >= 400:
            raise HTTPError(url, status, "err", {}, BytesIO(json.dumps({"message": out}).encode()))
        return FakeResp(json.dumps(out).encode())

    def route(self, m, p, b):
        repo = "o/r"
        p = p.replace(f"/repos/{repo}", "", 1)
        if m == "GET" and p == "":
            return 200, {"default_branch": "main"}
        if mt := re.fullmatch(r"/git/ref/heads/(.+)", p):
            sha = self.refs.get((repo, mt.group(1).replace("%2F", "/")))
            return (200, {"object": {"sha": sha}}) if sha else (404, "Not Found")
        if m == "POST" and p == "/git/refs":
            self.refs[(repo, b["ref"].removeprefix("refs/heads/"))] = b["sha"]
            return 201, {}
        if mt := re.fullmatch(r"/contents/(.+?)(\?ref=(.+))?", p):
            path = mt.group(1).replace("%2F", "/")
            if m == "GET":
                hit = self.files.get((repo, mt.group(3).replace("%2F", "/"), path))
                return (200, {"sha": hit[1], "content": base64.b64encode(hit[0].encode()).decode()}) if hit \
                    else (404, "Not Found")
            text = base64.b64decode(b["content"]).decode()
            sha = hashlib.sha1(text.encode()).hexdigest()
            self.files[(repo, b["branch"], path)] = (text, sha)
            self.refs[(repo, b["branch"])] = sha
            return 200, {"commit": {"sha": sha}}
        if m == "GET" and p.startswith("/pulls?"):
            head = p.split("head=o:", 1)[1].replace("%2F", "/")
            return 200, [pr for pr in self.pulls.values() if pr["head"]["ref"] == head]
        if m == "POST" and p == "/pulls":
            n = 100 + len(self.pulls)
            self.pulls[n] = {"number": n, "title": b["title"], "body": b["body"], "state": "open", "merged": False,
                             "merged_at": None, "labels": [], "html_url": f"https://github.com/o/r/pull/{n}",
                             "head": {"ref": b["head"], "sha": self.refs[(repo, b["head"])]},
                             "merge_commit_sha": None}
            return 201, self.pulls[n]
        if mt := re.fullmatch(r"/pulls/(\d+)", p):
            pr = self.pulls[int(mt.group(1))]
            if m == "PATCH":
                pr.update(b)
            return 200, pr
        if mt := re.fullmatch(r"/issues/(\d+)/labels", p):
            self.pulls[int(mt.group(1))]["labels"] = [{"name": n} for n in b["labels"]]
            return 200, []
        if mt := re.fullmatch(r"/pulls/(\d+)/merge", p):
            pr = self.pulls[int(mt.group(1))]
            if b["sha"] != pr["head"]["sha"]:
                return 409, "Head branch was modified"
            pr.update(merged=True, merged_at="now", state="closed", merge_commit_sha="m3rge")
            return 200, {"merged": True}
        if mt := re.fullmatch(r"/commits/(\w+)/check-runs\?per_page=100", p):
            return 200, {"check_runs": self.check_runs.get(mt.group(1), [])}
        if mt := re.fullmatch(r"/commits/(\w+)/status", p):
            return 200, {"statuses": [s for s in self.statuses if s["sha"] == mt.group(1)]}
        if mt := re.fullmatch(r"/statuses/(\w+)", p):
            self.statuses.append({"sha": mt.group(1), **b})
            return 201, {}
        return 404, f"no route {m} {p}"


class Tokens:
    def token(self, org):
        assert org == "o"
        return "t0k"


def test_github_adapter_branch_note_pr_checks_and_merge():
    api = FakeGitHubApi()
    gh = GitHubRest(Tokens(), opener=api)
    first = gh.put_file(repo="o/r", branch="feature/X-1", path=".vrixel/changes/X-1/TASK-1.md",
                        content="note", message="m")
    assert api.refs[("o/r", "feature/X-1")] == first["sha"]
    assert gh.put_file(repo="o/r", branch="feature/X-1", path=".vrixel/changes/X-1/TASK-1.md",
                       content="note", message="m")["unchanged"]

    pr = gh.open_pr(repo="o/r", branch="feature/X-1", title="X-1 TASK-1", body="b", files=[], labels=["product:AG"])
    again = gh.open_pr(repo="o/r", branch="feature/X-1", title="X-1 TASK-1", body="b", files=[], labels=["product:AG"])
    assert pr["number"] == again["number"] and len(api.pulls) == 1  # found by head branch, not duplicated
    assert pr["head_sha"] == first["sha"] and again["labels"] == ["product:AG"]

    sha = pr["head_sha"]
    assert gh.checks(repo="o/r", sha=sha)["state"] == "success"  # no CI configured
    api.check_runs[sha] = [{"name": "test", "status": "in_progress", "conclusion": None, "html_url": "u"}]
    assert gh.checks(repo="o/r", sha=sha)["state"] == "pending"
    api.check_runs[sha] = [{"name": "test", "status": "completed", "conclusion": "failure", "html_url": "u"}]
    assert gh.checks(repo="o/r", sha=sha)["state"] == "failure"
    api.check_runs[sha] = [{"name": "test", "status": "completed", "conclusion": "success", "html_url": "u"}]
    gh.set_status(repo="o/r", sha=sha, context="openchoreo/build", state="failure", description="x")
    assert gh.checks(repo="o/r", sha=sha)["state"] == "success"  # our own statuses are not CI

    merged = gh.merge(repo="o/r", number=pr["number"], actor="maria")
    assert merged["state"] == "merged" and merged["merge_sha"] == "m3rge"
    assert gh.merge(repo="o/r", number=pr["number"], actor="maria")["state"] == "merged"
    assert [c for c in api.calls if c[1].endswith("/merge")] == [("PUT", f"/repos/o/r/pulls/{pr['number']}/merge")]


# --------------------------------------------------------------------------- OpenChoreo MCP adapter


def fake_openchoreo():
    server = MCPServer("openchoreo")
    st = {"runs": {}, "bindings": {}, "latest": None, "calls": []}

    @server.tool()
    def trigger_workflow_run(namespace_name: str, project_name: str, component_name: str, commit: str = "") -> dict:
        st["calls"].append(("trigger", namespace_name, project_name, component_name, commit))
        name = f"{component_name}-run-{len(st['runs']) + 1}"
        st["runs"][name] = "Running"
        return {"name": name}

    @server.tool()
    def get_workflow_run_status(namespace_name: str, run_name: str) -> dict:
        phase = st["runs"][run_name]
        conds = [{"type": "WorkflowSucceeded", "status": "True"}] if phase == "Succeeded" else (
            [{"type": "WorkflowFailed", "status": "True"}] if phase == "Failed" else [])
        tasks = [{"name": "build", "phase": "Failed", "message": "image build failed"}] if phase == "Failed" else []
        return {"status": {"conditions": conds, "tasks": tasks}}

    @server.tool()
    def get_component(namespace_name: str, project_name: str, component_name: str) -> dict:
        return {"status": {"latestRelease": {"name": st["latest"]}} if st["latest"] else {}}

    @server.tool()
    def list_release_bindings(namespace_name: str, project_name: str, component_name: str) -> dict:
        return {"items": [b for b in st["bindings"].values() if b["component"] == component_name]}

    @server.tool()
    def create_release_binding(namespace_name: str, project_name: str, component_name: str, environment: str,
                               release_name: str) -> dict:
        st["calls"].append(("bind", component_name, environment, release_name))
        st["bindings"][(component_name, environment)] = {
            "name": f"{component_name}-{environment}", "component": component_name, "environment": environment,
            "releaseName": release_name, "status": {"conditions": [{"type": "Ready", "status": "False"}]}}
        return st["bindings"][(component_name, environment)]

    @server.tool()
    def update_release_binding(namespace_name: str, binding_name: str, release_name: str,
                               release_state: str = "Active") -> dict:
        st["calls"].append(("rebind", binding_name, release_name, release_state))
        b = next(x for x in st["bindings"].values() if x["name"] == binding_name)
        b["releaseName"] = release_name
        return b

    return server, st


def test_openchoreo_adapter_builds_deploys_and_promotes():
    server, st = fake_openchoreo()
    oc = OpenChoreoMcp(McpHttp("openchoreo", server), default_namespace="vrixel")
    run = oc.run_workflow(component="vrixel-agtech/agtech-field-api", ref="m3rge", kind="main", project="agtech")
    assert st["calls"][0] == ("trigger", "vrixel", "agtech", "agtech-field-api", "m3rge")
    scope = {"component": "vrixel-agtech/agtech-field-api", "project": "agtech"}
    assert oc.run_status(run_id=run["run_id"], **scope)["status"] == "running"
    st["runs"][run["run_id"]] = "Failed"
    failed = oc.run_status(run_id=run["run_id"], **scope)
    assert failed["status"] == "failed" and "image build failed" in failed["message"]
    st["runs"][run["run_id"]] = "Succeeded"
    assert oc.run_status(run_id=run["run_id"], **scope)["status"] == "succeeded"

    st["latest"] = "agtech-field-api-7f3a"
    oc.promote(env="dev", actor="agent:dev", actor_type="agent", **scope)
    assert oc.deployment(env="dev", **scope)["state"] == "progressing"
    st["bindings"][("agtech-field-api", "dev")]["status"]["conditions"][0]["status"] = "True"
    assert oc.deployment(env="dev", **scope) == {"state": "ready", "release": "agtech-field-api-7f3a", "url": ""}
    assert oc.deployment(env="staging", **scope)["state"] == "absent"

    rec = oc.promote(env="staging", actor="agent:staging", actor_type="agent", **scope)
    assert rec["release"] == "agtech-field-api-7f3a"  # the release dev runs, not a rebuild
    with pytest.raises(PermissionError):
        oc.promote(env="production", actor="agent:x", actor_type="agent", **scope)
    oc.promote(env="production", actor="rm", actor_type="human", **scope)
    assert ("bind", "agtech-field-api", "production", "agtech-field-api-7f3a") in st["calls"]

    st["latest"] = "agtech-field-api-9b21"  # a later build redeploys dev by updating its binding
    oc.promote(env="dev", actor="agent:dev", actor_type="agent", **scope)
    assert ("rebind", "agtech-field-api-dev", "agtech-field-api-9b21", "Active") in st["calls"]


# --------------------------------------------------------------------------- wiring


def test_runtime_component_names_are_validated(tmp_path, gov_root):
    path = gov_root / "vrixel-agtech" / "ag-governance" / "product.yaml"
    path.write_text(path.read_text().replace(
        "  pipeline: [dev, staging, production]",
        "  pipeline: [dev, staging, production]\n  namespace: vrixel\n  components:\n"
        "    vrixel-agtech/agtech-mobile: field-app\n    vrixel-emr/emr-web: nope"))
    s = load_settings(mode="stub", database_url="", artifact_dir=tmp_path / "a",
                      governance_source="local", governance_root=gov_root)
    with pytest.raises(GovernanceError, match="vrixel-emr/emr-web, which is not one of this product's repos"):
        build_services(s).snapshot("AG")


def test_component_mapping_and_namespace_reach_openchoreo(tmp_path, gov_root):
    path = gov_root / "vrixel-agtech" / "ag-governance" / "product.yaml"
    path.write_text(path.read_text().replace(
        "  pipeline: [dev, staging, production]",
        "  pipeline: [dev, staging, production]\n  namespace: vrixel\n  components:\n"
        "    vrixel-agtech/agtech-mobile: field-app"))
    s = load_settings(mode="stub", database_url="", artifact_dir=tmp_path / "a",
                      governance_source="local", governance_root=gov_root)
    eng = FeatureEngine(build_services(s), InMemorySaver())
    v = eng.auto_drive(to_build(eng)["feature_id"])
    run = v["runs"]["vrixel-agtech/agtech-mobile"]
    assert run["component"] == "field-app" and run["namespace"] == "vrixel"


def test_live_delivery_lists_what_is_missing(monkeypatch, tmp_path, gov_root):
    monkeypatch.setenv("VRIXEL_DELIVERY", "live")
    for v in ("VRIXEL_GITHUB_APP_ID", "VRIXEL_OPENCHOREO_MCP_URL", "VRIXEL_OPENCHOREO_CLIENT_SECRET"):
        monkeypatch.delenv(v, raising=False)
    s = load_settings(mode="stub", database_url="", artifact_dir=tmp_path / "a",
                      governance_source="local", governance_root=gov_root)
    with pytest.raises(DeliveryConfigError, match="VRIXEL_GITHUB_APP_ID.*VRIXEL_OPENCHOREO_MCP_URL"):
        default_delivery(s)


def test_github_webhook_is_verified_and_resumes_the_feature(monkeypatch, engine, services):
    monkeypatch.setenv("VRIXEL_GITHUB_WEBHOOK_SECRET", "s3cret")
    auth = AuthConfig(mode="dev", secret="x" * 32)
    client = TestClient(create_app(engine=engine, auth=auth, web_dist=None))
    services.github.ci_default = "pending"
    fid = to_build(engine)["feature_id"]
    body = json.dumps({"check_suite": {"head_branch": f"feature/{fid}"}}).encode()
    bad = client.post("/api/webhooks/github", content=body, headers={"x-hub-signature-256": "sha256=0"})
    assert bad.status_code == 401
    services.github.ci_default = "success"
    sig = "sha256=" + hmac.new(b"s3cret", body, hashlib.sha256).hexdigest()
    ok = client.post("/api/webhooks/github", content=body,
                     headers={"x-hub-signature-256": sig, "x-github-event": "check_suite",
                              "content-type": "application/json"})
    assert ok.status_code == 202 and ok.json()["resumed"] == [fid]
    wait_idle(engine, fid)
    assert not external(engine, fid)


def test_ready_deployments_start_each_repos_post_deploy_suite(engine, services):
    v = to_build(engine)
    sent = [(d["repo"], d["payload"]["stage"]) for d in services.github.dispatches]
    for repo in ("vrixel-agtech/agtech-field-api", "vrixel-agtech/agtech-mobile"):
        assert {(repo, "dev"), (repo, "staging"), (repo, "production")} <= set(sent)
    d = services.github.dispatches[0]
    assert d["event_type"] == "vrixel-deployed" and d["payload"]["sha"] == v["runs"][d["repo"]]["sha"]
    assert len(sent) == len(set(sent))  # once per stage and repo

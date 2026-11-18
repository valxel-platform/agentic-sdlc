"""Agent sessions in sandboxes of their own (valxel_sdlc.session_runner): what crosses into the
sandbox, what the runner does there, the Kubernetes executor's claim, and the runtime's remote path."""

import json
from pathlib import Path

import pytest

from valxel_sdlc import sandbox, session_runner, sessions
from valxel_sdlc.gateway import CallTags, PromptLayers
from valxel_sdlc.runtime.base import AgentTask


def _task(home="", **kw):
    layers = PromptLayers(tools="", skills="S", constitution="C", product_context="P", feature_context="F",
                          tail="Implement T1")
    tags = CallTags(product_id="FARM", feature_id="FARM-FEAT-1", phase="build", role="coder", data_class="general",
                    workspace="wrkspc-shared")
    return AgentTask(role="coder", phase="build", tags=tags, layers=layers, sandbox={"home": home, **kw.pop("box", {})},
                     **kw)


class FakeSDK:
    """Stands in for Claude Code: records what it was asked, writes a transcript, answers."""

    def __init__(self, fail_resume=False):
        self.calls, self.fail_resume = [], fail_resume

    def __call__(self, prompt, options):
        self.calls.append((prompt, options))
        if options.get("resume") and self.fail_resume:
            raise RuntimeError("no such session")
        home = options["env"]["HOME"]
        sid = options.get("resume") or "sess-1"
        t = sessions.claude_project_dir(home, options["cwd"]) / f"{sid}.jsonl"
        t.parent.mkdir(parents=True, exist_ok=True)
        t.write_text('{"type":"user"}\n')
        return {"text": '{"summary": "done"}', "usage": {"input_tokens": 100, "output_tokens": 20},
                "peak": 100, "session_id": sid, "skills": ["engineering:debug"], "plugin_errors": [],
                "is_error": False, "error": "", "model_usage": {}, "total_cost_usd": 0.01, "duration_s": 1.0}


# --------------------------------------------------------------------------- what crosses

def test_only_the_session_crosses_into_the_sandbox(tmp_path, monkeypatch):
    (tmp_path / "feature").mkdir()
    (tmp_path / "feature" / "PRD.md").write_text("# PRD")
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:secret@db/x")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-1")
    env = sandbox.session_env("claude_code", str(tmp_path))
    options = {"system_prompt": {"type": "preset"}, "model": "m", "cwd": "/local", "settings": "{}", "env": env,
               "plugins": [{"type": "local", "path": "/x"}], "max_turns": 9}
    checkout = {"repo": "acme-farm/farm-api", "branch": "feature/FARM-FEAT-1", "token": "ghs_one_repo"}
    spec = session_runner.spec_for(_task(str(tmp_path), box={"checkout": checkout}), options)
    assert spec["prompt"] == "Implement T1" and spec["checkout"] == checkout
    assert spec["files"] == {"feature/PRD.md": "# PRD"}
    assert spec["options"] == {"system_prompt": {"type": "preset"}, "model": "m", "max_turns": 9}  # runner sets the rest
    assert spec["env"]["ANTHROPIC_API_KEY"] == "sk-ant-1"  # until sessions get gateway tokens (T-06)
    assert "DATABASE_URL" not in spec["env"] and "HOME" not in spec["env"]
    assert "secret" not in json.dumps(spec)


# --------------------------------------------------------------------------- in the sandbox

def _spec(**kw):
    base = {"version": 1, "runtime": "claude_code", "id": "s1", "prompt": "Implement T1", "options": {"model": "m"},
            "env": {"ANTHROPIC_API_KEY": "k"}, "plugins": False, "checkout": None, "read_checkouts": [],
            "files": {"feature/PRD.md": "# PRD"}, "resume": None,
            "fallback": {"prompt": "Start over from the handoff", "session_mode": "handoff"},
            "keep_transcript": True, "timeout_s": 60}
    return {**base, **kw}


def test_the_runner_runs_the_session_in_its_own_folders(tmp_path):
    sdk = FakeSDK()
    out = session_runner.run_spec(_spec(), tmp_path, query=sdk)
    [(prompt, opts)] = sdk.calls
    assert prompt == "Implement T1" and opts["model"] == "m"
    assert opts["cwd"] == str(tmp_path / "work") and opts["env"]["HOME"] == str(tmp_path / "home")
    assert opts["env"]["npm_config_ignore_scripts"] == "true" and json.loads(opts["settings"])["disableAllHooks"]
    assert (tmp_path / "home" / "feature" / "PRD.md").read_text() == "# PRD"
    assert out["session_id"] == "sess-1" and out["transcript"] and out["resumed"] is False


def test_a_resume_restores_the_transcript_and_falls_back_to_the_handoff(tmp_path):
    spec = _spec(resume={"session_id": "sess-0", "transcript": '{"type":"user"}\n'})
    sdk = FakeSDK()
    out = session_runner.run_spec(spec, tmp_path / "a", query=sdk)
    assert sdk.calls[0][1]["resume"] == "sess-0" and out["resumed"] and out["session_mode"] == "resumed"
    sdk = FakeSDK(fail_resume=True)
    out = session_runner.run_spec(spec, tmp_path / "b", query=sdk)
    assert [c[0] for c in sdk.calls] == ["Implement T1", "Start over from the handoff"]
    assert out["resumed"] is False and out["session_mode"] == "handoff"


def test_the_runner_clones_with_the_one_repo_token(tmp_path, monkeypatch):
    got = {}

    def fake_clone(repo, branch, token, root):
        got.update(repo=repo, branch=branch, token=token)
        (Path(root) / "work" / "farm-api").mkdir(parents=True)
        return str(Path(root) / "work" / "farm-api")

    monkeypatch.setattr(sandbox, "clone", fake_clone)
    sdk = FakeSDK()
    session_runner.run_spec(_spec(checkout={"repo": "acme-farm/farm-api", "branch": "feature/F-1", "token": "t"}),
                            tmp_path, query=sdk)
    assert got == {"repo": "acme-farm/farm-api", "branch": "feature/F-1", "token": "t"}
    assert sdk.calls[0][1]["cwd"].endswith("work/farm-api")


def test_a_file_outside_the_sessions_home_is_refused(tmp_path):
    with pytest.raises(ValueError, match="outside"):
        session_runner.run_spec(_spec(files={"../../etc/x": "no"}), tmp_path, query=FakeSDK())
    with pytest.raises(ValueError, match="unknown session spec"):
        session_runner.run_spec(_spec(version=99), tmp_path, query=FakeSDK())


def test_a_sandbox_runs_one_session(tmp_path):
    from fastapi.testclient import TestClient

    c = TestClient(session_runner.make_app(tmp_path, query=FakeSDK()))
    assert c.post("/run", json=_spec()).json()["ok"] is True
    assert c.post("/run", json=_spec()).status_code == 409
    bad = TestClient(session_runner.make_app(tmp_path / "x", query=FakeSDK())).post("/run", json=_spec(version=2))
    assert bad.json() == {"ok": False, "error": "ValueError", "transient": False}


# --------------------------------------------------------------------------- the claim

class FakeApi:
    def __init__(self, ready_after=1):
        self.created, self.deleted, self.polls, self.ready_after = [], [], 0, ready_after

    def create_namespaced_custom_object(self, group, version, ns, plural, body):
        self.created.append((ns, body))

    def get_namespaced_custom_object(self, group, version, ns, plural, name):
        self.polls += 1
        if self.polls < self.ready_after:
            return {"status": {}}
        return {"status": {"conditions": [{"type": "Ready", "status": "True"}],
                           "sandbox": {"name": "sbx-1", "podIPs": ["10.1.2.3"], "serviceFQDN": "sbx-1.n.svc"}}}

    def delete_namespaced_custom_object(self, group, version, ns, plural, name):
        self.deleted.append(name)


class FakeHttp:
    def __init__(self, status=200):
        self.status, self.posts = status, []

    def post(self, url, json, timeout):
        import httpx

        self.posts.append((url, json))
        return httpx.Response(self.status, json={"ok": True, "text": "done"}, request=httpx.Request("POST", url))


def test_each_session_claims_a_sandbox_and_the_claim_is_deleted():
    api, http = FakeApi(ready_after=2), FakeHttp()
    ex = session_runner.KubernetesExecutor(namespace="valxel-sessions", pool="agent-session", api=api, http=http,
                                           sleep=lambda s: None)
    assert ex.run(_spec()) == {"ok": True, "text": "done"}
    [(ns, body)] = api.created
    assert ns == "valxel-sessions" and body["spec"]["warmPoolRef"] == {"name": "agent-session"}
    assert body["spec"]["lifecycle"]["shutdownPolicy"] == "Delete" and body["spec"]["lifecycle"]["shutdownTime"]
    assert body["apiVersion"] == "extensions.agents.x-k8s.io/v1beta1"
    assert http.posts[0][0] == "http://10.1.2.3:8081/run"  # the pod's address, from the claim
    assert api.deleted == [body["metadata"]["name"]]


def test_no_sandbox_in_time_or_a_bad_answer_is_unavailable_and_still_cleans_up():
    t = iter(range(1000))
    api = FakeApi(ready_after=10**6)
    ex = session_runner.KubernetesExecutor(namespace="n", pool="t", api=api, http=FakeHttp(), ready_s=5,
                                           sleep=lambda s: None, clock=lambda: next(t))
    with pytest.raises(session_runner.Unavailable, match="ready in time"):
        ex.run(_spec())
    assert len(api.deleted) == 1
    api = FakeApi()
    ex = session_runner.KubernetesExecutor(namespace="n", pool="t", api=api, http=FakeHttp(status=502),
                                           sleep=lambda s: None)
    with pytest.raises(session_runner.Unavailable, match="502"):
        ex.run(_spec())
    assert len(api.deleted) == 1


# --------------------------------------------------------------------------- the tool's side

def test_with_sandboxes_a_coder_box_holds_a_token_not_a_clone(services, monkeypatch):
    monkeypatch.setenv("VALXEL_SESSION_EXECUTOR", "kubernetes")
    monkeypatch.setattr(services.settings, "mode", "live")
    monkeypatch.setattr(sandbox, "repo_token", lambda svc, repo: f"token-for-{repo}")
    monkeypatch.setattr(sandbox, "_checkout", lambda *a: pytest.fail("cloned in the tool's container"))
    with sandbox.session(services, repo="acme-farm/farm-api", branch="feature/F-1") as box:
        assert box.spec["checkout"] == {"repo": "acme-farm/farm-api", "branch": "feature/F-1",
                                        "token": "token-for-acme-farm/farm-api"}
        assert not any(Path(box.cwd).iterdir())


def test_the_runtime_sends_the_session_and_accounts_for_it_here(services, monkeypatch, tmp_path):
    pytest.importorskip("claude_agent_sdk", reason="the live extra isn't installed")
    from valxel_sdlc.runtime.claude_code import ClaudeCodeRuntime

    monkeypatch.setenv("VALXEL_SESSION_EXECUTOR", "kubernetes")
    sdk = FakeSDK()
    monkeypatch.setattr(session_runner, "_executor", session_runner.LocalExecutor(query=sdk))
    rt = ClaudeCodeRuntime(services.gateway, plugin_dirs=[])
    before = len(services.ledger.records())
    res = rt.run(_task(str(tmp_path), keep_transcript=True))
    assert res.text == '{"summary": "done"}' and res.session_id == "sess-1" and res.runtime == "claude_code"
    assert len(services.ledger.records()) == before + 1  # the ledger is written here, as before
    store = sessions.TranscriptStore(services.settings.artifact_dir)
    assert store.has("FARM-FEAT-1", "sess-1")  # kept for a later resume
    res = rt.run(_task(str(tmp_path / "2"), resume="sess-1", fresh_tail="From the handoff", session_mode="resumed"))
    assert res.resumed is True and sdk.calls[-1][1]["resume"] == "sess-1"


def test_strict_mode_wants_sessions_in_sandboxes_in_live_mode(services, monkeypatch):
    from valxel_sdlc import strict

    monkeypatch.setattr(services.settings, "mode", "live")
    found = strict.stand_ins(services, auth_mode="github", env={"VALXEL_DELIVERY": "live"})
    assert any(f.startswith("VALXEL_SESSION_EXECUTOR is not kubernetes") for f in found)
    found = strict.stand_ins(services, auth_mode="github", env={"VALXEL_DELIVERY": "live",
                                                                 "VALXEL_SESSION_EXECUTOR": "kubernetes"})
    assert not any(f.startswith("VALXEL_SESSION_EXECUTOR") for f in found)


def test_an_architects_checkouts_are_made_in_its_sandbox(services, monkeypatch):
    from valxel_sdlc import workspace

    class GH:
        class tokens:
            @staticmethod
            def scoped(org, names, perms):
                return f"{org}:{','.join(names)}:{perms['contents']}"

        def _call(self, method, repo, path):
            return {"size": 10, "default_branch": "main"} if path == "" else {"sha": f"sha-{repo}"}

    monkeypatch.setattr(services, "github", GH())
    co = workspace.remote_checkout(services, ["acme-farm/farm-api", "acme-farm/farm-web"])
    assert co["root"] == "" and co["repos"]["acme-farm/farm-api"] == {"sha": "sha-acme-farm/farm-api"}
    assert co["remote"][0]["token"] == "acme-farm:farm-api,farm-web:read"  # read-only, these repos only
    assert workspace.session_box(co) == {"read_checkouts": co["remote"], "read_only": True}
    assert workspace.session_box({"root": "/x", "repos": {}}) is None


def test_in_sandboxes_codex_runs_only_with_the_gateway(services, monkeypatch, tmp_path):
    from valxel_sdlc import gateway_sessions
    from valxel_sdlc.gateway import ProviderUnavailable
    from valxel_sdlc.models import second_opinion
    from valxel_sdlc.runtime.codex import CodexRuntime

    monkeypatch.setenv("VALXEL_SESSION_EXECUTOR", "kubernetes")
    monkeypatch.delenv("VALXEL_LLM_GATEWAY_URL", raising=False)
    with pytest.raises(ProviderUnavailable, match="provider key there"):  # a fallback chain moves on
        CodexRuntime(services.gateway).run(_task(str(tmp_path)))
    choice = second_opinion(services.settings, set(services.settings.roles), "reviewer", "general")
    assert choice.role is None and "provider key there" in choice.skipped
    monkeypatch.setenv("VALXEL_LLM_GATEWAY_URL", "http://gw")
    monkeypatch.setenv("VALXEL_LLM_GATEWAY_API_KEY", "k")
    assert gateway_sessions.codex_blocked() == ""


def test_a_sandbox_not_yet_reachable_is_retried_before_the_session_is_sent():
    import httpx

    class Flaky(FakeHttp):
        def __init__(self):
            super().__init__()
            self.fails = 2

        def post(self, url, json, timeout):
            if self.fails:
                self.fails -= 1
                raise httpx.ConnectError("name does not resolve yet")
            return super().post(url, json, timeout)

    http = Flaky()
    ex = session_runner.KubernetesExecutor(namespace="n", pool="t", api=FakeApi(), http=http, sleep=lambda s: None)
    assert ex.run(_spec())["ok"] and len(http.posts) == 1


def test_the_cluster_is_called_with_the_pods_own_service_account(tmp_path, monkeypatch):
    import httpx

    sa = tmp_path / "sa"
    sa.mkdir()
    (sa / "token").write_text("sa-token\n")
    seen = []

    def handler(req):
        seen.append((req.method, str(req.url), req.headers["authorization"]))
        return httpx.Response(200, json={"status": {}})

    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "10.0.0.1")
    monkeypatch.setattr(session_runner.InClusterApi, "SA", sa)
    api = session_runner.InClusterApi(http=httpx.Client(transport=httpx.MockTransport(handler)))
    api.create_namespaced_custom_object(session_runner.GROUP, session_runner.API_VERSION, "valxel-sessions",
                                        session_runner.PLURAL, {"kind": "SandboxClaim"})
    api.delete_namespaced_custom_object(session_runner.GROUP, session_runner.API_VERSION, "valxel-sessions",
                                        session_runner.PLURAL, "session-1")
    assert seen == [
        ("POST", "https://10.0.0.1/apis/extensions.agents.x-k8s.io/v1beta1/namespaces/valxel-sessions/sandboxclaims",
         "Bearer sa-token"),
        ("DELETE", "https://10.0.0.1/apis/extensions.agents.x-k8s.io/v1beta1/namespaces/valxel-sessions/"
                   "sandboxclaims/session-1", "Bearer sa-token")]


def test_the_runner_refuses_paths_and_names_that_leave_its_work_area(tmp_path):
    base = {"version": session_runner.VERSION, "runtime": "claude_code", "prompt": "p",
            "fallback": {"prompt": "p", "session_mode": "fresh"}}
    for bad in ({"files": {"../escape": "x"}}, {"files": {"/etc/passwd": "x"}},
                {"checkout": {"repo": "../../etc", "branch": "b", "token": "t"}},
                {"read_checkouts": [{"repo": "o/r", "sha": "not-a-sha", "token": "t"}]},
                {"read_checkouts": [{"repo": "o/..", "sha": "a" * 40, "token": "t"}]},
                {"resume": {"session_id": "../../x", "transcript": ""}}):
        with pytest.raises(ValueError):
            session_runner.run_spec({**base, **bad}, tmp_path, query=lambda p, o: {"session_id": "s"})


def test_a_failed_session_reports_its_kind_not_its_text(tmp_path):
    from fastapi.testclient import TestClient

    def boom(prompt, options):
        raise RuntimeError("overloaded_error: secret detail at /work/home")

    app = session_runner.make_app(tmp_path, query=boom)
    out = TestClient(app).post("/run", json={"version": session_runner.VERSION, "runtime": "claude_code",
                                            "prompt": "p", "fallback": {"prompt": "p", "session_mode": "fresh"}}).json()
    assert out == {"ok": False, "error": "RuntimeError", "transient": True}

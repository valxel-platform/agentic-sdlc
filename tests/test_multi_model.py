"""Roles can run on other vendors behind the same stage contract: an OpenAI-compatible provider
for text, Codex for code, fallbacks when a provider is unavailable, and a second model's review."""

import json
import shutil
import subprocess
from pathlib import Path

import httpx
import pytest
from langgraph.checkpoint.memory import InMemorySaver

from valxel_sdlc import models
from valxel_sdlc.config import RoleConfig, load_settings
from valxel_sdlc.engine import FeatureEngine
from valxel_sdlc.gateway import (
    CallTags,
    InMemoryLedger,
    ModelGateway,
    PolicyViolation,
    PromptLayers,
    ProviderUnavailable,
)
from valxel_sdlc.gateway.providers import ModelRequest, OpenAICompatibleProvider
from valxel_sdlc.graph.phase import merge_second_opinion
from valxel_sdlc.runtime import AgentResult
from valxel_sdlc.runtime.base import AgentTask
from valxel_sdlc.runtime.codex import (
    CodexRuntime,
    codex_command,
    codex_prompt,
    usage_from,
)
from valxel_sdlc.services import build_services

ROOT = Path(__file__).resolve().parents[1]


def settings(tmp_path, mode="stub"):
    root = tmp_path / "governance"
    if not root.exists():
        shutil.copytree(ROOT / "examples" / "governance", root)
    return load_settings(mode=mode, database_url="", artifact_dir=tmp_path / "a",
                         governance_source="local", governance_root=root)


def layers(tail="Implement T1"):
    return PromptLayers(tools="[]", skills="# Role: coder\n\nSKILL", constitution="VX-1",
                        product_context="product", feature_context="PRD", tail=tail)


def tags(role="coder", data_class="general"):
    return CallTags(product_id="FARM", feature_id="F-1", phase="build", role=role, data_class=data_class,
                    workspace="w")


# ---------------------------------------------------------------- OpenAI-compatible provider

def test_an_openai_compatible_provider_sends_the_layers_first_and_reads_cached_tokens():
    seen = {}

    def handler(request):
        seen["url"], seen["body"] = str(request.url), json.loads(request.content)
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json={"id": "c1", "choices": [{"message": {"content": "done"}}],
                                         "usage": {"prompt_tokens": 1000, "completion_tokens": 50,
                                                   "prompt_tokens_details": {"cached_tokens": 800}}})

    p = OpenAICompatibleProvider("selfhosted", "http://vllm:8000/v1/", "k",
                                 client=httpx.Client(transport=httpx.MockTransport(handler)))
    r = p.complete(ModelRequest(model="selfhosted/qwen3-coder", layers=layers(), workspace="w", max_tokens=99))
    assert seen["url"] == "http://vllm:8000/v1/chat/completions" and seen["auth"] == "Bearer k"
    assert seen["body"]["model"] == "qwen3-coder" and seen["body"]["max_completion_tokens"] == 99
    system, user = seen["body"]["messages"]
    assert system["content"].startswith("[]\n\n# Role: coder") and system["content"].endswith("PRD")
    assert user == {"role": "user", "content": "Implement T1"}
    assert (r.text, r.input_tokens, r.cache_read_input_tokens, r.output_tokens) == ("done", 200, 800, 50)


@pytest.mark.parametrize("status", [429, 503])
def test_a_busy_or_down_provider_is_unavailable_not_failed(status):
    p = OpenAICompatibleProvider("openai", "https://x/v1", client=httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(status, text="busy"))))
    with pytest.raises(ProviderUnavailable):
        p.complete(ModelRequest(model="openai/m", layers=layers(), workspace="w"))


def test_an_unreachable_provider_is_unavailable():
    def boom(request):
        raise httpx.ConnectError("refused")

    p = OpenAICompatibleProvider("selfhosted", "http://x/v1", client=httpx.Client(transport=httpx.MockTransport(boom)))
    with pytest.raises(ProviderUnavailable):
        p.complete(ModelRequest(model="selfhosted/m", layers=layers(), workspace="w"))


def test_a_bad_request_is_an_error_not_a_fallback():
    p = OpenAICompatibleProvider("openai", "https://x/v1", client=httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(400, text="bad"))))
    with pytest.raises(httpx.HTTPStatusError):
        p.complete(ModelRequest(model="openai/m", layers=layers(), workspace="w"))


# ---------------------------------------------------------------- Codex runtime

def test_the_codex_command_is_headless_sandboxed_and_reads_the_prompt_from_stdin():
    cmd = codex_command("openai/gpt-5.4", cwd="/work", schema_path="/t/s.json", last_path="/t/l.txt", provider=None)
    assert cmd[:3] == ["codex", "exec", "--json"] and cmd[-1] == "-"
    assert {"--ephemeral", "--skip-git-repo-check"} <= set(cmd)
    assert cmd[cmd.index("--sandbox") + 1] == "workspace-write" and 'model="gpt-5.4"' in cmd
    assert cmd[cmd.index("--output-schema") + 1] == "/t/s.json" and cmd[cmd.index("--cd") + 1] == "/work"
    own = codex_command("selfhosted/qwen3-coder", cwd=None, schema_path=None, last_path="/t/l",
                        provider=("selfhosted", "http://vllm:8000/v1", "VALXEL_SELFHOSTED_API_KEY"))
    assert 'model_provider="selfhosted"' in own and 'model_providers.selfhosted.base_url="http://vllm:8000/v1"' in own
    assert 'model_providers.selfhosted.env_key="VALXEL_SELFHOSTED_API_KEY"' in own and "--output-schema" not in own


def test_the_codex_prompt_puts_the_stable_layers_before_the_task():
    task = AgentTask(role="coder", phase="build", tags=tags(), layers=layers())
    prompt = codex_prompt(task)
    assert prompt.index("SKILL") < prompt.index("VX-1") < prompt.index("PRD") < prompt.index("Implement T1")
    assert "[]" not in prompt  # Codex brings its own tools


def test_usage_is_summed_over_the_completed_turns():
    events = "\n".join([json.dumps({"type": "thread.started"}),
                        json.dumps({"type": "turn.completed", "usage": {"input_tokens": 100, "cached_input_tokens": 60, "output_tokens": 10}}),
                        "not json",
                        json.dumps({"type": "turn.completed", "usage": {"input_tokens": 50, "cached_input_tokens": 0, "output_tokens": 5}})])
    assert usage_from(events) == {"input_tokens": 150, "cached_input_tokens": 60, "output_tokens": 15}


class FakeCodex:
    def __init__(self, returncode=0, reply='{"done": true}', stderr="", events=None):
        self.returncode, self.reply, self.stderr, self.calls = returncode, reply, stderr, []
        self.events = events if events is not None else json.dumps(
            {"type": "turn.completed", "usage": {"input_tokens": 1000, "cached_input_tokens": 400, "output_tokens": 100}})

    def __call__(self, cmd, **kw):
        self.calls.append((cmd, kw))
        Path(cmd[cmd.index("-o") + 1]).write_text(self.reply)
        return subprocess.CompletedProcess(cmd, self.returncode, stdout=self.events, stderr=self.stderr)


def codex_gateway(tmp_path, monkeypatch):
    s = settings(tmp_path)
    s.prices["gpt-5.4"] = (1.0, 10.0, 0.1)
    return ModelGateway(s, InMemoryLedger(), {})


def test_a_codex_session_replies_in_the_schema_and_is_costed(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.delenv("CODEX_API_KEY", raising=False)
    gw, fake = codex_gateway(tmp_path, monkeypatch), FakeCodex()
    res = CodexRuntime(gw, cwd="/work", runner=fake).run(
        AgentTask(role="coder_codex", phase="build", tags=tags("coder_codex"), layers=layers(), schema={"type": "object"}))
    _, kw = fake.calls[0]
    assert res.text == '{"done": true}' and "Implement T1" in kw["input"]
    assert kw["env"]["CODEX_API_KEY"] == "sk-test" and kw["timeout"] == 40 * 60
    assert Path(kw["env"]["CODEX_HOME"]).name == "home"  # a home of its own, gone after the session
    rec = gw.ledger.records()[-1]
    assert (rec.runtime, rec.provider, rec.input_tokens, rec.cache_read_input_tokens) == ("codex", "openai", 600, 400)
    assert res.cost_usd == pytest.approx((600 * 1.0 + 400 * 0.1 + 100 * 10.0) / 1e6)


def test_a_rate_limited_codex_session_is_unavailable(tmp_path, monkeypatch):
    gw = codex_gateway(tmp_path, monkeypatch)
    fake = FakeCodex(returncode=1, events=json.dumps({"type": "turn.failed", "error": {"message": "Rate limit reached"}}))
    with pytest.raises(ProviderUnavailable):
        CodexRuntime(gw, runner=fake).run(AgentTask(role="coder_codex", phase="build", tags=tags("coder_codex"), layers=layers()))
    with pytest.raises(RuntimeError, match="exited 2"):
        CodexRuntime(gw, runner=FakeCodex(returncode=2, events="", stderr="bad flag")).run(
            AgentTask(role="coder_codex", phase="build", tags=tags("coder_codex"), layers=layers()))


def test_phi_never_reaches_a_provider_without_an_agreement(tmp_path, monkeypatch):
    gw = codex_gateway(tmp_path, monkeypatch)
    with pytest.raises(PolicyViolation):
        CodexRuntime(gw, runner=FakeCodex()).run(
            AgentTask(role="coder_codex", phase="build", tags=tags("coder_codex", "phi"), layers=layers()))


# ---------------------------------------------------------------- which role does the work

def test_fallbacks_are_only_those_allowed_the_data_class(tmp_path):
    s = settings(tmp_path)
    every = set(s.roles)
    assert models.chain(s, every, "coder", "general") == ["coder", "coder_codex", "coder_open"]
    assert models.chain(s, every, "coder", "phi") == ["coder", "coder_open"]
    assert models.chain(s, every - {"coder_open"}, "coder", "phi") == ["coder"]


def test_the_second_opinion_says_why_it_was_skipped(tmp_path):
    s = settings(tmp_path)
    every = set(s.roles)
    assert models.second_opinion(s, every, "reviewer", "general").role == "reviewer_second"
    assert models.second_opinion(s, every, "reviewer", "phi").role == "reviewer_open"
    none = models.second_opinion(s, every - {"reviewer_open"}, "reviewer", "phi")
    assert none.role is None and none.skipped == ("reviewer_second: phi data may not reach openai; "
                                                 "reviewer_open: provider not configured")
    assert models.second_opinion(s, every, "coder", "general").role is None
    # When the self-hosted fallback did the review, it can't also be its own second opinion.
    assert "same model as the review" in models.second_opinion(s, every, "reviewer", "phi", reviewed_by="reviewer_open").skipped


def test_live_mode_turns_optional_roles_off_and_refuses_missing_required_ones(tmp_path, monkeypatch):
    for var in ("OPENAI_API_KEY", "CODEX_API_KEY", "VALXEL_SELFHOSTED_URL", "VALXEL_SELFHOSTED_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    s = settings(tmp_path, mode="live")
    on = models.available_roles(s)
    assert "coder" in on and not {"coder_codex", "coder_open", "reviewer_second", "reviewer_open"} & on
    monkeypatch.setenv("OPENAI_API_KEY", "sk")
    assert "reviewer_second" not in models.available_roles(s)  # no price for gpt-5.4 yet
    s.prices["gpt-5.4"] = (1.0, 10.0, 0.1)
    assert {"reviewer_second", "coder_codex"} <= models.available_roles(s)
    s.roles["spec"] = RoleConfig(runtime="generic", model="openai/gpt-unpriced")
    with pytest.raises(RuntimeError, match="spec: no price"):
        models.available_roles(s)


def test_configuration_mistakes_fail_at_load(tmp_path):
    s = settings(tmp_path)
    s.roles["coder"] = RoleConfig(runtime="claude_code", model="anthropic/claude-sonnet-5-5", fallback=("nobody",))
    with pytest.raises(ValueError, match="'nobody' is not a role"):
        models.validate(s)
    s.roles["coder"] = RoleConfig(runtime="aider", model="anthropic/claude-sonnet-5-5")
    with pytest.raises(ValueError, match="runtime 'aider'"):
        models.validate(s)
    s.roles["coder"] = RoleConfig(runtime="generic", model="mistral/large")
    with pytest.raises(ValueError, match="provider 'mistral'"):
        models.validate(s)


def test_a_task_moves_to_the_fallback_when_the_provider_is_unavailable(tmp_path, monkeypatch):
    svc = build_services(settings(tmp_path))
    real, ran = svc.runtime_for, []

    def flaky(role):
        rt = real(role)

        class Wrapped:
            name, capabilities = rt.name, rt.capabilities

            def run(self, task):
                ran.append(task.role)
                if task.role == "coder":
                    raise ProviderUnavailable("anthropic: overloaded")
                return rt.run(task)
        return Wrapped()

    monkeypatch.setattr(svc, "runtime_for", flaky)
    eng = FeatureEngine(svc, InMemorySaver())
    fid = eng.create(product_id="FARM", title="Offline crop log")
    v = eng.auto_drive(fid)
    assert v["status"] == "done"
    assert "coder_codex" in ran and "coder_open" not in ran  # the first fallback took it
    roles = {r.role for r in svc.ledger.records()}
    assert "coder_codex" in roles and "coder" not in roles


def test_every_fallback_down_fails_the_task(tmp_path, monkeypatch):
    svc = build_services(settings(tmp_path))
    real = svc.runtime_for

    def down(role):
        rt = real(role)

        class Wrapped:
            name, capabilities = rt.name, rt.capabilities

            def run(self, task):
                if task.role.startswith("coder"):
                    raise ProviderUnavailable("down")
                return rt.run(task)
        return Wrapped()

    monkeypatch.setattr(svc, "runtime_for", down)
    from valxel_sdlc.graph.phase import call_agent
    with pytest.raises(ProviderUnavailable):
        call_agent(svc, role="coder", phase="build", ctx={"product_id": "FARM", "feature_id": "F-1"},
                   skills=(), tail="t", stub_text="x")


# ---------------------------------------------------------------- the second opinion

def reply(approve, blocking=(), score=0.9):
    return AgentResult(text=json.dumps({"approve": approve, "blocking": list(blocking), "notes": [], "score": score}),
                       tokens=10, cost_usd=0.01)


def test_an_advisory_second_opinion_is_reported_without_blocking():
    review = {"approve": True, "blocking": [], "score": 0.9}
    out = merge_second_opinion(review, reply(False, ["SQL built from input"], 0.4), "reviewer_second", "openai/gpt-5.4",
                               blocking=False)
    assert out["approve"] is True and out["blocking"] == []
    assert out["second_opinion"]["blocking"] == ["SQL built from input"] and out["second_opinion"]["agree"] is False


def test_a_blocking_second_opinion_blocks_the_merge():
    review = {"approve": True, "blocking": [], "score": 0.9}
    out = merge_second_opinion(review, reply(False, ["SQL built from input"], 0.4), "reviewer_second", "openai/gpt-5.4",
                               blocking=True)
    assert out["approve"] is False and out["blocking"] == ["[openai/gpt-5.4] SQL built from input"] and out["score"] == 0.4


def test_every_build_review_gets_a_second_opinion_where_the_data_class_allows(tmp_path):
    svc = build_services(settings(tmp_path))
    eng = FeatureEngine(svc, InMemorySaver())
    farm = eng.create(product_id="FARM", title="Offline crop log")
    clinic = eng.create(product_id="CLN", title="Visit summary")
    for fid in (farm, clinic):
        assert eng.auto_drive(fid)["status"] == "done"
    build = lambda fid: svc.artifacts.load(eng._state(fid)["artifacts"]["BUILD"])
    assert "second opinion (openai/gpt-5.4) approves (advisory)" in build(farm)
    assert "second opinion (selfhosted/qwen3-coder) approves (advisory)" in build(clinic)
    assert "reviewer_second" in {r.role for r in svc.ledger.records() if r.feature_id == farm}


def test_a_session_stuck_reconnecting_is_stopped_early(tmp_path):
    import sys
    import time

    from valxel_sdlc.runtime.codex import failure_from, run_codex
    script = tmp_path / "fake_codex.py"
    script.write_text("import json, sys, time\nsys.stdin.read()\n"
                      "for _ in range(50):\n"
                      "    print(json.dumps({'type': 'error', 'message': 'Reconnecting... waiting for network'}), flush=True)\n"
                      "    time.sleep(0.05)\n")
    started = time.monotonic()
    proc = run_codex([sys.executable, str(script)], input="prompt", env={}, timeout=30)
    assert time.monotonic() - started < 10 and proc.returncode == -9
    assert "unreachable" in failure_from(proc.stdout)


def test_each_codex_session_starts_from_a_copy_of_the_plugin_home(tmp_path, monkeypatch):
    template = tmp_path / "codex-home"
    (template / "plugins" / "cache").mkdir(parents=True)
    (template / "config.toml").write_text('[plugins."engineering@valxel-plugins"]\nenabled = true\n')
    (template / "tmp").mkdir()  # the install's scratch state stays behind
    seen = []

    class Peek(FakeCodex):
        def __call__(self, cmd, **kw):
            home = Path(kw["env"]["CODEX_HOME"])
            seen.append((home, (home / "config.toml").read_text()))
            assert sorted(p.name for p in home.iterdir()) == ["config.toml", "plugins"]
            (home / "state.db").write_text("session state")
            return super().__call__(cmd, **kw)

    gw = codex_gateway(tmp_path, monkeypatch)
    rt = CodexRuntime(gw, runner=Peek(), home_template=str(template))
    for _ in range(2):
        rt.run(AgentTask(role="coder_codex", phase="build", tags=tags("coder_codex"), layers=layers()))
    assert seen[0][0] != seen[1][0] and all("engineering@valxel-plugins" in c for _, c in seen)
    assert not (template / "state.db").exists() and not seen[0][0].exists()


def test_the_image_plugins_are_skills_only_and_form_a_marketplace(tmp_path):
    upstream = tmp_path / "kwp"
    for p in ("product-management", "engineering", "design"):
        (upstream / p / "skills" / "s").mkdir(parents=True)
        (upstream / p / "skills" / "s" / "SKILL.md").write_text("---\nname: s\n---\n")
        (upstream / p / ".mcp.json").write_text('{"mcpServers": {"slack": {}}}')
    (upstream / "engineering" / "hooks").mkdir()
    (upstream / "LICENSE").write_text("Apache")
    out = tmp_path / "out"
    subprocess.run([str(ROOT / "plugins" / "prepare.sh"), str(upstream), str(ROOT / "plugins" / "valxel"), str(out)],
                   check=True)
    assert not list(out.rglob(".mcp.json")) and not (out / "engineering" / "hooks").exists()
    market = json.loads((out / ".claude-plugin" / "marketplace.json").read_text())
    assert {p["name"] for p in market["plugins"]} == {"product-management", "engineering", "design", "valxel"}
    assert all((out / p["source"]).is_dir() for p in market["plugins"])

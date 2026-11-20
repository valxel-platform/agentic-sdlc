"""Codex through the LLM gateway, with a live dollar cap, here or in a session sandbox
(tracker T-47, T-09; runtime/codex.py)."""

import json
from contextlib import contextmanager

import pytest

from valxel_sdlc import gateway_sessions, session_runner
from valxel_sdlc.runtime.base import AgentTask
from valxel_sdlc.runtime.codex import GATEWAY_TOKEN_ENV, CodexRuntime

from test_multi_model import FakeCodex, codex_gateway, layers, tags


@pytest.fixture
def minted(monkeypatch):
    seen = {}

    @contextmanager
    def fake_mint(t, models, budget, ttl, **_):
        seen.update(models=models, budget=budget, ttl=ttl, data_class=t.data_class)
        yield "http://gw:8080", "vgs_token"
        seen["revoked"] = True

    monkeypatch.setenv("VALXEL_LLM_GATEWAY_URL", "http://gw:8080")
    monkeypatch.setenv("VALXEL_LLM_GATEWAY_API_KEY", "tool-key")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-the-tools-own")
    monkeypatch.setattr(gateway_sessions, "mint", fake_mint)
    return seen


def _task(**kw):
    return AgentTask(role="coder_codex", phase="build", tags=tags("coder_codex"), layers=layers(), **kw)


def test_codex_calls_the_gateway_with_a_capped_session_token_not_a_provider_key(tmp_path, minted):
    fake = FakeCodex()
    res = CodexRuntime(codex_gateway(tmp_path, None), cwd="/work", runner=fake).run(_task())
    cmd, kw = fake.calls[0]
    flags = " ".join(cmd)
    assert 'model_provider="valxel_gateway"' in flags and 'base_url="http://gw:8080/v1"' in flags
    assert 'wire_api="responses"' in flags and f'env_key="{GATEWAY_TOKEN_ENV}"' in flags
    assert kw["env"][GATEWAY_TOKEN_ENV] == "vgs_token" and "sk-the-tools-own" not in kw["env"].values()
    assert minted["models"] == ["gpt-5.4-2026-03-05"] and minted["budget"] == 15 and minted["revoked"]
    assert res.text == '{"done": true}'


def test_a_session_the_gateway_stopped_at_its_cap_fails_without_a_fallback(tmp_path, minted):
    stopped = json.dumps({"type": "turn.failed", "error": {"message": "rate_limit_error: this session has used its budget"}})
    with pytest.raises(RuntimeError, match=r"reached its \$15.00 cap") as err:
        CodexRuntime(codex_gateway(tmp_path, None), runner=FakeCodex(returncode=1, events=stopped)).run(_task())
    assert type(err.value) is RuntimeError  # not ProviderUnavailable: the next role gets no fresh budget
    assert minted["revoked"]


def test_in_a_sandbox_the_session_goes_with_its_token_and_checkout_only(tmp_path, minted, monkeypatch):
    sent = {}

    class Executor:
        def run(self, spec):
            sent.update(spec)
            return {"ok": True, **session_runner.run_spec(spec, tmp_path / "sandbox", query=FakeCodex())}

    monkeypatch.setenv("VALXEL_SESSION_EXECUTOR", "kubernetes")
    monkeypatch.setattr(session_runner, "_executor", Executor())
    box = {"checkout": None, "read_checkouts": []}
    res = CodexRuntime(codex_gateway(tmp_path, None), runner=FakeCodex()).run(_task(sandbox=box))
    assert sent["runtime"] == "codex" and sent["keys"] == {GATEWAY_TOKEN_ENV: "vgs_token"}
    assert "sk-the-tools-own" not in json.dumps(sent)
    assert res.text == '{"done": true}' and res.runtime == "codex"


def test_with_the_gateway_codex_roles_need_no_provider_key(tmp_path, monkeypatch):
    from valxel_sdlc.models import provider_ready

    s = codex_gateway(tmp_path, None).settings
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("CODEX_API_KEY", raising=False)
    monkeypatch.delenv("VALXEL_LLM_GATEWAY_URL", raising=False)
    assert not provider_ready(s, "openai", "codex")
    monkeypatch.setenv("VALXEL_LLM_GATEWAY_URL", "http://gw")
    monkeypatch.setenv("VALXEL_LLM_GATEWAY_API_KEY", "k")
    assert not provider_ready(s, "openai", "codex")  # until the gateway serves and prices Codex's models
    monkeypatch.setenv("VALXEL_LLM_GATEWAY_CODEX", "on")
    assert provider_ready(s, "openai", "codex") and provider_ready(s, "selfhosted", "codex")

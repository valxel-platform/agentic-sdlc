import httpx
import pytest

from valxel_sdlc import gateway_sessions
from valxel_sdlc.gateway import CallTags

TAGS = CallTags(product_id="emr", feature_id="F-1", phase="build", role="coder", data_class="phi", workspace="ws-1")
ENV = {"VALXEL_LLM_GATEWAY_URL": "http://gw/", "VALXEL_LLM_GATEWAY_API_KEY": "tool-key"}


class Gateway:
    def __init__(self, mint_status=201):
        self.calls, self.mint_status = [], mint_status

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append((request.method, str(request.url), request.headers.get("x-api-key"),
                           request.content and httpx.Response(200, content=request.content).json()))
        if request.method == "POST":
            if self.mint_status != 201:
                return httpx.Response(self.mint_status, json={"type": "error", "error": {"message": "not a phi gateway"}})
            return httpx.Response(201, json={"id": "s-1", "token": "vgs_abc", "expires_at": 1})
        return httpx.Response(200, json={"id": "s-1", "spent_usd": 0.42, "calls": 3, "revoked": True})


def test_a_session_gets_a_token_not_the_tools_key_and_it_is_revoked_after():
    gw = Gateway()
    http = httpx.Client(transport=httpx.MockTransport(gw))
    with gateway_sessions.session_token(TAGS, "claude-sonnet-5-5", 2.5, 4200, http=http, env=ENV) as env:
        assert env["ANTHROPIC_BASE_URL"] == "http://gw" and env["ANTHROPIC_API_KEY"] == "vgs_abc"
        assert env["ANTHROPIC_AUTH_TOKEN"] == "" and env["CLAUDE_CODE_USE_BEDROCK"] == ""
        assert [c[0] for c in gw.calls] == ["POST"]
    method, url, key, body = gw.calls[0]
    assert url == "http://gw/v1/sessions" and key == "tool-key"
    assert body == {"budget_usd": 2.5, "ttl_s": 4200, "data_class": "phi",
                    "models": ["claude-sonnet-5-5", "claude-haiku-*"],
                    "labels": {"product": "emr", "feature": "F-1", "phase": "build", "agent": "coder", "workspace": "ws-1"}}
    assert gw.calls[1][:2] == ("DELETE", "http://gw/v1/sessions/s-1")


def test_the_token_is_revoked_when_the_session_fails_too():
    gw = Gateway()
    http = httpx.Client(transport=httpx.MockTransport(gw))
    with pytest.raises(RuntimeError), gateway_sessions.session_token(TAGS, "m", 1, 60, http=http, env=ENV):
        raise RuntimeError("the session broke")
    assert gw.calls[-1][0] == "DELETE"


def test_no_token_no_session():
    http = httpx.Client(transport=httpx.MockTransport(Gateway(mint_status=403)))
    with pytest.raises(gateway_sessions.GatewayUnavailable, match="not a phi gateway"):
        with gateway_sessions.session_token(TAGS, "m", 1, 60, http=http, env=ENV):
            pass


def test_configured_needs_both_the_url_and_the_key():
    assert gateway_sessions.configured(ENV)
    assert not gateway_sessions.configured({"VALXEL_LLM_GATEWAY_URL": "http://gw"})
    assert not gateway_sessions.configured({"VALXEL_LLM_GATEWAY_API_KEY": "k"})


def test_the_runtime_gives_the_session_the_token(services, monkeypatch, tmp_path):
    pytest.importorskip("claude_agent_sdk", reason="the live extra isn't installed")
    from contextlib import contextmanager

    from valxel_sdlc.runtime import claude_code

    seen = {}

    @contextmanager
    def fake_token(tags, model, budget, ttl, **_):
        seen.update(model=model, budget=budget, ttl=ttl)
        yield {"ANTHROPIC_BASE_URL": "http://gw", "ANTHROPIC_API_KEY": "vgs_abc"}
        seen["revoked"] = True

    async def fake_session(self, task, model, kwargs, home, tmp_home, store):
        seen["env"] = kwargs["env"]
        return "result"

    monkeypatch.setattr(gateway_sessions, "configured", lambda env=None: True)
    monkeypatch.setattr(gateway_sessions, "session_token", fake_token)
    monkeypatch.setattr(claude_code.ClaudeCodeRuntime, "_session", fake_session)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-the-tools-own")
    rt, task = _runtime_and_task(services, tmp_path)
    import asyncio

    assert asyncio.run(rt._run(task, "claude-sonnet-5-5")) == "result"
    assert seen["env"]["ANTHROPIC_API_KEY"] == "vgs_abc" and seen["revoked"]
    assert "sk-the-tools-own" not in seen["env"].values()


def _runtime_and_task(services, tmp_path):
    from valxel_sdlc.runtime.base import AgentTask
    from valxel_sdlc.runtime.claude_code import ClaudeCodeRuntime
    from valxel_sdlc.gateway import PromptLayers

    layers = PromptLayers(tools="", skills="S", constitution="C", product_context="P", feature_context="F", tail="T")
    task = AgentTask(role="coder", phase="build", tags=TAGS, layers=layers, sandbox={"home": str(tmp_path)})
    return ClaudeCodeRuntime(services.gateway, plugin_dirs=[]), task


def test_strict_mode_wants_gateway_tokens_in_live_mode(services, monkeypatch):
    from valxel_sdlc import strict

    monkeypatch.setattr(services.settings, "mode", "live")
    found = strict.stand_ins(services, auth_mode="github", env={"VALXEL_DELIVERY": "live"})
    assert any(f.startswith("VALXEL_LLM_GATEWAY_URL is not set") for f in found)
    found = strict.stand_ins(services, auth_mode="github", env={"VALXEL_DELIVERY": "live",
                                                                 "VALXEL_LLM_GATEWAY_URL": "http://gw"})
    assert not any(f.startswith("VALXEL_LLM_GATEWAY_URL") for f in found)


class FakeMessages:
    def __init__(self):
        self.kwargs = None

    def create(self, **kwargs):
        from types import SimpleNamespace

        self.kwargs = kwargs
        usage = SimpleNamespace(input_tokens=10, output_tokens=5, cache_creation_input_tokens=0,
                                cache_read_input_tokens=0, server_tool_use=None)
        return SimpleNamespace(content=[SimpleNamespace(text="ok")], usage=usage, id="msg_1")


def _request(**kw):
    from valxel_sdlc.gateway import PromptLayers
    from valxel_sdlc.gateway.providers import ModelRequest

    layers = PromptLayers(tools="", skills="S", constitution="C", product_context="P", feature_context="F", tail="T")
    return ModelRequest(model="anthropic/claude-sonnet-5-5", layers=layers, workspace="ws-1", **kw)


def test_through_the_gateway_a_call_carries_its_labels_and_declares_its_data_class():
    from types import SimpleNamespace

    from valxel_sdlc.gateway.providers import AnthropicProvider

    msgs = FakeMessages()
    p = AnthropicProvider(client=SimpleNamespace(messages=msgs), routed=True)
    p.complete(_request(labels={"feature": "F-1", "agent": "spec"}, data_class="phi"))
    assert msgs.kwargs["extra_headers"] == {"x-valxel-feature": "F-1", "x-valxel-agent": "spec",
                                            "x-valxel-data-class": "phi"}
    direct = FakeMessages()
    AnthropicProvider(client=SimpleNamespace(messages=direct), routed=False).complete(_request(data_class="phi"))
    assert "extra_headers" not in direct.kwargs


def test_the_client_points_at_the_gateway_with_the_tools_gateway_key(monkeypatch):
    pytest.importorskip("anthropic")
    from valxel_sdlc.gateway.providers import AnthropicProvider

    monkeypatch.setenv("VALXEL_LLM_GATEWAY_URL", "http://gw:8080/")
    monkeypatch.setenv("VALXEL_LLM_GATEWAY_API_KEY", "tool-key")
    p = AnthropicProvider()
    assert p.routed and str(p._client.base_url).rstrip("/") == "http://gw:8080" and p._client.api_key == "tool-key"


def test_the_gateway_tags_every_call_the_tool_makes(services, monkeypatch):
    from valxel_sdlc.gateway import PromptLayers

    seen = []
    stub = services.gateway.providers["stub"]

    class Recorder:
        name = "anthropic"

        def complete(self, req):
            seen.append(req)
            return stub.complete(req)

    monkeypatch.setitem(services.gateway.providers, "anthropic", Recorder())
    monkeypatch.setattr(services.settings, "mode", "live")
    layers = PromptLayers(tools="", skills="S", constitution="C", product_context="P", feature_context="F", tail="T")
    services.gateway.complete(TAGS, layers, stub_text="ok")
    assert seen[0].data_class == "phi"
    assert seen[0].labels == {"product": "emr", "feature": "F-1", "phase": "build", "agent": "coder", "workspace": "ws-1"}


def test_strict_mode_flags_a_provider_key_left_with_the_tool(services):
    from valxel_sdlc import strict

    env = {"VALXEL_DELIVERY": "live", "VALXEL_LLM_GATEWAY_URL": "http://gw", "ANTHROPIC_API_KEY": "sk"}
    assert any("belongs to the gateway alone" in f for f in strict.stand_ins(services, auth_mode="github", env=env))

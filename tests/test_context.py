"""Context measurement: every agent call records its session, the model's window, how full the
context got and the size of each prompt layer; the report groups them by week and role."""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import InMemorySaver

from valxel_sdlc import context
from valxel_sdlc.engine import FeatureEngine
from valxel_sdlc.gateway import CallTags, PromptLayers
from valxel_sdlc.gateway.ledger import InMemoryLedger, UsageRecord
from valxel_sdlc.runtime import claude_code, codex
from valxel_sdlc.runtime.base import AgentTask

NOW = datetime.now(UTC)
LAYERS = PromptLayers(tools="t" * 400, skills="s" * 4000, constitution="c" * 800, product_context="p" * 2000,
                      feature_context="f" * 8000, tail="do the task" * 10)


def tags(role="spec"):
    return CallTags(product_id="FARM", feature_id="FARM-FEAT-1", phase="spec", role=role, data_class="general",
                    workspace="w")


def rec(role="coder", *, ts=None, session="s1", mode="fresh", window=1_000_000, ctx=200_000, read=0, cost=1.0,
        layers=None, product="FARM"):
    return UsageRecord(ts=(ts or NOW).isoformat(), product_id=product, feature_id="F", phase="build", role=role,
                       runtime="claude_code", provider="anthropic", model="anthropic/claude-sonnet-5-5", workspace="w",
                       input_tokens=1000, cache_creation_input_tokens=0, cache_read_input_tokens=read,
                       output_tokens=100, cache_ttl="1h", prompt_version="v1", est_cost_usd=cost,
                       session_id=session, session_mode=mode, context_window=window, context_tokens=ctx,
                       layer_tokens=layers or {"L1": 100, "L2": 1000, "L3": 200, "L4": 500, "L5": 2000, "L6": 30},
                       request_id=f"r-{session}-{ctx}")


def test_a_single_gateway_call_records_its_window_fill_and_layers(services):
    res = services.gateway.complete(tags(), LAYERS, stub_text="ok")
    r = res.record
    assert r.session_mode == "single" and r.session_id == ""
    assert r.context_window == 1_000_000  # claude-sonnet-5-5, from models.yaml
    assert r.context_tokens == r.total_input and r.context_fill == round(r.total_input / 1_000_000, 4)
    assert r.layer_tokens == {"L1": 100, "L2": 1000, "L3": 200, "L4": 500, "L5": 2000, "L6": 27}
    assert services.settings.context_window("claude-opus-5-5") == 1_000_000
    assert services.settings.context_window("openai/gpt-5.4") == 0


def test_a_claude_code_session_records_its_id_and_its_largest_prompt(services, monkeypatch):
    from claude_agent_sdk import types

    def turn(inp, read):
        return types.AssistantMessage(content=[], model="claude-sonnet-5-5", session_id="sess-42",
                                      usage={"input_tokens": inp, "cache_creation_input_tokens": 0,
                                             "cache_read_input_tokens": read, "output_tokens": 10})

    result = types.ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, is_error=False, num_turns=3,
                                 session_id="sess-42", total_cost_usd=0.5, result="done",
                                 usage={"input_tokens": 9000, "cache_read_input_tokens": 300_000,
                                        "cache_creation_input_tokens": 0, "output_tokens": 30},
                                 model_usage={"claude-sonnet-5-5": {"contextWindow": 1_000_000}})

    async def fake_query(prompt, options):
        for m in (turn(3000, 50_000), turn(4000, 180_000), turn(2000, 70_000), result):
            yield m

    monkeypatch.setattr("claude_agent_sdk.query", fake_query)
    rt = claude_code.ClaudeCodeRuntime(services.gateway)
    task = AgentTask(role="coder", phase="build", tags=tags("coder"), layers=LAYERS)
    out = asyncio.run(rt._query(task, "anthropic/claude-sonnet-5-5", options=None))
    assert out.text == "done"
    [r] = [x for x in services.ledger.records() if x.runtime == "claude_code"]
    assert (r.session_id, r.session_mode, r.context_tokens, r.context_window) == ("sess-42", "fresh", 184_000, 1_000_000)
    assert r.context_fill == 0.184


def test_codex_reports_its_thread_and_largest_turn():
    events = "\n".join(json.dumps(e) for e in (
        {"type": "thread.started", "thread_id": "th-7"},
        {"type": "turn.completed", "usage": {"input_tokens": 50_000, "cached_input_tokens": 40_000}},
        {"type": "turn.completed", "usage": {"input_tokens": 120_000, "cached_input_tokens": 90_000}}))
    assert codex.session_from(events) == ("th-7", 120_000)
    assert codex.session_from("not json") == ("", 0)
    assert claude_code.prompt_tokens(None) == 0
    assert claude_code.reported_window(SimpleNamespace(model_usage={"claude-opus-5-5[1m]": {"contextWindow": 1_000_000}}),
                                       "anthropic/claude-opus-5-5") == 1_000_000


def test_the_report_groups_by_week_and_role_and_counts_sessions_by_mode():
    rows = context.report([
        rec(session="a", ctx=100_000), rec(session="a", ctx=300_000), rec(session="b", ctx=800_000, mode="resumed"),
        rec(role="spec", session="", mode="single", ctx=20_000, read=500),
        rec(role="coder", session="c", window=0, ctx=50_000),  # a model with no window: fill unknown
        rec(session="old", ts=NOW - timedelta(weeks=8)),  # outside the period
    ], weeks=4)
    coder = next(r for r in rows if r["role"] == "coder")
    assert coder["calls"] == 4 and coder["sessions"] == {"fresh": 2, "resumed": 1}
    assert coder["fill_max"] == 0.8 and coder["over_resume_fill"] == 1 and coder["context_tokens_max"] == 800_000
    assert coder["fill_avg"] == round((0.1 + 0.3 + 0.8) / 3, 4)  # the unknown one is left out
    spec = next(r for r in rows if r["role"] == "spec")
    assert spec["sessions"] == {"single": 1} and spec["cache_hit_rate"] == round(500 / 1500, 4)
    assert "old" not in json.dumps(rows)
    text = context.table(rows)
    assert "coder" in text and "L5 2,000" in text and "80%" in text
    assert context.table([]) == "No agent calls in this period."


def test_the_ledger_filters_by_product_and_time():
    led = InMemoryLedger()
    for r in (rec(product="FARM"), rec(product="CLN"), rec(product="FARM", ts=NOW - timedelta(days=60))):
        led.record(r)
    since = (NOW - timedelta(days=7)).isoformat()
    assert len(led.records(product_id="FARM", since=since)) == 1 and len(led.records(product_id="FARM")) == 2


def test_strict_mode_warns_about_a_model_with_no_window(services):
    from dataclasses import replace

    from valxel_sdlc.config import RoleConfig
    from valxel_sdlc.strict import known_gaps

    assert not [g for g in known_gaps(services) if "context window" in g]  # fallbacks that can't run don't count
    services.settings = replace(services.settings, roles={**services.settings.roles,
                                                          "spec": RoleConfig(runtime="generic", model="openai/gpt-5.4")})
    gaps = " ".join(known_gaps(services))
    assert "no context window for openai/gpt-5.4" in gaps


def test_the_context_report_is_on_the_api_and_the_cli(services, capsys, monkeypatch):
    from valxel_sdlc.api.app import create_app
    from valxel_sdlc.cli import _usage_context
    from valxel_sdlc.web.auth import AuthConfig

    services.ledger.record(rec(session="z", ctx=500_000))
    eng = FeatureEngine(services, InMemorySaver())
    client = TestClient(create_app(engine=eng, auth=AuthConfig(mode="dev", secret="x" * 32), web_dist=None))
    client.post("/api/auth/dev-login", json={"login": "dev-1", "teams": ["acme-farm/product-owners"], "orgs": ["acme-farm"]})
    rows = client.get("/api/usage/context", params={"product_id": "FARM", "weeks": 2}).json()
    assert rows[0]["role"] == "coder" and rows[0]["fill_max"] == 0.5
    assert _usage_context(SimpleNamespace(product="FARM", weeks=2, json=False), engine=eng) == 0
    assert "coder" in capsys.readouterr().out
    with pytest.raises(ValueError):
        context.week_of("not a date")

"""Traces of agents' work: one span per session, their runtimes nested under it, content by data class."""

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from vrixel_sdlc import telemetry
from vrixel_sdlc.gateway import CallTags, PromptLayers
from vrixel_sdlc.runtime import AgentTask
from vrixel_sdlc.runtime.claude_code import session_options
from vrixel_sdlc.runtime.codex import add_otel
from vrixel_sdlc.telemetry import Telemetry, TelemetryConfig, TracedRuntime


def traced(cfg=None):
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    cfg = cfg or TelemetryConfig(endpoint="http://collector:4318", trace_url="https://lf.example/traces/{trace_id}",
                                 content={"general": "tool_details", "phi": "full"})
    return Telemetry(cfg, provider=provider, config_version="cfg1"), exporter


def task(data_class="general", phase="build", role="coder"):
    tags = CallTags(product_id="FARM", feature_id="FARM-FEAT-1", phase=phase, role=role, data_class=data_class,
                    workspace="w")
    return AgentTask(role=role, phase=phase, tags=tags, layers=PromptLayers(tools="t", skills="", constitution="", product_context="", feature_context="", tail="do it"))


def test_content_follows_the_data_class_and_a_baa():
    cfg = TelemetryConfig(content={"general": "full", "phi": "full", "payments": "tool_details"})
    assert (cfg.level("general"), cfg.level("phi"), cfg.level("payments"), cfg.level("other")) == ("full", "none", "none", "none")
    cfg.content_store_baa = True
    assert cfg.level("phi") == "full" and cfg.level("payments") == "tool_details"
    with pytest.raises(ValueError, match="content levels"):
        import tempfile
        from pathlib import Path

        d = Path(tempfile.mkdtemp())
        (d / "telemetry.yaml").write_text("content: {general: everything}\n")
        telemetry.load_config(d, env={})


def test_the_shipped_config_keeps_content_off_until_a_store_is_approved(settings):
    cfg = telemetry.load_config(settings.config_dir, env={"VRIXEL_OTEL_ENDPOINT": "http://c:4318/"})
    assert cfg.endpoint == "http://c:4318" and {cfg.level(c) for c in ("general", "phi", "payments")} == {"none"}
    assert not Telemetry(telemetry.load_config(settings.config_dir, env={})).enabled


def test_a_claude_code_session_exports_under_the_tools_span():
    tel, exporter = traced()
    t = task()
    with telemetry.labels(task="TASK-2", attempt=1), tel.agent_span(t, "claude_code", model="anthropic/claude-opus-5-5") as span:
        env = tel.subprocess_env(t, "claude_code")
        trace_id, span_id = telemetry.current_ids()
    assert env["TRACEPARENT"] == f"00-{trace_id}-{span_id}-01"
    assert env["CLAUDE_CODE_ENABLE_TELEMETRY"] == "1" and env["OTEL_TRACES_EXPORTER"] == "otlp"
    assert env["OTEL_EXPORTER_OTLP_ENDPOINT"] == "http://collector:4318"
    assert (env["OTEL_LOG_TOOL_DETAILS"], env["OTEL_LOG_USER_PROMPTS"], env["OTEL_LOG_TOOL_CONTENT"]) == ("1", "0", "0")
    assert "vrixel.feature_id=FARM-FEAT-1" in env["OTEL_RESOURCE_ATTRIBUTES"] and "vrixel.task=TASK-2" in env["OTEL_RESOURCE_ATTRIBUTES"]
    [s] = exporter.get_finished_spans()
    assert s.name == "vrixel.agent build/coder"
    assert s.attributes["vrixel.task"] == "TASK-2" and s.attributes["vrixel.attempt"] == "1"
    assert s.attributes["gen_ai.request.model"] == "claude-opus-5-5" and s.attributes["vrixel.config_version"] == "cfg1"
    t.telemetry = {"env": env}
    opts = session_options(t, type("R", (), {"max_turns": 5, "max_call_usd": 1.0})(), "anthropic/claude-opus-5-5",
                           cwd=None, plugin_dirs=[])
    assert opts["env"]["TRACEPARENT"] == env["TRACEPARENT"] and opts["env"]["CLAUDE_CODE_PROMPT_CACHE_TTL"] == "1h"


def test_phi_sessions_carry_structure_only_without_a_baa(tmp_path):
    tel, _ = traced()
    t = task("phi")
    with tel.agent_span(t, "claude_code"):
        env = tel.subprocess_env(t, "claude_code")
    assert (env["OTEL_LOG_TOOL_DETAILS"], env["OTEL_LOG_USER_PROMPTS"], env["OTEL_LOG_TOOL_CONTENT"],
            env["OTEL_LOG_RAW_API_BODIES"]) == ("0", "0", "0", "0")
    section = tel.codex_config(t)
    assert "log_user_prompt = false" in section and 'endpoint = "http://collector:4318/v1/traces"' in section
    add_otel(tmp_path, section)
    add_otel(tmp_path, section)  # a template with its own [otel] keeps it
    assert (tmp_path / "config.toml").read_text().count("[otel]") == 1


def test_every_session_is_a_span_and_the_ledger_knows_its_trace(engine, services):
    tel, exporter = traced()
    services.telemetry = tel
    fid = engine.start(product_id="FARM", title="Offline crop log", description="x")["feature_id"]
    v = engine.auto_drive(fid)
    spans = exporter.get_finished_spans()
    names = {s.name for s in spans}
    assert {"vrixel.agent spec/spec", "vrixel.agent build/coder", "vrixel.agent build/reviewer"} <= names
    coder = [s for s in spans if s.name == "vrixel.agent build/coder"]
    assert {s.attributes["vrixel.task"] for s in coder} == {t["id"] for t in engine._state(fid)["tasks"]}
    rows = services.ledger.records(fid)
    assert all(r.trace_id for r in rows)  # every call belongs to a traced session
    sessions = v["agent_sessions"]
    assert len(sessions) == len({r.trace_id for r in rows})
    built = [x for x in sessions if x["role"] == "coder"]
    assert built[0]["url"] == f"https://lf.example/traces/{built[0]['trace_id']}" and built[0]["task"].startswith("TASK-")
    g6 = [e for e in v["gate_decisions"] if e["gate"] == "g6"]
    assert g6  # the release was decided with the build sessions in front of it


def test_a_failed_session_is_an_error_span():
    tel, exporter = traced()

    class Boom:
        name, capabilities = "generic", frozenset({"text"})

        def run(self, task):
            raise RuntimeError("provider down")

    with pytest.raises(RuntimeError):
        TracedRuntime(Boom(), tel).run(task())
    [s] = exporter.get_finished_spans()
    assert s.status.status_code.name == "ERROR" and "provider down" in s.status.description


def test_full_content_adds_the_prompt_and_reply_for_general_lanes_only():
    tel, exporter = traced(TelemetryConfig(endpoint="http://c", content={"general": "full"}))

    class Echo:
        name, capabilities = "generic", frozenset({"text"})

        def run(self, task):
            from vrixel_sdlc.runtime import AgentResult

            return AgentResult(text="the reply", tokens=10, cost_usd=0.01)

    TracedRuntime(Echo(), tel).run(task("general"))
    TracedRuntime(Echo(), tel).run(task("phi"))
    general, phi = exporter.get_finished_spans()
    assert general.events[0].attributes["gen_ai.completion"] == "the reply" and not phi.events

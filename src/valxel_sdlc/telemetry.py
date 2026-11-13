"""Traces of agents' work (OpenTelemetry).

Every agent session (a coder's Claude Code session, a critic's single call, a Codex review) is
one span, `valxel.agent <phase>/<role>`, with the feature, product, phase, role, task, attempt,
data class and config version as attributes, plus OpenTelemetry's GenAI attributes (operation,
agent, model, provider). The session's own telemetry nests under it:

- Claude Code exports spans for each turn, model request, tool call and subagent, and events for
  tool results. The session gets the OTLP settings and a TRACEPARENT, so its spans join our trace.
- Codex exports a session span with model requests and tool calls, configured in the session's
  own CODEX_HOME, with the same trace context and resource attributes.
- Single calls through the gateway (documents, critics) are the agent span itself.

What spans may carry beyond structure depends on the product's data class (config/telemetry.yaml):
`none` (structure only: tool names, timings, tokens, cost, outcome), `tool_details` (plus tool
arguments) or `full` (plus prompts and tool output). It is `none` everywhere until a trace store is
approved, and PHI and payments go above it only when the store is under a BAA.

Every ledger row carries the trace id of the session that made it, so the feature page and the G5
and G6 decisions link each session to its trace. Without an endpoint, or without the
OpenTelemetry SDK installed, nothing is exported and trace ids are empty.
"""

from __future__ import annotations

import contextvars
import logging
import os
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote

import yaml

log = logging.getLogger(__name__)

LEVELS = ("none", "tool_details", "full")
CONTENT_MAX = 8000  # characters of prompt or reply on a span event, at `full`

# Who the current agent work is for, beyond the call tags: the task and the attempt.
_labels: contextvars.ContextVar[dict] = contextvars.ContextVar("valxel_agent_labels", default={})


@contextmanager
def labels(**kw):
    """Label the agent sessions started inside this block (task, attempt, repo...)."""
    token = _labels.set({**_labels.get(), **{k: str(v) for k, v in kw.items() if v not in (None, "")}})
    try:
        yield
    finally:
        _labels.reset(token)


def push_labels(**kw):
    """Like `labels`, for a loop body: returns a token for pop_labels."""
    return _labels.set({**_labels.get(), **{k: str(v) for k, v in kw.items() if v not in (None, "")}})


def pop_labels(token) -> None:
    if token is not None:
        _labels.reset(token)


def current_labels() -> dict:
    return dict(_labels.get())


def current_ids() -> tuple[str, str]:
    """(trace id, span id) of the active span, as hex; empty when nothing is traced."""
    try:
        from opentelemetry import trace
    except ImportError:
        return "", ""
    ctx = trace.get_current_span().get_span_context()
    if not ctx.is_valid:
        return "", ""
    return f"{ctx.trace_id:032x}", f"{ctx.span_id:016x}"


@dataclass
class TelemetryConfig:
    endpoint: str = ""
    headers: dict = field(default_factory=dict)
    service_name: str = "valxel-sdlc"
    content: dict = field(default_factory=dict)
    content_store_baa: bool = False
    trace_url: str = ""

    def level(self, data_class: str) -> str:
        lvl = self.content.get(data_class, "none")
        if data_class != "general" and not self.content_store_baa:
            return "none"  # regulated data reaches a trace store only under a BAA
        return lvl if lvl in LEVELS else "none"

    def link(self, trace_id: str) -> str:
        return self.trace_url.replace("{trace_id}", trace_id) if self.trace_url and trace_id else ""


def _headers(raw: str) -> dict:
    out = {}
    for part in filter(None, (p.strip() for p in (raw or "").split(","))):
        k, _, v = part.partition("=")
        if k and v:
            out[k.strip()] = v.strip()
    return out


def load_config(config_dir: Path | str, env=None) -> TelemetryConfig:
    env = env if env is not None else os.environ
    path = Path(config_dir) / "telemetry.yaml"
    raw = yaml.safe_load(path.read_text()) if path.exists() else {}
    raw = raw or {}
    content = {str(k): str(v) for k, v in (raw.get("content") or {}).items()}
    bad = {k: v for k, v in content.items() if v not in LEVELS}
    if bad:
        raise ValueError(f"telemetry.yaml: content levels must be one of {LEVELS}, not {bad}")
    return TelemetryConfig(
        endpoint=(env.get(raw.get("endpoint_env") or "VALXEL_OTEL_ENDPOINT") or "").rstrip("/"),
        headers=_headers(env.get(raw.get("headers_env") or "VALXEL_OTEL_HEADERS", "")),
        service_name=str(raw.get("service_name") or "valxel-sdlc"),
        content=content, content_store_baa=bool(raw.get("content_store_baa")),
        trace_url=env.get(raw.get("trace_url_env") or "VALXEL_TRACE_URL", ""))


def _otlp_provider(cfg: TelemetryConfig):
    """A tracer provider exporting OTLP/HTTP to the endpoint, or None without the SDK."""
    try:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError:
        log.warning("VALXEL_OTEL_ENDPOINT is set but the OpenTelemetry SDK isn't installed "
                    "(pip install '.[telemetry]'): agent sessions aren't traced")
        return None
    provider = TracerProvider(resource=Resource.create({"service.name": cfg.service_name}))
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=f"{cfg.endpoint}/v1/traces",
                                                                    headers=cfg.headers or None)))
    return provider


def _attr_list(attrs: dict) -> str:
    """OTEL_RESOURCE_ATTRIBUTES: key=value pairs, values percent-encoded (commas and = are reserved)."""
    return ",".join(f"{k}={quote(str(v), safe='')}" for k, v in attrs.items() if v not in (None, ""))


class Telemetry:
    def __init__(self, cfg: TelemetryConfig | None = None, provider=None, config_version: str = "") -> None:
        self.cfg = cfg or TelemetryConfig()
        self.provider = provider if provider is not None else (_otlp_provider(self.cfg) if self.cfg.endpoint else None)
        self._tracer = self.provider.get_tracer("valxel_sdlc") if self.provider is not None else None
        self.config_version = config_version

    @property
    def enabled(self) -> bool:
        return self._tracer is not None

    def attributes(self, task, runtime: str, config_version: str = "") -> dict:
        t = task.tags
        out = {"valxel.feature_id": t.feature_id, "valxel.product_id": t.product_id, "valxel.phase": t.phase,
               "valxel.role": t.role, "valxel.data_class": t.data_class, "valxel.runtime": runtime,
               "valxel.prompt_version": t.prompt_version,
               "valxel.config_version": config_version or self.config_version,
               "gen_ai.operation.name": "invoke_agent", "gen_ai.agent.name": t.role}
        out.update({f"valxel.{k}": v for k, v in current_labels().items()})
        return {k: v for k, v in out.items() if v not in (None, "")}

    @contextmanager
    def agent_span(self, task, runtime: str, *, model: str = "", provider: str = "", config_version: str = ""):
        """The span for one agent session; yields it (None when tracing is off)."""
        if not self.enabled:
            yield None
            return
        attrs = self.attributes(task, runtime, config_version)
        if model:
            attrs["gen_ai.request.model"] = model.split("/", 1)[-1]
        if provider:
            attrs["gen_ai.provider.name"] = provider
        with self._tracer.start_as_current_span(f"valxel.agent {task.phase}/{task.role}", attributes=attrs) as span:
            yield span

    def traceparent(self) -> str:
        trace_id, span_id = current_ids()
        return f"00-{trace_id}-{span_id}-01" if trace_id else ""

    def subprocess_env(self, task, runtime: str) -> dict:
        """Environment for a Claude Code or Codex session: export to the collector under our span,
        tagged like it, with content as the data class allows."""
        if not self.enabled or not self.cfg.endpoint:
            return {}
        level = self.cfg.level(task.tags.data_class)
        attrs = {k: v for k, v in self.attributes(task, runtime).items() if k.startswith("valxel.")}
        env = {
            "OTEL_EXPORTER_OTLP_ENDPOINT": self.cfg.endpoint,
            "OTEL_EXPORTER_OTLP_PROTOCOL": "http/protobuf",
            "OTEL_SERVICE_NAME": f"{self.cfg.service_name}-{runtime.replace('_', '-')}",
            "OTEL_RESOURCE_ATTRIBUTES": _attr_list(attrs),
        }
        if self.cfg.headers:
            env["OTEL_EXPORTER_OTLP_HEADERS"] = ",".join(f"{k}={v}" for k, v in self.cfg.headers.items())
        parent = self.traceparent()
        if parent:
            env["TRACEPARENT"] = parent
        if runtime == "claude_code":
            env |= {
                "CLAUDE_CODE_ENABLE_TELEMETRY": "1", "CLAUDE_CODE_ENHANCED_TELEMETRY_BETA": "1",
                "OTEL_TRACES_EXPORTER": "otlp", "OTEL_LOGS_EXPORTER": "otlp", "OTEL_METRICS_EXPORTER": "otlp",
                # Short sessions end before the default intervals: flush often.
                "OTEL_TRACES_EXPORT_INTERVAL": "2000", "OTEL_LOGS_EXPORT_INTERVAL": "2000",
                "OTEL_METRIC_EXPORT_INTERVAL": "10000",
                # Content only as far as the data class allows; set either way so nothing inherited leaks.
                "OTEL_LOG_TOOL_DETAILS": "1" if level in ("tool_details", "full") else "0",
                "OTEL_LOG_USER_PROMPTS": "1" if level == "full" else "0",
                "OTEL_LOG_TOOL_CONTENT": "1" if level == "full" else "0",
                "OTEL_LOG_RAW_API_BODIES": "0",
            }
        return env

    def codex_config(self, task) -> str:
        """The [otel] table for a Codex session's config.toml (log events and traces over OTLP/HTTP)."""
        if not self.enabled or not self.cfg.endpoint:
            return ""
        level = self.cfg.level(task.tags.data_class)
        headers = ", ".join(f'"{k}" = "{v}"' for k, v in self.cfg.headers.items())
        hdr = f"\nheaders = {{ {headers} }}" if headers else ""
        return (f'\n[otel]\nenvironment = "valxel"\nlog_user_prompt = {"true" if level == "full" else "false"}\n'
                f'\n[otel.exporter.otlp-http]\nendpoint = "{self.cfg.endpoint}/v1/logs"\nprotocol = "binary"{hdr}\n'
                f'\n[otel.trace_exporter.otlp-http]\nendpoint = "{self.cfg.endpoint}/v1/traces"\nprotocol = "binary"{hdr}\n')

    def record_result(self, span, task, result=None, *, error: Exception | None = None, content: tuple = ()) -> None:
        """Outcome attributes on the agent span; prompt and reply as events only at `full`."""
        if span is None:
            return
        if result is not None:
            span.set_attribute("valxel.tokens", int(result.tokens or 0))
            span.set_attribute("valxel.cost_usd", float(result.cost_usd or 0.0))
            if result.role and result.role != task.role:
                span.set_attribute("valxel.fallback_role", result.role)
            if getattr(result, "extra_skills", ()):
                span.set_attribute("valxel.extra_skills", list(result.extra_skills))
        if error is not None:
            from opentelemetry.trace import Status, StatusCode

            span.record_exception(error)
            span.set_status(Status(StatusCode.ERROR, f"{type(error).__name__}: {str(error)[:200]}"))
        if content and self.cfg.level(task.tags.data_class) == "full":
            prompt, reply = content
            span.add_event("gen_ai.content", {"gen_ai.prompt": str(prompt)[:CONTENT_MAX],
                                              "gen_ai.completion": str(reply)[:CONTENT_MAX]})

    def flush(self) -> None:
        if self.provider is not None and hasattr(self.provider, "force_flush"):
            self.provider.force_flush()


class TracedRuntime:
    """Wraps a runtime so each session is one span, and hands the session its telemetry settings."""

    def __init__(self, inner, telemetry: Telemetry) -> None:
        self.inner = inner
        self.telemetry = telemetry
        self.name = inner.name
        self.capabilities = inner.capabilities

    def __getattr__(self, item):
        return getattr(self.inner, item)

    def run(self, task):
        gateway = getattr(self.inner, "gateway", None)
        settings = getattr(gateway, "settings", None)
        role = settings.role(task.role) if settings is not None else None
        # The config this runtime's gateway runs (the canary's, for a canary feature).
        with self.telemetry.agent_span(task, self.name, model=getattr(role, "model", ""),
                                       provider=getattr(role, "provider", ""),
                                       config_version=getattr(gateway, "config_version", "")) as span:
            task.telemetry = {"env": self.telemetry.subprocess_env(task, self.name),
                              "codex_config": self.telemetry.codex_config(task) if self.name == "codex" else ""}
            try:
                res = self.inner.run(task)
            except Exception as exc:
                self.telemetry.record_result(span, task, error=exc)
                raise
            self.telemetry.record_result(span, task, res, content=(task.layers.tail, res.text))
            return res


def sessions(records, link=None, *, phases: tuple[str, ...] = ()) -> list[dict]:
    """The feature's agent sessions from its ledger rows: one per trace (or per call when it wasn't
    traced), with what it was for, what it cost and a link to its trace."""
    groups: dict[str, list] = {}
    for r in records:
        if phases and r.phase not in phases:
            continue
        key = r.trace_id or f"call:{r.request_id or r.ts}"
        groups.setdefault(key, []).append(r)
    out = []
    for key, rows in groups.items():
        first = rows[0]
        trace_id = first.trace_id
        out.append({
            "trace_id": trace_id, "url": link(trace_id) if (link and trace_id) else "",
            "at": first.ts, "phase": first.phase, "role": first.role, "runtime": first.runtime,
            "model": first.model, "task": first.task_id, "attempt": first.attempt,
            "calls": len(rows), "duration_s": round(sum(r.duration_s for r in rows), 2),
            "extra_skills": sorted({s for r in rows for s in (getattr(r, "extra_skills", "") or "").split(",") if s}),
            "tokens": sum(r.total_input + r.output_tokens for r in rows),
            "cost_usd": round(sum(r.est_cost_usd for r in rows), 4)})
    return sorted(out, key=lambda x: x["at"])

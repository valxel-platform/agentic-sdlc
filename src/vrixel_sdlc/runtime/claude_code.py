"""Claude Code runtime: runs a task headless through the Claude Agent SDK.

The session starts from Claude Code's own system prompt (the `claude_code` preset: how to use
its tools, its safety rules) with our layers L2-L5 appended, so the agent keeps everything Claude
Code knows and adds the role's skills, the constitution, the product and the feature. Its
environment details (working directory, platform) move to the first message
(`exclude_dynamic_sections`), so the appended layers cache the same way from any checkout.

Settings come from the repository only (`setting_sources=["project"]`): the checkout's CLAUDE.md
and `.claude/skills/` load, nothing from the container's home directory. The plugins in the
config's skills folder (VRIXEL_SKILLS_DIR, or a copy at the config's pin: vrixel_sdlc.plugins)
load too, unless config/plugins.yaml turns `on_demand` off for the role: the agent then sees every
installed skill by name and description and can load one beyond those the phase pastes into L2.
Each skill it loads that way is recorded (`extra_skills`). The plugins carry skills and commands
only: plugins/prepare.sh removes their connectors (.mcp.json) and hooks, so a plugin gives the
agent no outside tool and runs nothing on its own.

Live mode only (`pip install .[live]`). Usage is written to the ledger from the SDK's
result message; per the SDK docs, its cost fields are client-side estimates and billing
comes from the Usage and Cost API. Not exercised by the offline test suite.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path

from .. import plugins
from ..gateway import ModelGateway, ModelResponse, PolicyViolation, ProviderUnavailable, is_transient
from ..sandbox import claude_sandbox, claude_settings, session_env
from .base import AgentResult, AgentTask

log = logging.getLogger(__name__)

# The SDK hands the appended prompt to the Claude Code process as one argument, and Linux caps one
# argument at 128 KiB. Past this size the session would fail to start; say why first.
APPEND_LIMIT_BYTES = 120_000


def plugin_dirs_from(base: str | None) -> list[str]:
    """Each installed plugin under VRIXEL_SKILLS_DIR (a folder with skills/, agents/, commands/,
    hooks/ or .claude-plugin/), as an absolute path, sorted so sessions start the same way."""
    if not base or not Path(base).is_dir():
        return []
    marks = ("skills", "agents", "commands", "hooks", ".claude-plugin")
    return sorted(str(d.resolve()) for d in Path(base).iterdir()
                  if d.is_dir() and any((d / m).exists() for m in marks))


def session_options(task: AgentTask, role, model: str, *, cwd: str | None, plugin_dirs: list[str],
                    home: str | None = None) -> dict:
    """The ClaudeAgentOptions for one session, as keyword arguments (kept free of the SDK so the
    offline tests can check them)."""
    lay = task.layers.layers()
    # Stable layers first: skills, constitution, product, then this feature.
    append = "\n\n".join(lay[k] for k in ("L2", "L3", "L4", "L5") if lay[k])
    if len(append.encode()) > APPEND_LIMIT_BYTES:
        log.warning("%s/%s: the appended prompt is %d bytes, near the 128 KiB argument limit; "
                    "trim the feature context", task.phase, task.role, len(append.encode()))
    return {
        "system_prompt": {"type": "preset", "preset": "claude_code", "append": append,
                          "exclude_dynamic_sections": True},
        "setting_sources": ["project"],
        # No hooks from anywhere (a repository's .claude/settings.json runs nothing), and the tool's
        # own files and secrets unreadable (vrixel_sdlc.sandbox).
        "settings": json.dumps(claude_settings(home or "")),
        "plugins": [{"type": "local", "path": p} for p in plugin_dirs],
        "model": model.split("/", 1)[-1],
        "cwd": task.cwd or cwd,
        # Telemetry export under the tool's span, when tracing is on (vrixel_sdlc.telemetry).
        # Only the allowlisted environment: every other name the tool's process has is blanked.
        "env": session_env("claude_code", home or os.environ.get("HOME", ""),
                           {"CLAUDE_CODE_PROMPT_CACHE_TTL": "1h", **((task.telemetry or {}).get("env") or {})}),
        # Shell commands run in Claude Code's sandbox, network only to the allowlist; a read-only
        # session has no shell to sandbox.
        **({} if task.read_only else {"sandbox": claude_sandbox()}),
        # Caps: a session that runs away stops here, not at the feature's budget.
        "max_turns": task.max_turns or role.max_turns,
        "max_budget_usd": session_cap(task, role),
        **({"output_format": {"type": "json_schema", "schema": task.schema}} if task.schema else {}),
        # Research may search and read the web without asking (vrixel_sdlc.research).
        **({"allowed_tools": ["WebSearch", "WebFetch"]} if task.web_searches else {}),
        # Reading a checkout: look, never touch.
        **({"allowed_tools": ["Read", "Grep", "Glob"],
            "disallowed_tools": ["Write", "Edit", "MultiEdit", "NotebookEdit", "Bash"]} if task.read_only else {}),
    }


class ClaudeCodeRuntime:
    name = "claude_code"
    capabilities = frozenset({"text", "native_plugins", "subagents", "code_execution"})

    def __init__(self, gateway: ModelGateway, *, cwd: str | None = None, plugin_dirs: list[str] | None = None):
        self.gateway = gateway
        self.cwd = cwd
        settings = gateway.settings
        base = getattr(settings, "skills_dir", "") or os.environ.get("VRIXEL_SKILLS_DIR")
        self.plugin_dirs = plugin_dirs if plugin_dirs is not None else plugin_dirs_from(base)

    def plugins_for(self, role: str) -> list[str]:
        """Every installed plugin, or none when the role gets only its phase's skills."""
        cfg = getattr(self.gateway.settings, "plugins", None)
        return self.plugin_dirs if cfg is None or cfg.on_demand(role) else []

    def run(self, task: AgentTask) -> AgentResult:
        role = self.gateway.settings.role(task.role)
        if not self.gateway.allowed(role.provider, task.tags.data_class):
            raise PolicyViolation(f"{task.tags.data_class} data may not reach {role.provider!r}")
        try:
            return asyncio.run(self._run(task, role.model))
        except ProviderUnavailable:
            raise
        except Exception as exc:  # the CLI reports overload and rate limits as text
            if is_transient(str(exc)):
                raise ProviderUnavailable(f"claude_code: {exc}") from exc
            raise

    async def _run(self, task: AgentTask, model: str) -> AgentResult:
        from claude_agent_sdk import ClaudeAgentOptions  # lazy import

        role = self.gateway.settings.role(task.role)
        import tempfile

        # Every session gets a home of its own: nothing from the container's home directory.
        with tempfile.TemporaryDirectory(prefix="vrixel-session-") as home:
            Path(home, "tmp").mkdir()
            options = ClaudeAgentOptions(**session_options(task, role, model, cwd=self.cwd,
                                                           plugin_dirs=self.plugins_for(task.role),
                                                           home=(task.sandbox or {}).get("home") or home))
            return await self._query(task, model, options)

    async def _query(self, task: AgentTask, model: str, options) -> AgentResult:
        from claude_agent_sdk import ResultMessage, query  # lazy import

        text, result, loaded = "", None, set()
        started = time.monotonic()
        async for message in query(prompt=task.layers.tail, options=options):
            loaded |= skills_used(message)
            if getattr(message, "subtype", None) == "init":
                data = getattr(message, "data", {}) or {}
                for err in data.get("plugin_errors") or []:
                    log.warning("%s/%s: a plugin did not load: %s", task.phase, task.role, err)
            if isinstance(message, ResultMessage):
                result = message
                if getattr(message, "is_error", False) and is_transient(str(getattr(message, "result", ""))):
                    raise ProviderUnavailable(f"claude_code: {getattr(message, 'result', '')}")
                structured = getattr(message, "structured_output", None)
                text = (json.dumps(structured) if structured is not None else getattr(message, "result", "")) or text
        usage = (getattr(result, "usage", None) or {}) if result else {}
        resp = ModelResponse(
            text=text,
            input_tokens=int(usage.get("input_tokens", 0)),
            cache_creation_input_tokens=int(usage.get("cache_creation_input_tokens", 0)),
            cache_read_input_tokens=int(usage.get("cache_read_input_tokens", 0)),
            output_tokens=int(usage.get("output_tokens", 0)),
        )
        extra = plugins.extra(loaded, task.skills)
        if extra:
            log.info("%s/%s loaded skills beyond its phase's: %s", task.phase, task.role, ", ".join(extra))
        rec = self.gateway.record_external(
            task.tags, runtime=self.name, model=model, layers=task.layers, response=resp,
            duration_s=time.monotonic() - started, extra_skills=extra,
        )
        sdk_cost = getattr(result, "total_cost_usd", None) if result else None
        return AgentResult(
            text=text,
            tokens=rec.total_tokens,
            cost_usd=float(sdk_cost) if sdk_cost is not None else rec.cost_usd,
            request_ids=[resp.request_id],
            extra_skills=extra,
        )


def skills_used(message) -> set[str]:
    """The skills an assistant message invokes through Claude Code's Skill tool."""
    out = set()
    for block in getattr(message, "content", None) or []:
        if getattr(block, "name", None) == "Skill":
            name = (getattr(block, "input", None) or {}).get("skill") or (getattr(block, "input", None) or {}).get("command")
            if name:
                out.add(str(name).lstrip("/"))
    return out


def session_cap(task: AgentTask, role) -> float:
    """Dollars one session may spend: the role's cap, and never more than the task allows."""
    cap = float(role.max_call_usd)
    if task.max_budget_usd is not None:
        cap = min(cap, max(0.0, float(task.max_budget_usd)))
    return round(cap, 4)

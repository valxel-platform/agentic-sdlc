"""Claude Code runtime: runs a task headless through the Claude Agent SDK.

The session starts from Claude Code's own system prompt (the `claude_code` preset: how to use
its tools, its safety rules) with our layers L2-L5 appended, so the agent keeps everything Claude
Code knows and adds the role's skills, the constitution, the product and the feature. Its
environment details (working directory, platform) move to the first message
(`exclude_dynamic_sections`), so the appended layers cache the same way from any checkout.

Settings come from the repository only (`setting_sources=["project"]`): the checkout's CLAUDE.md
and `.claude/skills/` load, nothing from the container's home directory. The plugins in the
config's skills folder (VALXEL_SKILLS_DIR, or a copy at the config's pin: valxel_sdlc.plugins)
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
import contextlib
import dataclasses
import json
import logging
import os
import time
from pathlib import Path

from .. import gateway_sessions, plugins, sessions
from ..gateway import ModelGateway, ModelResponse, PolicyViolation, ProviderUnavailable, is_transient
from ..sandbox import claude_sandbox, claude_settings, session_env
from .base import AgentResult, AgentTask

log = logging.getLogger(__name__)

# The SDK hands the appended prompt to the Claude Code process as one argument, and Linux caps one
# argument at 128 KiB. Past this size the session would fail to start; say why first.
APPEND_LIMIT_BYTES = 120_000


def plugin_dirs_from(base: str | None) -> list[str]:
    """Each installed plugin under VALXEL_SKILLS_DIR (a folder with skills/, agents/, commands/,
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
        # own files and secrets unreadable (valxel_sdlc.sandbox).
        "settings": json.dumps(claude_settings(home or "")),
        "plugins": [{"type": "local", "path": p} for p in plugin_dirs],
        "model": model.split("/", 1)[-1],
        "cwd": task.cwd or cwd,
        # Telemetry export under the tool's span, when tracing is on (valxel_sdlc.telemetry).
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
        # Research may search and read the web without asking (valxel_sdlc.research).
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
        base = getattr(settings, "skills_dir", "") or os.environ.get("VALXEL_SKILLS_DIR")
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
        role = self.gateway.settings.role(task.role)
        import tempfile

        # Every session gets a home of its own: nothing from the container's home directory.
        with tempfile.TemporaryDirectory(prefix="valxel-session-") as tmp_home:
            Path(tmp_home, "tmp").mkdir()
            home = (task.sandbox or {}).get("home") or tmp_home
            kwargs = session_options(task, role, model, cwd=self.cwd, plugin_dirs=self.plugins_for(task.role), home=home)
            store = sessions.TranscriptStore(self.gateway.settings.artifact_dir)
            with contextlib.ExitStack() as stack:
                if gateway_sessions.configured():  # a short-lived gateway token instead of the tool's key
                    try:
                        token_env = stack.enter_context(gateway_sessions.session_token(
                            task.tags, model, kwargs.get("max_budget_usd") or session_cap(task, role),
                            int(os.environ.get("VALXEL_SESSION_TIMEOUT_S", "3600")) + 600))
                    except gateway_sessions.GatewayUnavailable as exc:
                        raise ProviderUnavailable(f"claude_code: {exc}") from exc
                    kwargs["env"] = {**kwargs["env"], **token_env}
                return await self._session(task, model, kwargs, home, tmp_home, store)

    async def _session(self, task: AgentTask, model: str, kwargs: dict, home: str, tmp_home: str, store) -> AgentResult:
        from claude_agent_sdk import ClaudeAgentOptions  # lazy import

        fid = task.tags.feature_id
        from ..sandbox import remote

        if remote():  # in a sandbox of its own (valxel_sdlc.session_runner)
            return self._run_remote(task, model, kwargs, store, tmp_home)
        if task.resume:
            try:  # the kept transcript where Claude Code looks for this working directory's sessions
                store.restore(fid, task.resume, sessions.claude_project_dir(home, kwargs["cwd"] or os.getcwd())
                              / f"{task.resume}.jsonl")
                res = await self._query(task, model, ClaudeAgentOptions(**kwargs, resume=task.resume),
                                        session_mode="resumed")
                res.resumed = True
            except ProviderUnavailable:
                raise
            except Exception as exc:  # gone, or Claude Code can't find it: start fresh, from the handoff
                log.warning("%s %s/%s: couldn't resume session %s (%s); starting fresh", fid, task.phase,
                            task.role, task.resume, exc)
                fresh = dataclasses.replace(task, resume="", session_mode="handoff" if task.fresh_tail else "fresh",
                                            layers=dataclasses.replace(task.layers, tail=task.fresh_tail or task.layers.tail))
                res = await self._query(fresh, model, ClaudeAgentOptions(**kwargs), session_mode=fresh.session_mode)
        else:
            res = await self._query(task, model, ClaudeAgentOptions(**kwargs), session_mode=task.session_mode)
        if task.keep_transcript and res.session_id:
            found = sessions.find_transcript(home, res.session_id)
            if not (found and store.keep(fid, res.session_id, found)):
                log.info("%s %s/%s: no transcript to keep for session %s", fid, task.phase, task.role, res.session_id)
        return res

    def _run_remote(self, task: AgentTask, model: str, kwargs: dict, store, scratch: str) -> AgentResult:
        """The session in its own sandbox: hand over the spec, account for the outcome here."""
        from .. import session_runner

        fid = task.tags.feature_id
        transcript = ""
        if task.resume and store.has(fid, task.resume):
            transcript = store.restore(fid, task.resume, Path(scratch) / "resume.jsonl").read_text(encoding="utf-8")
        spec = session_runner.spec_for(task, kwargs, transcript=transcript,
                                       fallback_prompt=task.fresh_tail or task.layers.tail,
                                       fallback_mode="handoff" if task.fresh_tail else task.session_mode,
                                       plugins_on=bool(self.plugins_for(task.role)))
        out = session_runner.executor().run(spec)
        if not out.get("ok"):
            if out.get("transient"):
                raise ProviderUnavailable(f"claude_code: the session's provider was unavailable ({out.get('error')})")
            raise RuntimeError(f"the session failed in its sandbox: {out.get('error')}")
        used = task
        if task.resume and not out.get("resumed"):
            log.warning("%s %s/%s: couldn't resume session %s in its sandbox; started fresh", fid, task.phase,
                        task.role, task.resume)
            used = dataclasses.replace(task, resume="", layers=dataclasses.replace(
                task.layers, tail=task.fresh_tail or task.layers.tail))
        res = self._finish(used, model, out, str(out.get("session_mode") or task.session_mode))
        res.resumed = bool(out.get("resumed"))
        if task.keep_transcript and res.session_id and out.get("transcript"):
            kept = Path(scratch) / f"{res.session_id}.jsonl"
            kept.write_text(out["transcript"], encoding="utf-8")
            store.keep(fid, res.session_id, kept)
        return res

    async def _query(self, task: AgentTask, model: str, options, session_mode: str = "fresh") -> AgentResult:
        return self._finish(task, model, await run_query(task.layers.tail, options), session_mode)

    def _finish(self, task: AgentTask, model: str, out: dict, session_mode: str) -> AgentResult:
        """A session's outcome (run_query, here or in its sandbox) into the ledger and a result."""
        if out.get("is_error") and is_transient(str(out.get("error") or "")):
            raise ProviderUnavailable(f"claude_code: {out.get('error')}")
        for err in out.get("plugin_errors") or []:
            log.warning("%s/%s: a plugin did not load: %s", task.phase, task.role, err)
        usage = out.get("usage") or {}
        resp = ModelResponse(
            text=out.get("text") or "",
            input_tokens=int(usage.get("input_tokens", 0)),
            cache_creation_input_tokens=int(usage.get("cache_creation_input_tokens", 0)),
            cache_read_input_tokens=int(usage.get("cache_read_input_tokens", 0)),
            output_tokens=int(usage.get("output_tokens", 0)),
            context_tokens=int(out.get("peak") or 0),
            session_id=str(out.get("session_id") or ""),
            context_window=window_of(out.get("model_usage") or {}, model),
            served_model=served_of(out.get("model_usage") or {}),
        )
        extra = plugins.extra(set(out.get("skills") or ()), task.skills)
        if extra:
            log.info("%s/%s loaded skills beyond its phase's: %s", task.phase, task.role, ", ".join(extra))
        rec = self.gateway.record_external(
            task.tags, runtime=self.name, model=model, layers=task.layers, response=resp,
            duration_s=float(out.get("duration_s") or 0.0), extra_skills=extra, session_mode=session_mode,
        )
        sdk_cost = out.get("total_cost_usd")
        return AgentResult(
            text=resp.text,
            tokens=rec.total_tokens,
            cost_usd=float(sdk_cost) if sdk_cost is not None else rec.cost_usd,
            request_ids=[resp.request_id],
            extra_skills=extra,
            session_id=resp.session_id,
            context_fill=rec.record.context_fill,
            model=model,
            runtime=self.name,
        )


async def run_query(prompt: str, options) -> dict:
    """One Claude Code session through the SDK, as a plain outcome: what the tool needs of it.
    The same here and in a session's sandbox (valxel_sdlc.session_runner)."""
    from claude_agent_sdk import ResultMessage, query  # lazy import

    text, result, loaded, errors = "", None, set(), []
    peak, session_id = 0, ""  # the largest prompt of any one call, and the session (valxel_sdlc.context)
    started = time.monotonic()
    async for message in query(prompt=prompt, options=options):
        loaded |= skills_used(message)
        peak = max(peak, prompt_tokens(getattr(message, "usage", None)) if not isinstance(message, ResultMessage) else 0)
        session_id = getattr(message, "session_id", None) or session_id
        if getattr(message, "subtype", None) == "init":
            errors += list((getattr(message, "data", {}) or {}).get("plugin_errors") or [])
        if isinstance(message, ResultMessage):
            result = message
            structured = getattr(message, "structured_output", None)
            text = (json.dumps(structured) if structured is not None else getattr(message, "result", "")) or text
    return {"text": text, "usage": (getattr(result, "usage", None) or {}) if result else {},
            "peak": peak, "session_id": str(session_id or ""), "skills": sorted(loaded), "plugin_errors": errors,
            "is_error": bool(getattr(result, "is_error", False)) if result else False,
            "error": str(getattr(result, "result", "") or "") if result and getattr(result, "is_error", False) else "",
            "model_usage": (getattr(result, "model_usage", None) or {}) if result else {},
            "total_cost_usd": getattr(result, "total_cost_usd", None) if result else None,
            "duration_s": round(time.monotonic() - started, 3)}


def prompt_tokens(usage) -> int:
    """The prompt one model call sent (input, cache writes and cache reads), from a message's usage."""
    if not isinstance(usage, dict):
        return 0
    return sum(int(usage.get(k) or 0) for k in ("input_tokens", "cache_creation_input_tokens",
                                                  "cache_read_input_tokens"))


def reported_window(result, model: str) -> int:
    """The context window Claude Code reported for the model with its result, 0 when it didn't."""
    return window_of(getattr(result, "model_usage", None) or {}, model)


def served_of(usage: dict) -> str:
    """The model that did most of a session's work, by output tokens, from Claude Code's per-model
    usage (a session can call a smaller model for side tasks)."""
    best, most = "", -1
    for key, u in usage.items() if isinstance(usage, dict) else ():
        n = int((u or {}).get("outputTokens") or (u or {}).get("output_tokens") or 0) if isinstance(u, dict) else 0
        if n > most:
            best, most = str(key), n
    return best


def window_of(usage: dict, model: str) -> int:
    name = model.split("/", 1)[-1]
    for key, u in usage.items() if isinstance(usage, dict) else ():
        if isinstance(u, dict) and (key == name or key.startswith(name)):
            return int(u.get("contextWindow") or u.get("context_window") or 0)
    return 0


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

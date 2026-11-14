"""Codex runtime: runs a task headless through OpenAI's Codex CLI (`codex exec`), with OpenAI's
models or any model behind an OpenAI-compatible Responses API (a self-hosted vLLM, for example).

The stage contract is the same as Claude Code's: the session works in the checkout, reads the
repository's AGENTS.md as Codex always does, and replies with the stage's JSON schema
(`--output-schema`). Each session gets its own Codex home (CODEX_HOME), copied from the image's
template (VALXEL_CODEX_HOME), in which the same skills-only plugins Claude Code loads are
installed: the agent sees every plugin skill by name and description and reads one when it needs
it, unless config/plugins.yaml turns `on_demand` off for the role (or the config pins plugins other
than the image's: the template has the image's). Each skill file a session reads beyond its
phase's is recorded (`extra_skills`). Nothing from a user's own Codex configuration applies. Codex keeps its own built-in instructions; our layers L2-L5 (the phase's
skills, the constitution, the product, the feature) go first in the prompt, then the task, so
providers that cache prompt prefixes reuse them. The prompt goes through stdin, so its size is
not limited by the process-argument cap.

Codex has no turn or dollar cap of its own. A session gets the role's `max_minutes`, its usage is
recorded from the `turn.completed` events, and a session that spent more than its cap is logged so
the budget check that follows sees it.

A provider that answers 429 or 5xx makes Codex fail the turn after its retries, which reads as
unavailable. One that can't be reached at all makes Codex wait for the network indefinitely, so
the session is stopped after a few reconnect attempts and also reads as unavailable: the role's
fallback gets the task instead of the session sitting out its time limit.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import shutil
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable
from pathlib import Path

from .. import plugins
from ..gateway import (
    ModelGateway,
    ModelResponse,
    PolicyViolation,
    ProviderUnavailable,
    is_transient,
)
from .base import AgentResult, AgentTask
from .claude_code import session_cap

log = logging.getLogger(__name__)

Runner = Callable[..., subprocess.CompletedProcess]

# Reconnect notices in a row (with nothing else between) that mean the provider is unreachable.
RECONNECTS_BEFORE_GIVING_UP = 3
UNREACHABLE = json.dumps({"type": "turn.failed", "error": {"message": "connection error: the provider is unreachable"}})


def run_codex(cmd: list[str], *, input: str, env: dict, timeout: float, **_) -> subprocess.CompletedProcess:
    """subprocess.run for `codex exec`, reading its events as they come so a session stuck
    reconnecting to an unreachable provider is stopped early."""
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, env=env)
    lines: queue.Queue = queue.Queue()
    err: list[str] = []
    threading.Thread(target=lambda: [lines.put(line) for line in proc.stdout] + [lines.put(None)], daemon=True).start()
    threading.Thread(target=lambda: err.extend(proc.stderr), daemon=True).start()
    proc.stdin.write(input)
    proc.stdin.close()
    out, reconnects, deadline = [], 0, time.monotonic() + timeout
    while True:
        left = deadline - time.monotonic()
        if left <= 0:
            proc.kill()
            raise subprocess.TimeoutExpired(cmd, timeout, output="".join(out))
        try:
            line = lines.get(timeout=min(left, 5.0))
        except queue.Empty:
            continue
        if line is None:
            break
        out.append(line)
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if ev.get("type") == "error" and "reconnecting" in str(ev.get("message", "")).lower():
            reconnects += 1
            if reconnects >= RECONNECTS_BEFORE_GIVING_UP:
                proc.kill()
                proc.wait()
                return subprocess.CompletedProcess(cmd, -9, stdout="".join(out) + UNREACHABLE + "\n",
                                                   stderr="".join(err))
        else:
            reconnects = 0
    proc.wait()
    return subprocess.CompletedProcess(cmd, proc.returncode, stdout="".join(out), stderr="".join(err))


def codex_prompt(task: AgentTask) -> str:
    lay = task.layers.layers()
    context = "\n\n".join(lay[k] for k in ("L2", "L3", "L4", "L5") if lay[k])
    return f"{context}\n\n---\n\n# Task\n\n{task.layers.tail}\n" if context else task.layers.tail


def codex_command(model: str, *, cwd: str | None, schema_path: str | None, last_path: str,
                  provider: tuple[str, str, str] | None) -> list[str]:
    """`codex exec` for one session. provider: (id, base_url, api_key_env) for a model that is not
    OpenAI's; None uses OpenAI with CODEX_API_KEY."""
    cmd = ["codex", "exec", "--json", "--ephemeral", "--skip-git-repo-check",
           "--sandbox", "workspace-write", "-c", "approval_policy=never",
           "-c", f"model={json.dumps(model.split('/', 1)[-1])}", "-o", last_path]
    if provider:
        pid, url, key_env = provider
        cmd += ["-c", f"model_provider={json.dumps(pid)}",
                "-c", f"model_providers.{pid}.name={json.dumps(pid)}",
                "-c", f"model_providers.{pid}.base_url={json.dumps(url)}"]
        if key_env:
            cmd += ["-c", f"model_providers.{pid}.env_key={json.dumps(key_env)}"]
        # Give up on a busy provider quickly: the role's fallback is the better retry.
        cmd += ["-c", f"model_providers.{pid}.request_max_retries=2",
                "-c", f"model_providers.{pid}.stream_max_retries=2"]
    if cwd:
        cmd += ["--cd", cwd]
    if schema_path:
        cmd += ["--output-schema", schema_path]
    return cmd + ["-"]  # the prompt comes on stdin


def usage_from(events: str) -> dict[str, int]:
    """Sum the token usage of every completed turn in `codex exec --json` output."""
    total = {"input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0}
    for line in events.splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if ev.get("type") == "turn.completed":
            for k in total:
                total[k] += int((ev.get("usage") or {}).get(k) or 0)
    return total


def failure_from(events: str) -> str:
    for line in reversed(events.splitlines()):
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if ev.get("type") in ("error", "turn.failed"):
            err = ev.get("error") or ev
            return str(err.get("message") if isinstance(err, dict) else err)
    return ""


def session_home(template: str, target: Path, *, with_plugins: bool = True) -> str:
    """A Codex home for one session: a copy of the template (the installed plugins and the config
    that enables them), or an empty one. Sessions never share state or see a user's config."""
    if template and Path(template).is_dir():
        target.mkdir(parents=True)
        parts = ("config.toml", "plugins") if with_plugins else ("config.toml",)
        for part in parts:  # what the template is for; never its scratch state
            src = Path(template) / part
            if src.is_dir():
                shutil.copytree(src, target / part, symlinks=True)
            elif src.is_file():
                shutil.copy2(src, target / part)
    else:
        target.mkdir(parents=True)
    return str(target)


def add_otel(home: Path, section: str) -> None:
    """The session's telemetry export (valxel_sdlc.telemetry), unless its config already has one."""
    if not section:
        return
    cfg = home / "config.toml"
    text = cfg.read_text() if cfg.exists() else ""
    if "[otel" in text:
        log.warning("the Codex home template configures [otel] itself; the tool's export is left out")
        return
    cfg.write_text(text + section)


PLUGIN_NAMES = {*plugins.UPSTREAM, "valxel"}


class CodexRuntime:
    name = "codex"
    capabilities = frozenset({"text", "code_execution"})

    def __init__(self, gateway: ModelGateway, *, cwd: str | None = None, runner: Runner = run_codex,
                 home_template: str | None = None):
        self.gateway = gateway
        self.cwd = cwd
        self.runner = runner
        self.home_template = home_template if home_template is not None else os.environ.get("VALXEL_CODEX_HOME", "")

    def run(self, task: AgentTask) -> AgentResult:
        settings = self.gateway.settings
        role = settings.role(task.role)
        if not self.gateway.allowed(role.provider, task.tags.data_class):
            raise PolicyViolation(f"{task.tags.data_class} data may not reach {role.provider!r}")
        from ..sandbox import clean_env

        provider = None
        extra_keys = {}
        if role.provider == "openai":
            from ..secret_ring import get

            extra_keys["CODEX_API_KEY"] = get("CODEX_API_KEY") or get("OPENAI_API_KEY")
        else:
            pc = settings.providers.get(role.provider)
            if pc is None:
                raise RuntimeError(f"No provider {role.provider!r} in models.yaml for role {task.role}")
            provider = (role.provider, pc.url(), pc.api_key_env)
            if pc.api_key_env:
                extra_keys[pc.api_key_env] = os.environ.get(pc.api_key_env, "")
        started = time.monotonic()
        with tempfile.TemporaryDirectory(prefix="valxel-codex-") as tmp:
            # Only the allowlisted environment and the model's key: none of the tool's secrets
            # (valxel_sdlc.sandbox). Codex's workspace-write sandbox confines the commands it runs.
            (Path(tmp) / "user" / "tmp").mkdir(parents=True)
            env = clean_env("codex", str(Path(tmp) / "user"), extra_keys)
            env["CODEX_HOME"] = session_home(self.home_template, Path(tmp) / "home",
                                             with_plugins=self.on_demand(task.role))
            add_otel(Path(env["CODEX_HOME"]), (task.telemetry or {}).get("codex_config", ""))
            env.update((task.telemetry or {}).get("env") or {})
            schema_path = None
            if task.schema:
                schema_path = str(Path(tmp) / "schema.json")
                Path(schema_path).write_text(json.dumps(task.schema))
            last_path = str(Path(tmp) / "last.txt")
            cmd = codex_command(role.model, cwd=task.cwd or self.cwd, schema_path=schema_path, last_path=last_path,
                                provider=provider)
            try:
                proc = self.runner(cmd, input=codex_prompt(task), capture_output=True, text=True,
                                   timeout=role.max_minutes * 60, env=env)
            except subprocess.TimeoutExpired as exc:
                raise RuntimeError(f"codex: {task.phase}/{task.role} ran past {role.max_minutes} minutes") from exc
            events = proc.stdout or ""
            if proc.returncode != 0:
                why = failure_from(events) or (proc.stderr or "").strip()[-500:]
                if is_transient(why):
                    raise ProviderUnavailable(f"codex: {why}")
                raise RuntimeError(f"codex exited {proc.returncode}: {why}")
            last = Path(last_path)
            text = last.read_text() if last.exists() else ""
        u = usage_from(events)
        resp = ModelResponse(
            text=text,
            input_tokens=max(0, u["input_tokens"] - u["cached_input_tokens"]),
            cache_creation_input_tokens=0,
            cache_read_input_tokens=u["cached_input_tokens"],
            output_tokens=u["output_tokens"],
        )
        extra = plugins.extra(plugins.skills_in_paths(events, PLUGIN_NAMES), task.skills)
        rec = self.gateway.record_external(task.tags, runtime=self.name, model=role.model, layers=task.layers,
                                           response=resp, duration_s=time.monotonic() - started,
                                           extra_skills=extra)
        cap = session_cap(task, role)
        if rec.cost_usd > cap:
            log.warning("%s/%s: the codex session cost $%.2f, over its $%.2f cap", task.phase, task.role,
                        rec.cost_usd, cap)
        return AgentResult(text=text, tokens=rec.total_tokens, cost_usd=rec.cost_usd, request_ids=[resp.request_id],
                           extra_skills=extra)

    def on_demand(self, role: str) -> bool:
        """Whether the session gets the template's plugins: the role may load skills by itself, and
        the config's plugins are the image's (the ones installed in the template)."""
        settings = self.gateway.settings
        cfg = getattr(settings, "plugins", None)
        if cfg is not None and not cfg.on_demand(role):
            return False
        installed = os.environ.get("VALXEL_SKILLS_DIR", "")
        mine = getattr(settings, "skills_dir", "") or installed
        return not installed or Path(mine).resolve() == Path(installed).resolve()

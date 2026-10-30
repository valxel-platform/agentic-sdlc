"""Claude Code runtime: runs a task headless through the Claude Agent SDK, so the
Engineering, Product and Design plugins, subagents and hooks run natively.

Live mode only (`pip install .[live]`). Usage is written to the ledger from the SDK's
result message; per the SDK docs, its cost fields are client-side estimates and billing
comes from the Usage and Cost API. Not exercised by the offline test suite.
"""

from __future__ import annotations

import asyncio

from ..gateway import ModelGateway, ModelResponse, PolicyViolation
from .base import AgentResult, AgentTask


class ClaudeCodeRuntime:
    name = "claude_code"
    capabilities = frozenset({"text", "native_plugins", "subagents", "code_execution"})

    def __init__(self, gateway: ModelGateway, *, cwd: str | None = None, plugin_dirs: list[str] | None = None):
        self.gateway = gateway
        self.cwd = cwd
        self.plugin_dirs = plugin_dirs or []

    def run(self, task: AgentTask) -> AgentResult:
        role = self.gateway.settings.role(task.role)
        if not self.gateway.allowed(role.provider, task.tags.data_class):
            raise PolicyViolation(f"{task.tags.data_class} data may not reach {role.provider!r}")
        return asyncio.run(self._run(task, role.model))

    async def _run(self, task: AgentTask, model: str) -> AgentResult:
        from claude_agent_sdk import ClaudeAgentOptions, ResultMessage, query  # lazy import

        lay = task.layers.layers()
        # Stable layers first so the SDK's automatic caching reuses them across steps.
        system_prompt = "\n\n".join(lay[k] for k in ("L2", "L3", "L4", "L5") if lay[k])
        options = ClaudeAgentOptions(
            system_prompt=system_prompt,
            model=model.split("/", 1)[-1],
            cwd=self.cwd,
            env={"CLAUDE_CODE_PROMPT_CACHE_TTL": "1h"},
        )
        text, result = "", None
        async for message in query(prompt=task.layers.tail, options=options):
            if isinstance(message, ResultMessage):
                result = message
                text = getattr(message, "result", "") or text
        usage = (getattr(result, "usage", None) or {}) if result else {}
        resp = ModelResponse(
            text=text,
            input_tokens=int(usage.get("input_tokens", 0)),
            cache_creation_input_tokens=int(usage.get("cache_creation_input_tokens", 0)),
            cache_read_input_tokens=int(usage.get("cache_read_input_tokens", 0)),
            output_tokens=int(usage.get("output_tokens", 0)),
        )
        rec = self.gateway.record_external(
            task.tags, runtime=self.name, model=model, layers=task.layers, response=resp
        )
        sdk_cost = getattr(result, "total_cost_usd", None) if result else None
        return AgentResult(
            text=text,
            tokens=rec.total_tokens,
            cost_usd=float(sdk_cost) if sdk_cost is not None else rec.cost_usd,
            request_ids=[resp.request_id],
        )

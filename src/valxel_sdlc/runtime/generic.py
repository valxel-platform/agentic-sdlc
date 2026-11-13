"""Generic runtime: one gateway call per task, any provider. In stub mode the gateway's
StubProvider returns the task's deterministic stub text."""

from __future__ import annotations

from ..gateway import ModelGateway
from .base import AgentResult, AgentTask


class GatewayRuntime:
    def __init__(self, gateway: ModelGateway, name: str = "generic", capabilities: frozenset[str] | None = None):
        self.gateway = gateway
        self.name = name
        self.capabilities = capabilities or frozenset({"text"})

    def run(self, task: AgentTask) -> AgentResult:
        res = self.gateway.complete(task.tags, task.layers, stub_text=task.stub_text, max_tokens=task.max_tokens,
                                     web_searches=task.web_searches)
        return AgentResult(
            text=res.text,
            tokens=res.total_tokens,
            cost_usd=res.cost_usd,
            request_ids=[res.response.request_id],
        )

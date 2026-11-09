"""AgentRuntime: the seam that keeps the design LLM-agnostic. Phase nodes call only this."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from ..gateway import CallTags, PromptLayers


@dataclass
class AgentTask:
    role: str
    phase: str
    tags: CallTags
    layers: PromptLayers
    stub_text: str | None = None  # deterministic output used in stub mode
    max_tokens: int = 4096
    # Caps for this session: at most this many turns and dollars (None: the role's caps).
    max_turns: int | None = None
    max_budget_usd: float | None = None
    # A JSON schema the reply must match. Claude Code enforces it (structured output); callers
    # validate every reply against it either way (agents/structured.py).
    schema: dict | None = None


@dataclass
class AgentResult:
    text: str
    tokens: int
    cost_usd: float
    request_ids: list[str] = field(default_factory=list)


class AgentRuntime(Protocol):
    name: str
    # Capability flags; the supervisor refuses to route a phase to a runtime missing one it needs.
    capabilities: frozenset[str]

    def run(self, task: AgentTask) -> AgentResult: ...

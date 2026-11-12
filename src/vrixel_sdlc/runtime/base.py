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
    # Set by the tracing wrapper (vrixel_sdlc.telemetry): {"env": ..., "codex_config": ...} for the session.
    telemetry: dict | None = None
    # The skills the phase gave this session (pasted into L2); anything else it loads is recorded.
    skills: tuple[str, ...] = ()
    # Web searches the session may make (0: none). Runtimes that can't search ignore it
    # (vrixel_sdlc.research.can_search says which can).
    web_searches: int = 0
    # A folder to work in, read-only: the session may read files there and change nothing
    # (vrixel_sdlc.workspace). Runtimes without folder access ignore it.
    cwd: str | None = None
    read_only: bool = False


@dataclass
class AgentResult:
    text: str
    tokens: int
    cost_usd: float
    request_ids: list[str] = field(default_factory=list)
    role: str = ""  # the role that did the work: the one asked for, or a fallback
    # Installed skills the session loaded by itself, beyond its phase's (vrixel_sdlc.plugins).
    extra_skills: tuple[str, ...] = ()


class AgentRuntime(Protocol):
    name: str
    # Capability flags; the supervisor refuses to route a phase to a runtime missing one it needs.
    capabilities: frozenset[str]

    def run(self, task: AgentTask) -> AgentResult: ...

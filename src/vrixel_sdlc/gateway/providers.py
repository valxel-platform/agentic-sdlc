"""Model providers behind the gateway.

StubProvider is deterministic and offline: it returns the text the caller supplies and
simulates prompt caching (exact-prefix match at breakpoints, TTL refreshed on every hit,
isolated per workspace and model) so the ledger shows realistic cache behavior.
AnthropicProvider calls the Claude API with the same layer breakpoints.
"""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Callable, Protocol

from .prompt import BREAKPOINTS, LAYER_NAMES, PromptLayers, estimate_tokens

TTL_SECONDS = {"5m": 300, "1h": 3600}


@dataclass
class ModelRequest:
    model: str  # "<provider>/<model-name>"
    layers: PromptLayers
    workspace: str
    ttl: str = "1h"
    max_tokens: int = 4096
    stub_text: str | None = None  # used only by StubProvider


@dataclass
class ModelResponse:
    text: str
    input_tokens: int
    cache_creation_input_tokens: int
    cache_read_input_tokens: int
    output_tokens: int
    request_id: str = field(default_factory=lambda: f"req_{uuid.uuid4().hex[:16]}")


class Provider(Protocol):
    name: str

    def complete(self, req: ModelRequest) -> ModelResponse: ...


class StubProvider:
    name = "stub"

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._cache: dict[tuple[str, str, str], float] = {}
        self._lock = threading.Lock()

    def complete(self, req: ModelRequest) -> ModelResponse:
        counts = req.layers.token_counts()
        hashes = req.layers.prefix_hashes()
        now = self._clock()
        ttl = TTL_SECONDS.get(req.ttl, 300)

        with self._lock:
            hit_at = None
            for bp in reversed(BREAKPOINTS):  # longest matching prefix first
                key = (req.workspace, req.model, hashes[bp])
                exp = self._cache.get(key)
                if exp is not None and exp > now:
                    hit_at = bp
                    break
            hit_idx = LAYER_NAMES.index(hit_at) if hit_at else -1
            read = sum(counts[n] for n in LAYER_NAMES[: hit_idx + 1])
            write = sum(counts[n] for n in LAYER_NAMES[hit_idx + 1 :])
            for bp in BREAKPOINTS:  # write new entries and refresh hit ones
                self._cache[(req.workspace, req.model, hashes[bp])] = now + ttl

        text = req.stub_text if req.stub_text is not None else ""
        return ModelResponse(
            text=text,
            input_tokens=counts["L6"],
            cache_creation_input_tokens=write,
            cache_read_input_tokens=read,
            output_tokens=estimate_tokens(text),
        )


class AnthropicProvider:
    """Claude API. Requires `pip install .[live]` and ANTHROPIC_API_KEY."""

    name = "anthropic"

    def __init__(self, client=None) -> None:
        if client is None:
            import anthropic  # lazy: only in live mode

            client = anthropic.Anthropic()
        self._client = client

    def complete(self, req: ModelRequest) -> ModelResponse:
        lay = req.layers.layers()
        cache = {"type": "ephemeral", "ttl": req.ttl}
        # L1 is sent as text here; a tool-using runtime passes real tool definitions instead.
        system = [
            {"type": "text", "text": lay["L1"] + "\n\n" + lay["L2"], "cache_control": cache},
            {"type": "text", "text": lay["L3"] + "\n\n" + lay["L4"], "cache_control": cache},
            {"type": "text", "text": lay["L5"] or "(no feature context yet)", "cache_control": cache},
        ]
        resp = self._client.messages.create(
            model=req.model.split("/", 1)[-1],
            max_tokens=req.max_tokens,
            system=system,
            messages=[{"role": "user", "content": req.layers.tail}],
        )
        text = "".join(getattr(b, "text", "") for b in resp.content)
        u = resp.usage
        return ModelResponse(
            text=text,
            input_tokens=u.input_tokens,
            cache_creation_input_tokens=getattr(u, "cache_creation_input_tokens", 0) or 0,
            cache_read_input_tokens=getattr(u, "cache_read_input_tokens", 0) or 0,
            output_tokens=u.output_tokens,
            request_id=getattr(resp, "id", "") or "",
        )

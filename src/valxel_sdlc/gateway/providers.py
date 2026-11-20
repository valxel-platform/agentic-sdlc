"""Model providers behind the gateway.

StubProvider is deterministic and offline: it returns the text the caller supplies and
simulates prompt caching (exact-prefix match at breakpoints, TTL refreshed on every hit,
isolated per workspace and model) so the ledger shows realistic cache behavior.
AnthropicProvider calls the Claude API with the same layer breakpoints. OpenAICompatibleProvider
calls any OpenAI-compatible chat API (OpenAI, a self-hosted vLLM, the products' LLM gateway).

A provider that is rate limited, overloaded or unreachable raises ProviderUnavailable, so the
caller can hand the task to the role's fallback.
"""

from __future__ import annotations

import threading
import os
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
    web_searches: int = 0  # the Claude API's web search tool, at most this many uses (0: off)
    # Pictures sent before the tail: [{"label", "media_type", "data" (bytes)}]. This call only.
    images: tuple = ()
    # Through the LLM gateway (valxel_sdlc.gateway_sessions): what the call is for, and the data
    # class whose rule the gateway holds it to.
    labels: dict[str, str] = field(default_factory=dict)
    data_class: str = ""


@dataclass
class ModelResponse:
    text: str
    input_tokens: int
    cache_creation_input_tokens: int
    cache_read_input_tokens: int
    output_tokens: int
    web_searches: int = 0  # searches the provider ran (billed per search)
    request_id: str = field(default_factory=lambda: f"req_{uuid.uuid4().hex[:16]}")
    # How full the context got: the largest prompt (input, cache writes and reads) of any one model
    # call in the session; for a single call, the prompt itself. 0: take the call's total input.
    context_tokens: int = 0
    # The agent session's id (Claude Code's session, Codex's thread), "" for a single call.
    session_id: str = ""
    # The model's context window, when the vendor reported it with the response (0: from config).
    context_window: int = 0
    # The model the provider says answered (its own name for it), "" when it didn't say. A change
    # here under the same config is a provider moving a model (valxel_sdlc.what_changed).
    served_model: str = ""


class ProviderUnavailable(RuntimeError):
    """The provider could not take the call right now (rate limited, overloaded, down, timed out).
    Not raised for a bad request or a refusal: those would fail the same way elsewhere."""


# Phrases that mark a transient provider failure in an error message (the Claude and Codex CLIs
# report through their exit status and text, not typed errors).
TRANSIENT_MARKERS = ("rate limit", "rate_limit", "overloaded", "429", "529", "502", "503", "504",
                     "timed out", "timeout", "connection error", "connection reset", "service unavailable")


def is_transient(message: str) -> bool:
    text = message.lower()
    return any(m in text for m in TRANSIENT_MARKERS)


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
            served_model=req.model.split("/", 1)[-1],  # a stand-in answers as what was asked for
        )


IMAGE_TYPES = ("image/png", "image/jpeg", "image/webp", "image/gif")


def _pictures(req: ModelRequest):
    import base64

    for im in req.images or ():
        if im.get("media_type") in IMAGE_TYPES and im.get("data"):
            yield str(im.get("label") or ""), im["media_type"], base64.b64encode(im["data"]).decode()


def _anthropic_content(req: ModelRequest):
    """The tail as text, or with the pictures before it, each after its label."""
    parts: list[dict] = []
    for label, media, b64 in _pictures(req):
        if label:
            parts.append({"type": "text", "text": label})
        parts.append({"type": "image", "source": {"type": "base64", "media_type": media, "data": b64}})
    return parts + [{"type": "text", "text": req.layers.tail}] if parts else req.layers.tail


def _openai_content(req: ModelRequest):
    parts: list[dict] = []
    for label, media, b64 in _pictures(req):
        if label:
            parts.append({"type": "text", "text": label})
        parts.append({"type": "image_url", "image_url": {"url": f"data:{media};base64,{b64}"}})
    return parts + [{"type": "text", "text": req.layers.tail}] if parts else req.layers.tail


class AnthropicProvider:
    """Claude API. Requires `pip install .[live]`, and the LLM gateway (VALXEL_LLM_GATEWAY_URL and
    VALXEL_LLM_GATEWAY_API_KEY) or ANTHROPIC_API_KEY.

    Through the gateway, each call carries its labels (product, feature, phase, agent, workspace) and
    declares its data class, and the gateway applies that class's rule: providers, redaction, audit.
    The tool's ledger, budgets and fallbacks are unchanged."""

    name = "anthropic"

    def __init__(self, client=None, routed: bool | None = None) -> None:
        from .. import gateway_sessions

        self.routed = gateway_sessions.configured() if routed is None else routed
        if client is None:
            import anthropic  # lazy: only in live mode

            from ..secret_ring import get

            if self.routed:
                client = anthropic.Anthropic(base_url=os.environ[gateway_sessions.URL_ENV].rstrip("/"),
                                             api_key=gateway_sessions.gateway_key())
            else:
                client = anthropic.Anthropic(**({"api_key": get("ANTHROPIC_API_KEY")} if get("ANTHROPIC_API_KEY") else {}))
        self._client = client

    def _headers(self, req: ModelRequest) -> dict:
        if not self.routed:
            return {}
        headers = {f"x-valxel-{k}": v for k, v in req.labels.items() if v}
        if req.data_class:
            headers["x-valxel-data-class"] = req.data_class
        return {"extra_headers": headers}

    def complete(self, req: ModelRequest) -> ModelResponse:
        lay = req.layers.layers()
        cache = {"type": "ephemeral", "ttl": req.ttl}
        # L1 is sent as text here; a tool-using runtime passes real tool definitions instead.
        system = [
            {"type": "text", "text": lay["L1"] + "\n\n" + lay["L2"], "cache_control": cache},
            {"type": "text", "text": lay["L3"] + "\n\n" + lay["L4"], "cache_control": cache},
            {"type": "text", "text": lay["L5"] or "(no feature context yet)", "cache_control": cache},
        ]
        try:
            resp = self._client.messages.create(
                model=req.model.split("/", 1)[-1],
                max_tokens=req.max_tokens,
                system=system,
                messages=[{"role": "user", "content": _anthropic_content(req)}],
                **({"tools": [{"type": "web_search_20250305", "name": "web_search", "max_uses": req.web_searches}]}
                   if req.web_searches else {}),
                **self._headers(req),
            )
        except Exception as exc:
            # The SDK's rate-limit, overload, connection and 5xx errors all carry a status or say so.
            status = getattr(exc, "status_code", None)
            if status in (429, 500, 502, 503, 504, 529) or type(exc).__name__ in (
                    "RateLimitError", "APIConnectionError", "APITimeoutError", "InternalServerError"):
                raise ProviderUnavailable(f"anthropic: {exc}") from exc
            raise
        text = "".join(getattr(b, "text", "") for b in resp.content)
        u = resp.usage
        return ModelResponse(
            text=text,
            input_tokens=u.input_tokens,
            cache_creation_input_tokens=getattr(u, "cache_creation_input_tokens", 0) or 0,
            cache_read_input_tokens=getattr(u, "cache_read_input_tokens", 0) or 0,
            output_tokens=u.output_tokens,
            web_searches=int(getattr(getattr(u, "server_tool_use", None), "web_search_requests", 0) or 0),
            request_id=getattr(resp, "id", "") or "",
            served_model=str(getattr(resp, "model", "") or ""),
        )


class OpenAICompatibleProvider:
    """Any OpenAI-compatible chat completions API. The layers go first in one system message, most
    stable first, so providers that cache prompt prefixes automatically (OpenAI, vLLM's prefix
    caching) reuse them across calls; the tail is the user message."""

    def __init__(self, name: str, base_url: str, api_key: str = "", client=None, timeout_s: float = 300.0) -> None:
        import httpx  # lazy: live mode only

        self.name = name
        self._url = base_url.rstrip("/") + "/chat/completions"
        self._headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self._client = client or httpx.Client(timeout=timeout_s)

    def complete(self, req: ModelRequest) -> ModelResponse:
        import httpx

        lay = req.layers.layers()
        system = "\n\n".join(lay[k] for k in ("L1", "L2", "L3", "L4", "L5") if lay[k])
        body = {"model": req.model.split("/", 1)[-1], "max_completion_tokens": req.max_tokens,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": _openai_content(req)}]}
        try:
            r = self._client.post(self._url, json=body, headers=self._headers)
        except httpx.TransportError as exc:  # connection refused, reset, timed out
            raise ProviderUnavailable(f"{self.name}: {exc}") from exc
        if r.status_code in (429, 500, 502, 503, 504):
            raise ProviderUnavailable(f"{self.name}: HTTP {r.status_code}: {r.text[:200]}")
        r.raise_for_status()
        data = r.json()
        text = "".join((c.get("message") or {}).get("content") or "" for c in data.get("choices") or [])
        u = data.get("usage") or {}
        cached = int((u.get("prompt_tokens_details") or {}).get("cached_tokens") or 0)
        return ModelResponse(
            text=text,
            input_tokens=max(0, int(u.get("prompt_tokens") or 0) - cached),
            cache_creation_input_tokens=0,  # these APIs don't charge for writing the cache
            cache_read_input_tokens=cached,
            output_tokens=int(u.get("completion_tokens") or 0),
            request_id=str(data.get("id") or ""),
            served_model=str(data.get("model") or ""),  # the resolved snapshot, for an alias
        )

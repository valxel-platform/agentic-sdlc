"""Short-lived model credentials for agent sessions, from the LLM gateway (ss-llm-gateway).

Without this, a Claude Code session gets the tool's own model key: it lands in the session's
environment and, with sandboxes on, in the spec sent to the sandbox. With VALXEL_LLM_GATEWAY_URL
and VALXEL_LLM_GATEWAY_API_KEY set, the tool instead mints a gateway token per session
(POST /v1/sessions) and the session talks to the gateway with it:

- the token expires shortly after the session's own time limit;
- it may spend no more than the session's cap (the same `max_budget_usd` Claude Code is given), and
  the gateway refuses the next call once it has;
- it may use only the session's model and the small model Claude Code uses for housekeeping;
- its labels (product, feature, phase, agent, workspace) are fixed, so spend lands where it belongs;
- it is minted for the session's data class, and a gateway set up for less refuses it;
- it is revoked as soon as the session ends, and what it spent is logged.

The tool's own key (VALXEL_LLM_GATEWAY_API_KEY) is a secret: read through the secret store, never
given to a session.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import httpx

log = logging.getLogger(__name__)

URL_ENV, KEY_ENV = "VALXEL_LLM_GATEWAY_URL", "VALXEL_LLM_GATEWAY_API_KEY"
# What a session must not fall back to when it has a token: every other way Claude Code finds a model.
CLEARED = ("ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX")


class GatewayUnavailable(RuntimeError):
    """The gateway couldn't mint a token: the session doesn't start."""


def gateway_key(env: Any = None) -> str:
    """The tool's own gateway key: a secret, never given to a session."""
    from .secret_ring import current

    return current(KEY_ENV, env)


_key = gateway_key


def configured(env: Any = None) -> bool:
    e = os.environ if env is None else env
    return bool(e.get(URL_ENV)) and bool(_key(env))


def labels_for(tags: Any) -> dict[str, str]:
    from .gateway.gateway import labels_for as of

    return of(tags)


def codex_blocked() -> str:
    """Why Codex can't run here, or "". In a sandbox it needs the gateway: no provider key goes into
    a sandbox, only a session token."""
    from .sandbox import remote

    if remote() and not configured():
        return ("sessions run in sandboxes (VALXEL_SESSION_EXECUTOR=kubernetes) and Codex would need a provider "
                "key there: set the LLM gateway (VALXEL_LLM_GATEWAY_URL) so it gets a session token instead")
    return ""


@contextmanager
def session_token(tags: Any, model: str, budget_usd: float, ttl_s: int, *,
                  http: Any = None, env: Any = None) -> Iterator[dict[str, str]]:
    """The environment a Claude Code session uses instead of the tool's key, for as long as the
    `with` block lasts; the token is revoked when it ends, however it ends."""
    e = os.environ if env is None else env
    extra = [m.strip() for m in str(e.get("VALXEL_LLM_GATEWAY_SESSION_MODELS", "claude-haiku-*")).split(",") if m.strip()]
    with mint(tags, [model, *[m for m in extra if m != model]], budget_usd, ttl_s, http=http, env=env) as (url, token):
        yield {"ANTHROPIC_BASE_URL": url, "ANTHROPIC_API_KEY": token, **dict.fromkeys(CLEARED, "")}


@contextmanager
def mint(tags: Any, models: list[str], budget_usd: float, ttl_s: int, *,
         http: Any = None, env: Any = None) -> Iterator[tuple[str, str]]:
    """(the gateway's URL, a session token) for as long as the `with` block lasts: the token may spend
    `budget_usd` on `models`, in the session's data class and under its labels, and is revoked when
    the block ends, however it ends. The gateway refuses the session's next call once the budget is
    spent: a live dollar cap, whatever the agent (Codex has none of its own)."""
    e = os.environ if env is None else env
    url = str(e.get(URL_ENV, "")).rstrip("/")
    headers = {"x-api-key": _key(env)}
    client = http or httpx.Client(timeout=30)
    body = {"budget_usd": round(max(float(budget_usd), 0.01), 4), "ttl_s": int(ttl_s), "data_class": tags.data_class,
            "models": list(dict.fromkeys(models)), "labels": labels_for(tags)}
    try:
        res = client.post(f"{url}/v1/sessions", json=body, headers=headers)
    except httpx.HTTPError as exc:
        raise GatewayUnavailable(f"the LLM gateway is unreachable: {type(exc).__name__}") from exc
    if res.status_code != 201:
        try:
            why = res.json()["error"]["message"]
        except (ValueError, KeyError, TypeError):
            why = f"HTTP {res.status_code}"
        raise GatewayUnavailable(f"the LLM gateway didn't mint a session token: {why}")
    sess = res.json()
    try:
        yield url, sess["token"]
    finally:
        try:
            done = client.delete(f"{url}/v1/sessions/{sess['id']}", headers=headers)
            seen = done.json() if done.status_code == 200 else {}
            log.info("gateway session %s for %s %s/%s ended: $%.4f in %s calls", sess["id"], tags.feature_id,
                     tags.phase, tags.role, float(seen.get("spent_usd") or 0), seen.get("calls", "?"))
        except (httpx.HTTPError, ValueError) as exc:  # it expires on its own
            log.warning("couldn't revoke gateway session %s (%s); it expires at %s", sess["id"],
                        type(exc).__name__, sess.get("expires_at"))
        if http is None:
            client.close()

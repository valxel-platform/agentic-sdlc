"""A small synchronous client for remote MCP servers (Streamable HTTP).

Graph nodes and the work-tracking sync run in worker threads, so each call opens a session,
calls one tool and closes it. That costs a round trip per call, which is fine at the volume an
SDLC pipeline generates (a handful of calls per gate), and keeps no connection state that could
go stale between human decisions that are days apart.

Authentication is a header whose value comes from the environment, e.g. a Lucid API key or an
OpenProject API token. Unattended agents cannot complete an OAuth consent screen, so every
server used by the pipeline needs a key or token issued to a service account.
"""

from __future__ import annotations

import json
from typing import Any, Protocol


class McpError(RuntimeError):
    """The server rejected a call, or a tool returned isError."""


class McpTools(Protocol):
    name: str

    def call(self, tool: str, args: dict) -> Any: ...
    def tool_names(self) -> set[str]: ...
    def tool_schema(self, tool: str) -> dict: ...


def _result_value(result) -> Any:
    """Prefer structured content; otherwise join the text parts and parse JSON when possible."""
    # SDK 2.x uses snake_case fields; 1.x uses the wire names.
    if getattr(result, "is_error", None) or getattr(result, "isError", None):
        text = " ".join(getattr(c, "text", "") for c in getattr(result, "content", []) or [])
        raise McpError(text.strip() or "tool returned an error")
    structured = getattr(result, "structured_content", None) or getattr(result, "structuredContent", None)
    if structured:
        # FastMCP-style servers wrap other return types as {"result": ...}.
        value = structured.get("result", structured) if set(structured) == {"result"} else structured
        return _maybe_json(value) if isinstance(value, str) else value
    parts = [getattr(c, "text", "") for c in getattr(result, "content", []) or [] if hasattr(c, "text")]
    if len(parts) > 1:  # some servers send one text part per list element
        values = [_maybe_json(p) for p in parts]
        if all(not isinstance(v, str) for v in values):
            return values
    return _maybe_json("".join(parts))


def _maybe_json(text: str) -> Any:
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return text


class McpHttp:
    """One remote MCP server. `target` is a URL, or an in-process server object in tests."""

    def __init__(self, name: str, target: Any, headers: dict[str, str] | None = None,
                 timeout_s: float = 60.0, auth=None) -> None:
        self.name = name
        self.target = target
        self.headers = dict(headers or {})
        self.auth = auth  # optional callable returning headers per call, e.g. a refreshed bearer token
        self.timeout_s = timeout_s
        self._schemas: dict[str, dict] | None = None

    async def _with_client(self, fn):
        try:  # MCP Python SDK 2.x
            from mcp.client.client import Client
            from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client
        except ImportError:
            return await self._with_session_v1(fn)
        if isinstance(self.target, str):
            transport = streamable_http_client(self.target, http_client=create_mcp_http_client(headers=self._headers()))
            client = Client(transport, read_timeout_seconds=self.timeout_s)
        else:
            client = Client(self.target, read_timeout_seconds=self.timeout_s)
        async with client as c:
            return await fn(c)

    async def _with_session_v1(self, fn):  # MCP Python SDK 1.x
        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client

        async with streamablehttp_client(self.target, headers=self._headers()) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                return await fn(session)

    def _headers(self) -> dict[str, str]:
        return {**self.headers, **(self.auth() if self.auth else {})}

    def _run(self, fn):
        import anyio

        try:
            return anyio.run(self._with_client, fn)
        except McpError:
            raise
        except Exception as exc:  # connection, auth and protocol failures
            raise McpError(f"{self.name}: {type(exc).__name__}: {exc}") from exc

    def call(self, tool: str, args: dict) -> Any:
        async def go(c):
            return await c.call_tool(tool, args)

        # Parse outside the session: an exception raised inside it surfaces as an ExceptionGroup.
        result = self._run(go)
        try:
            return _result_value(result)
        except McpError as exc:
            raise McpError(f"{self.name}.{tool}: {exc}") from exc

    def _load_tools(self) -> dict[str, dict]:
        if self._schemas is None:
            async def go(c):
                res = await c.list_tools()
                return {t.name: (getattr(t, "input_schema", None) or getattr(t, "inputSchema", None) or {})
                        for t in res.tools}

            self._schemas = self._run(go)
        return self._schemas

    def tool_names(self) -> set[str]:
        return set(self._load_tools())

    def tool_schema(self, tool: str) -> dict:
        return dict(self._load_tools().get(tool) or {})


def require_tools(server: McpTools, needed: set[str]) -> None:
    """Fail with a clear message when an admin has disabled a tool the adapter relies on."""
    missing = needed - server.tool_names()
    if missing:
        raise McpError(f"{server.name} does not offer {sorted(missing)}; ask its admin to enable them")


def pick_tool(server: McpTools, *candidates: str) -> str:
    """The first of several names a server may use for the same tool (servers rename tools
    between versions, and admins can rename them)."""
    names = server.tool_names()
    for c in candidates:
        if c in names:
            return c
    raise McpError(f"{server.name} offers none of {list(candidates)}; ask its admin to enable one")


def fit_args(server: McpTools, tool: str, wanted: dict[tuple[str, ...], Any]) -> dict:
    """Map logical arguments onto the property names the tool's input schema actually uses.
    `wanted` maps candidate names (first match wins) to a value; None values are dropped."""
    props = set((server.tool_schema(tool).get("properties") or {}).keys())
    out = {}
    for names, value in wanted.items():
        if value is None:
            continue
        name = next((n for n in names if n in props), None) if props else names[0]
        if name:
            out[name] = value
    return out


class ClientCredentials:
    """OAuth 2.0 client-credentials grant, cached until shortly before expiry. OpenChoreo's
    control plane issues these for its confidential service client."""

    def __init__(self, token_url: str, client_id: str, client_secret: str, scope: str = "",
                 opener=None, clock=None) -> None:
        import threading
        import time
        from urllib.request import build_opener

        self.token_url, self.client_id, self.client_secret, self.scope = token_url, client_id, client_secret, scope
        self.opener = opener or build_opener()
        self.clock = clock or time.time
        self._token: tuple[str, float] | None = None
        self._lock = threading.Lock()

    def __call__(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token()}"}

    def token(self) -> str:
        from urllib.parse import urlencode
        from urllib.request import Request

        with self._lock:
            if self._token and self._token[1] - 60 > self.clock():
                return self._token[0]
            form = {"grant_type": "client_credentials", "client_id": self.client_id,
                    "client_secret": self.client_secret}
            if self.scope:
                form["scope"] = self.scope
            req = Request(self.token_url, data=urlencode(form).encode(), method="POST",
                          headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"})
            try:
                with self.opener.open(req, timeout=15) as resp:
                    body = json.loads(resp.read().decode("utf-8"))
            except Exception as exc:
                raise McpError(f"token request to {self.token_url} failed: {exc}") from exc
            self._token = (body["access_token"], self.clock() + float(body.get("expires_in", 300)))
            return self._token[0]

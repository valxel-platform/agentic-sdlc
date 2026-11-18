"""Recorded HTTP for the live smoke tests (tests/smoke, tracker T-18).

The offline tests run the tool with stand-ins (StubGitHub, the stub model). The live clients, the
code that actually talks to GitHub, the LLM gateway and the model API, run only in a deployment.
The smoke tests run those clients as they are and swap only the wire: each test has a cassette,
the exchanges it had with the real service, and replays them.

- **Replay** (CI, every pull request): each request must be the next one the cassette holds, same
  method, URL and body. A client that now sends something else fails the test, which is the point:
  the service would have seen a different request.
- **Record** (`VALXEL_SMOKE_RECORD=1`, against the dev services): requests go to the real service and
  the exchanges are written back to the cassette.

Cassettes keep no secrets: request headers are dropped except the ones that say what a call is for
(`x-valxel-*`, `x-github-api-version`), and any body field named like a token,
key, secret or password is masked. They hold synthetic data only: the smoke repository and prompts
are the tests' own.
"""

from __future__ import annotations

import io
import json
import re
from email.message import Message
from pathlib import Path
from typing import Any, Self
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import BaseHandler, OpenerDirector, Request, build_opener

KEEP_HEADERS = re.compile(r"^(x-valxel-.*|x-github-api-version)$", re.IGNORECASE)
SECRET_KEY = re.compile(r"(token|secret|password|api[_-]?key|authorization)", re.IGNORECASE)
MASK = "<masked>"


class CassetteMismatch(AssertionError):
    """The client sent a request the cassette doesn't hold next."""


def scrub(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: (MASK if SECRET_KEY.search(str(k)) and isinstance(v, str) else scrub(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [scrub(v) for v in value]
    return value


def _body(raw: bytes | None) -> Any:
    if not raw:
        return None
    try:
        return scrub(json.loads(raw))
    except ValueError:
        return raw.decode("utf-8", "replace")


def _where(url: str) -> str:
    """The request's host, path and query: what a cassette matches on."""
    u = urlsplit(url)
    return f"{u.netloc}{u.path}" + (f"?{u.query}" if u.query else "")


class Cassette:
    def __init__(self, path: str | Path, *, record: bool = False) -> None:
        self.path, self.record = Path(path), record
        self.meta: dict[str, Any] = {}
        self.exchanges: list[dict[str, Any]] = []
        if not record:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self.meta, self.exchanges = data.get("_meta", {}), data["exchanges"]
        self._next = 0

    # -- replay
    def answer(self, method: str, url: str, headers: dict[str, str], raw: bytes | None) -> dict[str, Any]:
        if self._next >= len(self.exchanges):
            raise CassetteMismatch(f"{method} {_where(url)}: the cassette {self.path.name} has no more exchanges")
        want = self.exchanges[self._next]
        got = {"method": method, "url": _where(url), "body": _body(raw),
               "headers": {k.lower(): v for k, v in headers.items() if KEEP_HEADERS.match(k)}}
        for key in ("method", "url", "body"):
            if got[key] != want["request"].get(key):
                raise CassetteMismatch(f"exchange {self._next} of {self.path.name}: the {key} differs\n"
                                       f"  recorded: {json.dumps(want['request'].get(key))[:600]}\n"
                                       f"  sent:     {json.dumps(got[key])[:600]}")
        for k, v in (want["request"].get("headers") or {}).items():
            if got["headers"].get(k) != v:
                raise CassetteMismatch(f"exchange {self._next} of {self.path.name}: header {k} is "
                                       f"{got['headers'].get(k)!r}, recorded {v!r}")
        self._next += 1
        return want["response"]

    def done(self) -> None:
        """Every recorded exchange was asked for: a client that skips a call fails too."""
        if not self.record and self._next != len(self.exchanges):
            raise CassetteMismatch(f"{self.path.name}: {len(self.exchanges) - self._next} recorded exchanges were "
                                   f"never asked for (next: {self.exchanges[self._next]['request']['method']} "
                                   f"{self.exchanges[self._next]['request']['url']})")

    # -- record
    def keep(self, method: str, url: str, headers: dict[str, str], raw: bytes | None, status: int,
             body: bytes) -> None:
        self.exchanges.append({
            "request": {"method": method, "url": _where(url), "body": _body(raw),
                        "headers": {k.lower(): v for k, v in headers.items() if KEEP_HEADERS.match(k)}},
            "response": {"status": status, "body": _body(body)}})

    def save(self) -> None:
        if self.record:
            meta = dict(self.meta)
            self.path.write_text(json.dumps({"_meta": meta, "exchanges": self.exchanges}, indent=1) + "\n",
                                 encoding="utf-8")


def _payload(response: dict[str, Any]) -> bytes:
    b = response.get("body")
    return b"" if b is None else (b.encode() if isinstance(b, str) else json.dumps(b).encode())


# --------------------------------------------------------------------------- httpx (gateway, model API)

def httpx_transport(cassette: Cassette, real: Any = None, *, module: Any = None) -> Any:
    """A transport for an httpx client, or for `module` (httpx2, which newer Anthropic SDKs use)."""
    import httpx as default

    httpx = module or default

    class Transport(httpx.BaseTransport):
        def handle_request(self, request: httpx.Request) -> httpx.Response:
            raw = request.read()
            headers = dict(request.headers)
            if cassette.record:
                resp = (real or httpx.HTTPTransport()).handle_request(request)
                body = resp.read()
                cassette.keep(request.method, str(request.url), headers, raw, resp.status_code, body)
                return httpx.Response(resp.status_code, headers=resp.headers, content=body)
            r = cassette.answer(request.method, str(request.url), headers, raw)
            return httpx.Response(r["status"], content=_payload(r), headers={"content-type": "application/json"})

    return Transport()


# --------------------------------------------------------------------------- urllib (GitHub)

def urllib_opener(cassette: Cassette, real: OpenerDirector | None = None) -> OpenerDirector:
    real = real or build_opener()

    class Handler(BaseHandler):
        handler_order = 100

        def _open(self, req: Request):
            headers = dict(req.header_items())
            raw = req.data if isinstance(req.data, bytes) else None
            if cassette.record:
                try:
                    with real.open(req) as resp:
                        status, body = resp.status, resp.read()
                except HTTPError as exc:
                    status, body = exc.code, exc.read()
                cassette.keep(req.get_method(), req.full_url, headers, raw, status, body)
            else:
                r = cassette.answer(req.get_method(), req.full_url, headers, raw)
                status, body = r["status"], _payload(r)
            if status >= 400:
                raise HTTPError(req.full_url, status, "recorded", Message(), io.BytesIO(body))
            return _Response(body, status, req.full_url)

        https_open = _open
        http_open = _open

    opener = OpenerDirector()
    opener.add_handler(Handler())
    return opener


class _Response(io.BytesIO):
    def __init__(self, body: bytes, status: int, url: str) -> None:
        super().__init__(body)
        self.status, self.code, self.url = status, status, url

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

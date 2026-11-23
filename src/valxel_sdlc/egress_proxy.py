"""The way out for agent sessions on one machine (T-68): an HTTPS proxy that lets them reach only the
hosts on its list (`valxel egress-proxy`).

Session containers sit on an internal Docker network with no route out. This proxy, on that network
and the outside one, accepts CONNECT to the hosts allowed, the way the cluster's network policy
allows sessions only GitHub, the package registries and the model gateway. Plain-HTTP requests
are refused: everything a session needs is HTTPS.

The hosts are config/egress.yaml's (sandbox.egress), the same list as the shell sandbox's and the
cluster's network policy; VALXEL_EGRESS_ALLOW adds a deployment's own (comma-separated;
'*.example.com' allows its subdomains).

Refusals are kept per session for the audit trail (T-115): the launcher gives each session a proxy
URL naming it (http://<session>@egress-proxy:3128, sent as Proxy-Authorization), and when the
session ends asks `GET /refusals/<session>` with the launcher's token, which hands them over once.
A session could name another in its proxy URL; that only misfiles its own refusals.

**Git through the broker** (valxel_sdlc.git_broker). With VALXEL_GIT_BROKER_KEY set, the proxy also
answers plain-HTTP git requests at /git/<owner>/<repo>.git/...: it opens the session's pass, checks
the repository and the operation (push only to the session's own repository), puts the GitHub
token on the request and sends it to github.com. Sessions then hold no GitHub token, and github.com
leaves the CONNECT list (VALXEL_EGRESS_GITHUB_DIRECT=true keeps it, for a deployment that needs
it). Each brokered request is logged: session, repository, operation and bytes each way.
"""

from __future__ import annotations

import asyncio
import base64
import hmac
import json
import logging
import os
import time
from collections import OrderedDict
from pathlib import Path

log = logging.getLogger(__name__)

# Reached only through the git broker when it's on: a tunnel to it would let a session push anywhere.
BROKERED = ("github.com",)


def allow_list(env=None, path: str | None = None) -> tuple[str, ...]:
    """config/egress.yaml, the list the shell sandbox and the cluster's network policy use too
    (sandbox.egress), plus VALXEL_EGRESS_ALLOW for a deployment's own hosts. With the git broker on,
    github.com itself is left out (git goes through the broker); its download hosts stay."""
    from .git_broker import key
    from .sandbox import egress

    e = env if env is not None else os.environ
    extra = tuple(h.strip().lower() for h in (e.get("VALXEL_EGRESS_ALLOW") or "").split(",") if h.strip())
    hosts = tuple(egress(path)) + extra
    if key(e) and (e.get("VALXEL_EGRESS_GITHUB_DIRECT") or "").lower() != "true":
        hosts = tuple(h for h in hosts if h not in BROKERED)
    return hosts


def allowed(host: str, port: int, allow: tuple[str, ...]) -> bool:
    """HTTPS (443) to a listed host, or a subdomain of a '*.' entry."""
    if port != 443:
        return False
    h = host.lower().rstrip(".")
    for a in allow:
        if a.startswith("*."):
            if h.endswith(a[1:]) and h != a[2:]:
                return True
        elif h == a:
            return True
    return False


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> int:
    n = 0
    try:
        while data := await reader.read(65536):
            n += len(data)
            writer.write(data)
            await writer.drain()
    except (ConnectionError, asyncio.IncompleteReadError):
        pass
    finally:
        writer.close()
    return n


class Refusals:
    """Refused CONNECTs per session, bounded: the last PER of each of the last SESSIONS sessions."""

    PER, SESSIONS = 200, 1000

    def __init__(self) -> None:
        self.by: OrderedDict[str, list[dict]] = OrderedDict()

    def add(self, session: str, host: str, port: int) -> None:
        got = self.by.pop(session, [])
        got = (got + [{"at": int(time.time()), "host": host[:255], "port": port}])[-self.PER:]
        self.by[session] = got
        while len(self.by) > self.SESSIONS:
            self.by.popitem(last=False)

    def take(self, session: str) -> list[dict]:
        return self.by.pop(session, [])


def session_of(head: bytes) -> str:
    """The session a request names in its Proxy-Authorization (the proxy URL's user), or ''."""
    for raw in head.split(b"\r\n")[1:]:
        name, _, value = raw.decode("latin-1").partition(":")
        if name.strip().lower() == "proxy-authorization":
            kind, _, cred = value.strip().partition(" ")
            if kind.lower() == "basic":
                try:
                    user = base64.b64decode(cred).decode().split(":", 1)[0]
                except (ValueError, UnicodeDecodeError):
                    return ""
                return user if user.replace("-", "").isalnum() and len(user) <= 64 else ""
    return ""


def _bearer(head: bytes) -> str:
    for raw in head.split(b"\r\n")[1:]:
        name, _, value = raw.decode("latin-1").partition(":")
        if name.strip().lower() == "authorization" and value.strip().lower().startswith("bearer "):
            return value.strip()[7:]
    return ""


def _basic_password(head: bytes) -> str:
    """The password of a request's Authorization: Basic, or ''."""
    for raw in head.split(b"\r\n")[1:]:
        name, _, value = raw.decode("latin-1").partition(":")
        if name.strip().lower() == "authorization":
            kind, _, cred = value.strip().partition(" ")
            if kind.lower() == "basic":
                try:
                    return base64.b64decode(cred).decode().split(":", 1)[1]
                except (ValueError, UnicodeDecodeError, IndexError):
                    return ""
    return ""


# Not passed upstream: replaced (Host, Authorization), hop-by-hop, or the session's proxy naming.
_DROP = {"host", "authorization", "proxy-authorization", "proxy-connection", "connection", "keep-alive"}


def upstream_head(method: str, path: str, head: bytes, token: str, host: str = "github.com") -> bytes:
    """The request as GitHub gets it: the client's headers, less the dropped ones, with the real
    token and one request per connection."""
    lines = [f"{method} {path} HTTP/1.1", f"Host: {host}"]
    for raw in head.split(b"\r\n")[1:]:
        if not raw:
            continue
        name, _, _value = raw.decode("latin-1").partition(":")
        if name.strip().lower() not in _DROP:
            lines.append(raw.decode("latin-1"))
    cred = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    lines += [f"Authorization: Basic {cred}", "Connection: close"]
    return ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1")


async def _github(host: str = "github.com", port: int = 443):
    import ssl

    return await asyncio.open_connection(host, port, ssl=ssl.create_default_context(), server_hostname=host)


async def _reply(writer: asyncio.StreamWriter, status: bytes, extra: bytes = b"") -> None:
    writer.write(b"HTTP/1.1 " + status + b"\r\n" + extra + b"Content-Length: 0\r\nConnection: close\r\n\r\n")
    await writer.drain()
    writer.close()


async def broker(reader, writer, method: str, target: str, head: bytes, key: bytes, refusals: Refusals,
                 upstream=_github) -> None:
    """One git request through the broker (valxel_sdlc.git_broker): the pass checked, the token
    put on, the request sent to GitHub and its answer streamed back."""
    from .git_broker import open_pass, parse_target

    parsed = parse_target(target)
    if not parsed or method not in ("GET", "POST"):
        await _reply(writer, b"404 Not Found")
        return
    repo, op, path = parsed
    grant = open_pass(_basic_password(head), key)
    if grant is None:  # no pass yet: git asks its askpass for one, then retries
        await _reply(writer, b"401 Unauthorized", b'WWW-Authenticate: Basic realm="valxel git"\r\n')
        return
    if not (grant.may_write(repo) if op == "write" else grant.may_read(repo)):
        log.info("git refused: %s %s (session %s)", op, repo, grant.session or "unnamed")
        if grant.session:
            refusals.add(grant.session, f"github.com/{repo} ({op})", 443)
        await _reply(writer, b"403 Forbidden")
        return
    try:
        up_r, up_w = await asyncio.wait_for(upstream(), timeout=30)
    except (TimeoutError, OSError):
        await _reply(writer, b"502 Bad Gateway")
        return
    up_w.write(upstream_head(method, path, head, grant.token(repo)))
    await up_w.drain()
    sent, got = await asyncio.gather(_pipe(reader, up_w), _pipe(up_r, writer))
    log.info("git %s %s: session %s, %d bytes up, %d down", op, repo, grant.session or "unnamed", sent, got)


def admin_token(env=None) -> str:
    e = env if env is not None else os.environ
    f = e.get("VALXEL_SESSION_LAUNCHER_TOKEN_FILE", "")
    return Path(f).read_text(encoding="utf-8").strip() if f else e.get("VALXEL_SESSION_LAUNCHER_TOKEN", "")


def make_handler(allow: tuple[str, ...], *, port_ok=None, connect=asyncio.open_connection,
                 refusals: Refusals | None = None, token: str = "", broker_key: bytes = b"", upstream=_github):
    """port_ok: for tests, a port other than 443 that counts as allowed. token: the launcher's, for
    GET /refusals/<session> (no token: refusals are only logged). broker_key: the git broker's
    (git_broker.key); without it /git/ isn't served. upstream: for tests, GitHub's stand-in."""
    refusals = refusals if refusals is not None else Refusals()

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=30)
        except (TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError):
            writer.close()
            return
        line = head.split(b"\r\n", 1)[0].decode("latin-1")
        parts = line.split()
        if len(parts) == 3 and parts[0].upper() == "GET" and parts[1].startswith("/refusals/"):
            ok = bool(token) and hmac.compare_digest(_bearer(head).encode(), token.encode())
            body = json.dumps(refusals.take(parts[1][len("/refusals/"):]) if ok else []).encode()
            status = b"200 OK" if ok else b"401 Unauthorized"
            writer.write(b"HTTP/1.1 " + status + b"\r\nContent-Type: application/json\r\nContent-Length: "
                         + str(len(body)).encode() + b"\r\nConnection: close\r\n\r\n" + body)
            await writer.drain()
            writer.close()
            return
        if broker_key and len(parts) == 3 and parts[0].upper() != "CONNECT" and "/git/" in parts[1]:
            await broker(reader, writer, parts[0].upper(), parts[1], head, broker_key, refusals, upstream)
            return
        if len(parts) != 3 or parts[0].upper() != "CONNECT" or ":" not in parts[1]:
            writer.write(b"HTTP/1.1 405 Method Not Allowed\r\nContent-Length: 0\r\n\r\n")
            await writer.drain()
            writer.close()
            return
        host, _, port_s = parts[1].rpartition(":")
        port = int(port_s) if port_s.isdigit() else 0
        if not (allowed(host, port, allow) or (port_ok and port == port_ok and allowed(host, 443, allow))):
            sid = session_of(head)
            log.info("egress refused: %s:%s (session %s)", host, port, sid or "unnamed")
            if sid:
                refusals.add(sid, host, port)
            writer.write(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
            await writer.drain()
            writer.close()
            return
        try:
            up_r, up_w = await asyncio.wait_for(connect(host, port), timeout=30)
        except (TimeoutError, OSError):
            writer.write(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n")
            await writer.drain()
            writer.close()
            return
        writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        await writer.drain()
        await asyncio.gather(_pipe(reader, up_w), _pipe(up_r, writer))

    return handle


def serve(port: int = 3128) -> None:
    from .git_broker import key

    allow, k = allow_list(), key()

    async def main() -> None:
        server = await asyncio.start_server(make_handler(allow, token=admin_token(), broker_key=k), "0.0.0.0", port)
        log.info("egress proxy on %s, %d hosts allowed, git broker %s", port, len(allow), "on" if k else "off")
        async with server:
            await server.serve_forever()

    asyncio.run(main())

"""The way out for agent sessions on one machine (T-68): an HTTPS proxy that lets them reach only the
hosts on its list (`valxel egress-proxy`).

Session containers sit on an internal Docker network with no route out. This proxy, on that network
and the outside one, accepts CONNECT to the hosts allowed, the way the cluster's network policy
allows sessions only GitHub, the package registries and the model gateway. Plain-HTTP requests
are refused: everything a session needs is HTTPS.

VALXEL_EGRESS_ALLOW adds hosts (comma-separated; '*.example.com' allows its subdomains).
"""

from __future__ import annotations

import asyncio
import logging
import os

log = logging.getLogger(__name__)

# GitHub, and the registries a coder installs packages from.
DEFAULT_ALLOW = (
    "github.com", "api.github.com", "codeload.github.com", "*.githubusercontent.com", "ghcr.io",
    "pypi.org", "files.pythonhosted.org", "registry.npmjs.org", "registry.yarnpkg.com",
    "repo.maven.apache.org", "repo1.maven.org", "plugins.gradle.org", "services.gradle.org",
    "proxy.golang.org", "sum.golang.org", "crates.io", "static.crates.io", "index.crates.io",
    "rubygems.org", "api.nuget.org",
)


def allow_list(env=None) -> tuple[str, ...]:
    e = env if env is not None else os.environ
    extra = tuple(h.strip().lower() for h in (e.get("VALXEL_EGRESS_ALLOW") or "").split(",") if h.strip())
    return DEFAULT_ALLOW + extra


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


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
    except (ConnectionError, asyncio.IncompleteReadError):
        pass
    finally:
        writer.close()


def make_handler(allow: tuple[str, ...], *, port_ok=None, connect=asyncio.open_connection):
    """port_ok: for tests, a port other than 443 that counts as allowed."""

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=30)
        except (TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError):
            writer.close()
            return
        line = head.split(b"\r\n", 1)[0].decode("latin-1")
        parts = line.split()
        if len(parts) != 3 or parts[0].upper() != "CONNECT" or ":" not in parts[1]:
            writer.write(b"HTTP/1.1 405 Method Not Allowed\r\nContent-Length: 0\r\n\r\n")
            await writer.drain()
            writer.close()
            return
        host, _, port_s = parts[1].rpartition(":")
        port = int(port_s) if port_s.isdigit() else 0
        if not (allowed(host, port, allow) or (port_ok and port == port_ok and allowed(host, 443, allow))):
            log.info("egress refused: %s:%s", host, port)
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
    allow = allow_list()

    async def main() -> None:
        server = await asyncio.start_server(make_handler(allow), "0.0.0.0", port)
        log.info("egress proxy on %s, %d hosts allowed", port, len(allow))
        async with server:
            await server.serve_forever()

    asyncio.run(main())

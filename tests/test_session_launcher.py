"""A container per agent session on one machine (T-68): the launcher alone talks to Docker, starts a
locked-down container from its own image, passes the session through and removes it; the egress
proxy lets sessions out only to the hosts on its list."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from valxel_sdlc import egress_proxy as ep
from valxel_sdlc import session_launcher as sl

ENV = {"VALXEL_SESSION_IMAGE": "ghcr.io/valxel-platform/agentic-sdlc@sha256:abc", "VALXEL_SESSION_LAUNCHER_TOKEN": "t0k",
       "VALXEL_SESSION_MAX": "1", "VALXEL_SESSION_READY_S": "5"}


class FakeDocker:
    def __init__(self, runtimes=("runc", "runsc")):
        self.calls, self.bodies, self.rt = [], [], set(runtimes)
        self.live: dict[str, dict] = {}

    def runtimes(self):
        return self.rt

    def create(self, name, body):
        self.calls.append(("create", name))
        self.bodies.append(body)
        cid = f"c{len(self.bodies)}"
        self.live[cid] = {"Id": cid, "Labels": body["Labels"]}
        return cid

    def start(self, cid):
        self.calls.append(("start", cid))

    def inspect(self, cid):
        return {"NetworkSettings": {"Networks": {"valxel-sessions": {"IPAddress": "10.9.0.5"}}}}

    def remove(self, cid):
        self.calls.append(("remove", cid))
        self.live.pop(cid, None)

    def sessions(self):
        return list(self.live.values())


def runner(status=200, ready_after=0):
    seen = {"health": 0, "spec": None}

    def handle(req: httpx.Request) -> httpx.Response:
        assert req.url.host == "10.9.0.5" and req.url.port == 8081
        if req.url.path == "/healthz":
            seen["health"] += 1
            return httpx.Response(200 if seen["health"] > ready_after else 503, json={"ok": True})
        seen["spec"] = json.loads(req.content)
        return httpx.Response(status, json={"ok": True, "reply": "done", "session_id": "s1"})

    return httpx.Client(transport=httpx.MockTransport(handle)), seen


def launcher(docker=None, http=None, env=None):
    return sl.Launcher(sl.Settings({**ENV, **(env or {})}), docker=docker or FakeDocker(), http=http,
                       sleep=lambda s: None)


def test_a_session_runs_in_a_locked_down_container_that_is_then_removed():
    d = FakeDocker()
    http, seen = runner(ready_after=2)
    out = launcher(d, http).run({"id": "abc123", "prompt": "build it", "timeout_s": 60})
    assert out == {"ok": True, "reply": "done", "session_id": "s1"} and seen["spec"]["prompt"] == "build it"
    assert [c[0] for c in d.calls] == ["create", "start", "remove"] and d.calls[0][1] == "valxel-session-abc123"
    [body] = d.bodies
    h = body["HostConfig"]
    assert body["Image"] == ENV["VALXEL_SESSION_IMAGE"]  # its own image, never one the spec names
    assert body["Cmd"][:2] == ["valxel", "session-runner"] and body["User"] == "10001:10001"
    assert h["Runtime"] == "runsc" and h["ReadonlyRootfs"] and h["CapDrop"] == ["ALL"]
    assert h["SecurityOpt"] == ["no-new-privileges"] and h["NetworkMode"] == "valxel-sessions"
    assert h["Memory"] == 4 << 30 and h["PidsLimit"] == 1024 and "uid=10001" in h["Tmpfs"]["/work"]
    assert "HTTPS_PROXY=http://egress-proxy:3128" in body["Env"]


def test_gvisor_is_used_when_docker_has_it_and_required_when_asked():
    assert launcher(FakeDocker(runtimes=("runc",))).runtime() == "runc"
    with pytest.raises(sl.Unavailable, match="no runsc runtime"):
        launcher(FakeDocker(runtimes=("runc",)), env={"VALXEL_SESSION_RUNTIME": "runsc"}).runtime()
    body = sl.container_body(sl.Settings(ENV), "runc", "x")
    assert "Runtime" not in body["HostConfig"]


def test_a_failed_session_still_removes_its_container():
    d = FakeDocker()
    http, _ = runner(status=500)
    with pytest.raises(sl.Unavailable, match="answered 500"):
        launcher(d, http).run({"id": "x"})
    assert d.calls[-1][0] == "remove" and not d.live


def test_a_container_that_never_gets_ready_is_removed():
    d = FakeDocker()
    http, _ = runner(ready_after=10**6)
    clock = iter(range(0, 1000, 2))
    lch = sl.Launcher(sl.Settings(ENV), docker=d, http=http, sleep=lambda s: None, clock=lambda: next(clock))
    with pytest.raises(sl.Unavailable, match="wasn't ready in time"):
        lch.run({"id": "x"})
    assert not d.live


def test_the_sweep_removes_leftovers_only():
    d = FakeDocker()
    d.live = {"old": {"Id": "old", "Labels": {"valxel.io/started": "1000"}},
              "new": {"Id": "new", "Labels": {"valxel.io/started": "99000"}}}
    assert launcher(d).sweep(older_than_s=3600, now=100000) == ["old"] and list(d.live) == ["new"]


def test_only_the_worker_with_the_token_starts_sessions():
    http, _ = runner()
    app = sl.make_app(launcher(http=http))
    c = TestClient(app)
    assert c.post("/sessions", json={"id": "a"}).status_code == 401
    assert c.post("/sessions", json={"id": "a"}, headers={"Authorization": "Bearer nope"}).status_code == 401
    r = c.post("/sessions", json={"id": "a"}, headers={"Authorization": "Bearer t0k"})
    assert r.status_code == 200 and r.json()["reply"] == "done"


def test_the_worker_hands_the_session_to_the_launcher():
    from valxel_sdlc.session_runner import Unavailable

    def handle(req):
        assert req.headers["Authorization"] == "Bearer t0k" and req.url.path == "/sessions"
        return httpx.Response(200, json={"ok": True, "reply": "hi"})

    ex = sl.DockerExecutor(url="http://session-launcher:8083", token="t0k",
                           http=httpx.Client(transport=httpx.MockTransport(handle)))
    assert ex.run({"id": "a"})["reply"] == "hi"
    busy = sl.DockerExecutor(url="http://x", token="t", http=httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(503, text="all 1 session slots were busy"))))
    with pytest.raises(Unavailable, match="503"):
        busy.run({"id": "a"})
    with pytest.raises(RuntimeError, match="no launcher token"):
        sl.executor_from_env({"VALXEL_SESSION_EXECUTOR": "docker"})


def test_sessions_get_out_only_to_listed_hosts():
    allow = ep.allow_list({"VALXEL_EGRESS_ALLOW": "artifacts.acme.com"})
    assert ep.allowed("github.com", 443, allow) and ep.allowed("objects.githubusercontent.com", 443, allow)
    assert ep.allowed("artifacts.acme.com", 443, allow)
    assert not ep.allowed("githubusercontent.com", 443, allow)  # '*.' means subdomains
    assert not ep.allowed("github.com", 80, allow) and not ep.allowed("evil.example", 443, allow)


def test_the_proxy_tunnels_allowed_hosts_and_refuses_the_rest():
    async def scenario():
        async def echo(r, w):
            w.write(await r.read(5))
            await w.drain()
            w.close()

        up = await asyncio.start_server(echo, "127.0.0.1", 0)
        up_port = up.sockets[0].getsockname()[1]
        proxy = await asyncio.start_server(ep.make_handler(("localhost",), port_ok=up_port), "127.0.0.1", 0)
        port = proxy.sockets[0].getsockname()[1]

        async def ask(target):
            r, w = await asyncio.open_connection("127.0.0.1", port)
            w.write(f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n\r\n".encode())
            await w.drain()
            head = await r.readuntil(b"\r\n\r\n")
            body = b""
            if b" 200 " in head:
                w.write(b"hello")
                await w.drain()
                body = await r.read(5)
            w.close()
            return head.split(b"\r\n")[0], body

        ok = await ask(f"localhost:{up_port}")
        no = await ask("evil.example:443")
        r, w = await asyncio.open_connection("127.0.0.1", port)
        w.write(b"GET http://github.com/ HTTP/1.1\r\n\r\n")
        await w.drain()
        plain = (await r.readuntil(b"\r\n\r\n")).split(b"\r\n")[0]
        w.close()
        proxy.close()
        up.close()
        return ok, no, plain

    ok, no, plain = asyncio.run(scenario())
    assert ok == (b"HTTP/1.1 200 Connection Established", b"hello")
    assert no[0] == b"HTTP/1.1 403 Forbidden" and plain == b"HTTP/1.1 405 Method Not Allowed"

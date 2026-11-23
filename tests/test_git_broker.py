"""Git for agent sessions through the egress proxy's broker (valxel_sdlc.git_broker): sessions hold a
pass, never a GitHub token; the proxy pushes only to the session's own repository and reads only
the repositories the session was given."""

from __future__ import annotations

import asyncio
import base64
import secrets
from pathlib import Path

import pytest

from valxel_sdlc import egress_proxy as ep
from valxel_sdlc import git_broker as gb
from valxel_sdlc import sandbox, session_runner

KEY = secrets.token_bytes(32)
ENV = {"VALXEL_GIT_BROKER_URL": "http://egress-proxy:3128", "VALXEL_GIT_BROKER_KEY": KEY.hex()}


def _grant(**kw):
    base = {"session": "s1", "tokens": {"acme/api": "ghs_write", "acme/lib": "ghs_read"}, "write": "acme/api",
            "exp": 2_000_000_000}
    return gb.Grant(**{**base, **kw})


def test_a_pass_opens_only_with_its_key_unaltered_and_unexpired():
    p = gb.seal(_grant(), KEY)
    assert p.startswith(gb.PREFIX) and "ghs_" not in p  # the tokens are in it, encrypted
    g = gb.open_pass(p, KEY, now=1_000_000_000)
    assert g.may_write("acme/api") and g.may_write("ACME/API") and not g.may_write("acme/lib")
    assert g.may_read("acme/lib") and not g.may_read("evil/exfil") and g.token("acme/api") == "ghs_write"
    assert gb.open_pass(p, secrets.token_bytes(32), now=1_000_000_000) is None  # another key
    assert gb.open_pass(p[:-4] + ("AAAA" if not p.endswith("AAAA") else "BBBB"), KEY, now=1_000_000_000) is None
    assert gb.open_pass(p, KEY, now=2_000_000_001) is None  # expired
    assert gb.open_pass("ghs_plain_token", KEY) is None


def test_the_key_must_be_32_bytes_and_the_url_an_address():
    with pytest.raises(RuntimeError, match="64 hex"):
        gb.key({"VALXEL_GIT_BROKER_KEY": "abc"})
    with pytest.raises(RuntimeError, match="http"):
        gb.url({"VALXEL_GIT_BROKER_URL": "egress-proxy:3128"})
    assert not gb.enabled({}) and not gb.enabled({"VALXEL_GIT_BROKER_URL": "http://egress-proxy:3128"})
    assert gb.enabled(ENV)


def test_the_key_can_come_from_a_file(tmp_path):
    f = tmp_path / "k"
    f.write_text(KEY.hex() + "\n")
    assert gb.key({"VALXEL_GIT_BROKER_KEY_FILE": str(f)}) == KEY


def test_the_spec_leaves_the_tool_with_a_pass_not_tokens():
    spec = {"id": "s9", "timeout_s": 600,
            "checkout": {"repo": "acme/api", "branch": "feature/F-1", "token": "ghs_write"},
            "read_checkouts": [{"repo": "acme/lib", "sha": "a" * 40, "token": "ghs_read"}]}
    out = gb.broker_spec(spec, ENV, now=1_000)
    assert "ghs_" not in repr(out)
    co, rc = out["checkout"], out["read_checkouts"][0]
    assert co["via"] == rc["via"] == "http://egress-proxy:3128" and co["token"] == rc["token"]
    g = gb.open_pass(co["token"], KEY, now=1_000)
    assert g.session == "s9" and g.write == "acme/api" and g.token("acme/lib") == "ghs_read"
    assert g.exp == 1_000 + 600 + gb.MARGIN_S
    assert gb.broker_spec(spec, {}) is spec  # broker off: unchanged
    assert gb.broker_spec({"id": "x"}, ENV) == {"id": "x"}  # nothing cloned: nothing to broker


def test_only_git_endpoints_are_brokered_and_push_is_a_write():
    assert gb.parse_target("/git/acme/api.git/info/refs?service=git-upload-pack") == (
        "acme/api", "read", "/acme/api.git/info/refs?service=git-upload-pack")
    assert gb.parse_target("/git/acme/api.git/info/refs?service=git-receive-pack")[1] == "write"
    assert gb.parse_target("/git/acme/api.git/git-receive-pack")[1] == "write"
    assert gb.parse_target("/git/acme/api.git/git-upload-pack")[1] == "read"
    assert gb.parse_target("http://egress-proxy:3128/git/acme/api.git/git-upload-pack")[0] == "acme/api"
    for bad in ("/git/acme/api.git/info/refs", "/git/acme/api.git/objects/info/packs",
                "/git/acme/../x.git/git-upload-pack", "/api/v3/repos/acme/api",
                "/git/acme/api.git/info/refs?service=other"):
        assert gb.parse_target(bad) is None, bad


def test_with_the_broker_on_github_itself_is_not_tunnelled():
    on = ep.allow_list({"VALXEL_GIT_BROKER_KEY": KEY.hex()})
    assert not ep.allowed("github.com", 443, on)
    assert ep.allowed("codeload.github.com", 443, on) and ep.allowed("registry.npmjs.org", 443, on)
    direct = ep.allow_list({"VALXEL_GIT_BROKER_KEY": KEY.hex(), "VALXEL_EGRESS_GITHUB_DIRECT": "true"})
    assert ep.allowed("github.com", 443, direct) and ep.allowed("github.com", 443, ep.allow_list({}))


def _basic(password):
    return base64.b64encode(f"x:{password}".encode()).decode()


def test_the_proxy_pushes_only_to_the_sessions_repo_with_the_real_token():
    p = gb.seal(_grant(exp=4_000_000_000), KEY)

    async def scenario():
        seen: list[bytes] = []

        async def github(r, w):  # GitHub's stand-in: records the request, answers, closes
            seen.append(await r.readuntil(b"\r\n\r\n"))
            body = await r.read(5)
            w.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok" if body == b"PACK!" else
                    b"HTTP/1.1 400 Bad\r\nContent-Length: 0\r\n\r\n")
            await w.drain()
            w.close()

        up = await asyncio.start_server(github, "127.0.0.1", 0)
        up_port = up.sockets[0].getsockname()[1]
        refusals = ep.Refusals()
        handler = ep.make_handler((), broker_key=KEY, refusals=refusals,
                                  upstream=lambda: asyncio.open_connection("127.0.0.1", up_port))
        proxy = await asyncio.start_server(handler, "127.0.0.1", 0)
        port = proxy.sockets[0].getsockname()[1]

        async def ask(path, auth=None, body=b""):
            r, w = await asyncio.open_connection("127.0.0.1", port)
            h = f"POST {path} HTTP/1.1\r\nHost: egress-proxy:3128\r\nContent-Length: {len(body)}\r\n"
            if auth:
                h += f"Authorization: Basic {_basic(auth)}\r\n"
            w.write(h.encode() + b"\r\n" + body)
            await w.drain()
            out = await r.read()
            w.close()
            return out

        own = await ask("/git/acme/api.git/git-receive-pack", p, b"PACK!")
        other = await ask("/git/evil/exfil.git/git-receive-pack", p, b"PACK!")
        read_only = await ask("/git/acme/lib.git/git-receive-pack", p, b"PACK!")
        anon = await ask("/git/acme/api.git/git-receive-pack", None, b"PACK!")
        forged = await ask("/git/acme/api.git/git-receive-pack", "ghs_guess", b"PACK!")
        proxy.close()
        up.close()
        return own, other, read_only, anon, forged, seen, refusals

    own, other, read_only, anon, forged, seen, refusals = asyncio.run(scenario())
    assert own.startswith(b"HTTP/1.1 200 OK") and own.endswith(b"ok")
    assert len(seen) == 1  # nothing but the allowed push reached GitHub
    sent = seen[0].decode()
    assert sent.startswith("POST /acme/api.git/git-receive-pack HTTP/1.1\r\nHost: github.com\r\n")
    assert f"Authorization: Basic {base64.b64encode(b'x-access-token:ghs_write').decode()}" in sent
    assert p not in sent and "Connection: close" in sent
    assert other.startswith(b"HTTP/1.1 403") and read_only.startswith(b"HTTP/1.1 403")
    assert anon.startswith(b"HTTP/1.1 401") and b'WWW-Authenticate: Basic realm="valxel git"' in anon
    assert forged.startswith(b"HTTP/1.1 401")
    assert [r["host"] for r in refusals.take("s1")] == ["github.com/evil/exfil (write)", "github.com/acme/lib (write)"]


def test_without_the_key_the_proxy_serves_no_git():
    async def scenario():
        proxy = await asyncio.start_server(ep.make_handler(()), "127.0.0.1", 0)
        r, w = await asyncio.open_connection("127.0.0.1", proxy.sockets[0].getsockname()[1])
        w.write(b"GET /git/acme/api.git/info/refs?service=git-upload-pack HTTP/1.1\r\n\r\n")
        await w.drain()
        line = (await r.readuntil(b"\r\n\r\n")).split(b"\r\n")[0]
        w.close()
        proxy.close()
        return line

    assert asyncio.run(scenario()) == b"HTTP/1.1 405 Method Not Allowed"


def test_the_sessions_clone_points_at_the_broker():
    assert gb.remote_url("acme/api") == "https://github.com/acme/api.git"
    assert gb.remote_url("acme/api", "http://egress-proxy:3128") == "http://egress-proxy:3128/git/acme/api.git"
    with pytest.raises(ValueError):
        gb.remote_url("acme/api", "file:///etc")


def test_the_runner_clones_through_the_broker(tmp_path, monkeypatch):
    got = {}

    def fake_clone(repo, branch, token, root, via=""):
        got.update(repo=repo, token=token, via=via)
        (Path(root) / "work" / "api").mkdir(parents=True)
        return str(Path(root) / "work" / "api")

    monkeypatch.setattr(sandbox, "clone", fake_clone)

    class SDK:
        def __call__(self, prompt, options):
            return {"result": "done", "session_id": "x"}

    spec = {"version": session_runner.VERSION, "runtime": "claude_code", "id": "s1", "prompt": "p", "options": {},
            "env": {}, "files": {}, "fallback": {"prompt": "p", "session_mode": "fresh"},
            "checkout": {"repo": "acme/api", "branch": "b", "token": "vxg1.x", "via": "http://egress-proxy:3128"}}
    session_runner.run_spec(spec, tmp_path, query=SDK())
    assert got == {"repo": "acme/api", "token": "vxg1.x", "via": "http://egress-proxy:3128"}


def test_every_executor_hands_over_passes_when_the_broker_is_on(monkeypatch):
    seen = []

    class Inner:
        def run(self, spec, on_step=None):
            seen.append(spec)
            return {"ok": True}

    for k, v in ENV.items():
        monkeypatch.setenv(k, v)
    out = session_runner.Brokered(Inner()).run(
        {"id": "s", "checkout": {"repo": "acme/api", "branch": "b", "token": "ghs_write"}}, on_step=lambda s: None)
    assert out == {"ok": True} and seen[0]["checkout"]["token"].startswith(gb.PREFIX)


def test_the_shell_sandbox_may_reach_the_broker(monkeypatch):
    monkeypatch.setenv("VALXEL_GIT_BROKER_URL", "http://egress-proxy.valxel.svc.cluster.local:3128")
    assert "egress-proxy.valxel.svc.cluster.local" in sandbox.claude_sandbox()["network"]["allowedDomains"]

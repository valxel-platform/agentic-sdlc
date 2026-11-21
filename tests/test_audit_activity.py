"""T-115..T-117: what agent sessions do, who read what, and what assistants and agents did for people."""

import asyncio
import base64
import json

import httpx
import pytest
from test_mcp_server import pool  # noqa: F401 - the fake user pool
from fastapi.testclient import TestClient

from valxel_sdlc import agent_steps as st
from valxel_sdlc import audit_events as ae
from valxel_sdlc import egress_proxy as ep
from valxel_sdlc import session_launcher as sl
from valxel_sdlc.api.app import create_app
from valxel_sdlc.web.auth import AuthConfig

AUTH = AuthConfig(mode="dev", secret="x" * 40)


def acts(engine, prefix):
    return [e["data"] for e in engine.services.audit.outbox.events() if e["data"]["action"].startswith(prefix)]


def test_a_sessions_actions_are_recorded_as_the_agent_for_the_feature(engine):
    step = st.reporter("FARM-FEAT-7", "build", "coder", "acme/farm-api")
    step("Editing src/a.py")  # shown, not recorded
    step({"action": {"kind": "file.change", "tool": "Edit", "target": "src/a.py"}})
    step({"action": {"kind": "egress.refused", "tool": "proxy", "target": "evil.example", "port": 443}})
    got = acts(engine, "agent.")
    assert [(a["action"], a["outcome"]) for a in got] == [("agent.file.change", "success"),
                                                          ("agent.egress.refused", "denied")]
    a = got[0]
    assert a["actor"] == {"id": "agent:coder", "type": "agent"} and a["resource"] == {"type": "feature",
                                                                                      "ids": ["FARM-FEAT-7"]}
    assert a["product"] == "FARM" and a["details"]["target"] == "src/a.py" and a["details"]["repo"] == "farm-api"
    assert st.recent("FARM-FEAT-7")[-1]["step"] == "Editing src/a.py"  # actions don't reach the page


def test_a_failing_audit_never_stops_the_session(engine, monkeypatch, caplog):
    def down(*a, **k):
        raise ae.AuditUnavailable("down")

    monkeypatch.setattr(engine.services.audit, "record", down)
    st.reporter("FARM-FEAT-8", "build", "coder")({"action": {"kind": "file.change", "tool": "Edit", "target": "x"}})
    assert "not recorded" in caplog.text


def test_actions_cross_the_streamed_run_as_they_are():
    got = []
    st.read([st.line({"step": "Editing a"}), st.line({"step": {"action": {"kind": "mcp.call"}}}),
             st.line({"outcome": {"ok": True}})], got.append)
    assert got == ["Editing a", {"action": {"kind": "mcp.call"}}]
    assert st.action("mcp__jira__create_issue", {})["target"] == "jira/create_issue"
    assert st.action("WebFetch", {"url": "https://docs.example.com/a?token=x"})["target"] == "docs.example.com"
    assert st.action("Read", {"file_path": "a"}) is None


def test_the_proxy_keeps_each_sessions_refusals_for_the_launcher_only():
    async def scenario():
        proxy = await asyncio.start_server(ep.make_handler(("github.com",), token="t0k"), "127.0.0.1", 0)
        port = proxy.sockets[0].getsockname()[1]

        async def send(raw: bytes) -> bytes:
            r, w = await asyncio.open_connection("127.0.0.1", port)
            w.write(raw)
            await w.drain()
            out = await r.read(4096)
            w.close()
            return out

        named = base64.b64encode(b"s1:").decode()
        await send(f"CONNECT evil.example:443 HTTP/1.1\r\nProxy-Authorization: Basic {named}\r\n\r\n".encode())
        await send(b"CONNECT other.example:443 HTTP/1.1\r\n\r\n")  # unnamed: logged only
        stranger = await send(b"GET /refusals/s1 HTTP/1.1\r\nAuthorization: Bearer nope\r\n\r\n")
        mine = await send(b"GET /refusals/s1 HTTP/1.1\r\nAuthorization: Bearer t0k\r\n\r\n")
        again = await send(b"GET /refusals/s1 HTTP/1.1\r\nAuthorization: Bearer t0k\r\n\r\n")
        proxy.close()
        return stranger, mine, again

    stranger, mine, again = asyncio.run(scenario())
    assert b"401" in stranger.split(b"\r\n")[0]
    got = json.loads(mine.split(b"\r\n\r\n", 1)[1])
    assert [(r["host"], r["port"]) for r in got] == [("evil.example", 443)]
    assert json.loads(again.split(b"\r\n\r\n", 1)[1]) == []  # handed over once
    r = ep.Refusals()
    for i in range(ep.Refusals.PER + 5):
        r.add("s", f"h{i}", 443)
    assert len(r.take("s")) == ep.Refusals.PER


def test_the_launcher_passes_a_sessions_refusals_on_after_its_outcome():
    from test_session_launcher import ENV, FakeDocker

    def handle(req: httpx.Request) -> httpx.Response:
        if req.url.host == "egress-proxy":
            assert req.url.path == "/refusals/abc123" and req.headers["authorization"] == "Bearer t0k"
            return httpx.Response(200, json=[{"host": "evil.example", "port": 443}])
        if req.url.path == "/healthz":
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(200, text=st.line({"step": "Editing a"}).decode()
                              + st.line({"outcome": {"ok": True}}).decode())

    lch = sl.Launcher(sl.Settings(ENV), docker=FakeDocker(), http=httpx.Client(transport=httpx.MockTransport(handle)),
                      sleep=lambda s: None)
    got = []
    out = st.read(lch.stream({"id": "abc123", "prompt": "x", "timeout_s": 60}), got.append)
    assert out == {"ok": True}
    assert got[-1] == {"action": {"kind": "egress.refused", "tool": "proxy", "target": "evil.example", "port": 443}}


def test_reads_are_recorded_and_the_apps_own_plumbing_is_not(engine):
    with TestClient(create_app(engine, auth=AUTH, web_dist=None)) as c:
        c.post("/api/auth/dev-login", json={"login": "ann", "email": "a@t116.example", "orgs": ["x"]})
        c.get("/api/me")
        c.get("/api/features")
        c.get("/api/features/NOPE-FEAT-1")
    reads = acts(engine, "api.read")
    routes = [(r["details"]["route"], r["outcome"]) for r in reads]
    assert ("/api/features", "success") in routes
    assert any(route == "/api/features/{feature_id}" and r["resource"]["ids"] == ["NOPE-FEAT-1"]
               for (route, _), r in zip(routes, reads, strict=True))
    assert not any(route.startswith("/api/me") for route, _ in routes)


def test_reads_can_be_left_out(engine, monkeypatch):
    monkeypatch.setenv("VALXEL_AUDIT_READS", "off")
    with TestClient(create_app(engine, auth=AUTH, web_dist=None)) as c:
        c.post("/api/auth/dev-login", json={"login": "ann", "email": "a@t116.example", "orgs": ["x"]})
        c.get("/api/features")
    assert acts(engine, "api.read") == []


def test_an_assistants_calls_name_the_client_and_the_person(engine):
    from valxel_sdlc.api import mcp

    app = create_app(engine, auth=AUTH, web_dist=None)
    signer = __import__("valxel_sdlc.web.auth", fromlist=["SessionSigner"]).SessionSigner(AUTH.secret)
    person = {"login": "ann@acme.example", "name": "Ann", "email": "ann@acme.example", "signed_in_at": 0,
              "idp": "mcp", "strong": False}
    with TestClient(app):
        asyncio.run(mcp.call_api(app, signer, person, "GET", "/features", via="mcp", via_client="claude-desktop"))
    [r] = [r for r in acts(engine, "api.read") if r["details"]["route"] == "/api/features"]
    assert r["actor"] == {"id": "ann@acme.example", "type": "user", "client": "claude-desktop"}
    assert r["details"]["via"] == "mcp"


def test_a_web_session_cant_claim_to_be_an_assistant(engine):
    with TestClient(create_app(engine, auth=AUTH, web_dist=None)) as c:
        c.post("/api/auth/dev-login", json={"login": "ann", "email": "a@x.example", "orgs": ["x"]})
        c.get("/api/features", headers={"X-Valxel-Via": "mcp"})
    [r] = [r for r in acts(engine, "api.read") if r["details"]["route"] == "/api/features"]
    assert "client" not in r["actor"] and "via" not in r.get("details", {})


def test_an_agent_turned_away_and_one_let_in_are_both_recorded(engine, pool, monkeypatch):  # noqa: F811
    from test_a2a import call, say
    from test_mcp_server import DOMAIN, ISSUER, token

    monkeypatch.setenv("VALXEL_A2A_CLIENT_IDS", "mcp-client")
    auth = AuthConfig(mode="dev", secret="x" * 40, public_url="https://testserver", cognito_issuer=ISSUER,
                      cognito_domain=DOMAIN)
    with TestClient(create_app(engine, auth=auth, web_dist=None)) as c:
        assert call(c, "SendMessage", say("hi"), token(username=None, scope="valxel/agents")).status_code == 401
        call(c, "SendMessage", say("Patients should confirm consent before intake"))
    assert acts(engine, "a2a.refused")[0]["outcome"] == "denied"
    via = [a for a in acts(engine, "api.") if (a.get("details") or {}).get("via") == "a2a"]
    assert via and via[0]["actor"]["client"] == "mcp-client"

"""Valxel over MCP (api/mcp.py): only a person's token gets in, and tools act as that person."""

import json
import time
from io import BytesIO

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from valxel_sdlc.api import mcp as mcp_api
from valxel_sdlc.api.app import create_app
from valxel_sdlc.web.auth import AuthConfig

ISSUER = "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_TEST"
DOMAIN = "valxel-test.auth.us-east-1.amazoncognito.com"
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
JWK = {**json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(KEY.public_key())), "kid": "k1", "alg": "RS256", "use": "sig"}
PEOPLE = {"sub-ana": {"sub": "sub-ana", "email": "ana@example.com", "email_verified": "true", "name": "Ana"}}


class FakePool:
    """The user pool's JWKS and userInfo endpoints."""

    def __init__(self):
        self.userinfo_calls = 0

    def open(self, req, timeout=None):
        url = req.full_url
        if url.endswith("/.well-known/jwks.json"):
            return BytesIO(json.dumps({"keys": [JWK]}).encode())
        if url.endswith("/oauth2/userInfo"):
            self.userinfo_calls += 1
            claims = jwt.decode(req.headers["Authorization"].split()[1], options={"verify_signature": False})
            return BytesIO(json.dumps(PEOPLE[claims["sub"]]).encode())
        raise AssertionError(url)


def token(**over):
    now = int(time.time())
    claims = {"iss": ISSUER, "sub": "sub-ana", "token_use": "access", "client_id": "mcp-client",
              "username": "sub-ana", "scope": "openid email profile", "iat": now, "auth_time": now, "exp": now + 600,
              **over}
    return jwt.encode({k: v for k, v in claims.items() if v is not None}, KEY, algorithm="RS256", headers={"kid": "k1"})


@pytest.fixture
def pool(monkeypatch):
    fake = FakePool()
    monkeypatch.setattr(mcp_api, "default_opener", lambda: fake)
    monkeypatch.setenv("VALXEL_MCP_CLIENT_IDS", "mcp-client")
    return fake


@pytest.fixture
def client(engine, pool):
    auth = AuthConfig(mode="dev", secret="x" * 40, public_url="https://testserver", cognito_issuer=ISSUER,
                      cognito_domain=DOMAIN)
    with TestClient(create_app(engine, auth=auth, web_dist=None)) as c:
        yield c


def rpc(client, method, params=None, tok=None):
    headers = {"accept": "application/json, text/event-stream", "content-type": "application/json",
               "mcp-protocol-version": "2025-06-18"}
    if tok:
        headers["authorization"] = f"Bearer {tok}"
    return client.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}})


def tool(client, name, tok=None, **args):
    r = rpc(client, "tools/call", {"name": name, "arguments": args}, tok or token())
    assert r.status_code == 200, r.text
    result = r.json()["result"]
    text = result["content"][0]["text"] if result.get("content") else ""
    if result.get("isError"):
        return True, text
    out = result.get("structuredContent")
    return False, out.get("result", out) if isinstance(out, dict) else json.loads(text)


def wait_pending(engine, fid, timeout=10.0):
    end = time.time() + timeout
    while time.time() < end:
        if fid not in engine.running and engine.view(fid)["pending"]:
            return engine.view(fid)
        time.sleep(0.02)
    raise TimeoutError(fid)


def test_it_tells_clients_where_to_sign_in(client):
    r = rpc(client, "tools/list")
    assert r.status_code == 401 and "oauth-protected-resource" in r.headers["www-authenticate"]
    meta = client.get("/.well-known/oauth-protected-resource/mcp").json()
    assert meta["resource"] == "https://testserver/mcp" and meta["authorization_servers"] == [ISSUER]


# The claims, not the tokens: a token made at import differs by worker (the time in it), and pytest
# names a test by its parameters, so parallel workers would collect different tests (review #10).
@pytest.mark.parametrize("why, claims", [
    # A client-credentials token: an agent or a service, no person behind it.
    ("client credentials", {"username": None, "scope": "valxel/agents"}),
    ("no openid scope", {"scope": "email"}),
    ("another app's client", {"client_id": "someone-elses"}),
    ("an ID token", {"token_use": "id"}),
    ("another issuer", {"iss": "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_OTHER"}),
    ("expired", {"exp": -3600}),  # relative to now, made when the test runs
], ids=["client-credentials", "no-openid-scope", "another-apps-client", "id-token", "another-issuer", "expired"])
def test_only_a_persons_token_gets_in(client, why, claims):
    if "exp" in claims:
        claims = {**claims, "exp": int(time.time()) + claims["exp"]}
    assert rpc(client, "tools/list", tok=token(**claims)).status_code == 401, why


def test_tools_act_as_the_person(client, pool):
    assert {t["name"] for t in rpc(client, "tools/list", tok=token()).json()["result"]["tools"]} >= {
        "inbox", "features", "feature", "artifact", "decision_brief", "decide", "trace", "start_intent",
        "continue_intent", "submit_intent", "intents", "whoami"}
    tok = token(jti="whoami")  # its own token, whatever second the one above was made in
    before = pool.userinfo_calls
    err, me = tool(client, "whoami", tok)
    assert not err and me["login"] == "ana@example.com" and me["idp"] == "mcp" and me["strong"] is False
    tool(client, "whoami", tok)
    assert pool.userinfo_calls == before + 1  # the person is looked up once per token, not per call


def test_a_gate_is_decided_by_its_approver_as_themselves(client, engine, monkeypatch):
    fid = engine.start(product_id="CLN", title="Consent check on intake")["feature_id"]
    wait_pending(engine, fid)
    [item] = engine.pending(fid)

    err, msg = tool(client, "decide", feature_id=fid, interrupt_id=item["interrupt_id"], decision="approve")
    assert err and "403" in msg  # not one of the gate's approvers

    real = engine.item
    monkeypatch.setattr(engine, "item", lambda f, i: {**real(f, i), "approvers": ["ana@example.com"]})
    err, out = tool(client, "decide", feature_id=fid, interrupt_id=item["interrupt_id"], decision="approve",
                    feedback="Looks right")
    assert not err, out
    v = wait_pending(engine, fid)
    [d] = v["gate_decisions"]
    assert d["approver"] == "ana@example.com" and d.get("via") == "mcp"


def test_group_approvals_need_the_web_app(client, engine, monkeypatch):
    """An access token says nothing of a passkey, and a workspace group approves only with one."""
    fid = engine.start(product_id="CLN", title="Allergy list")["feature_id"]
    wait_pending(engine, fid)
    [item] = engine.pending(fid)
    real = engine.item
    monkeypatch.setattr(engine, "item", lambda f, i: {**real(f, i), "approvers": ["group:clinic/product"]})
    monkeypatch.setattr("valxel_sdlc.web.auth.User.matches", lambda self, a: True)
    err, msg = tool(client, "decide", feature_id=fid, interrupt_id=item["interrupt_id"], decision="approve")
    assert err and "passkey" in msg


def test_intents_are_the_persons_own(client):
    err, s = tool(client, "start_intent", text="Patients should confirm consent before intake")
    assert not err, s
    assert s["owner"] == "ana@example.com" and s["messages"]
    err, mine = tool(client, "intents")
    assert not err, mine
    assert [i["id"] for i in mine] == [s["id"]]


OIDC_ISSUER = "https://login.acme.example/oauth2/default"


class FakeOidc:
    """Discovery, keys and userinfo of an OpenID provider whose access tokens are JWTs (RFC 9068)."""

    def open(self, req, timeout=None):
        url = req.full_url
        if url == f"{OIDC_ISSUER}/.well-known/openid-configuration":
            return BytesIO(json.dumps({"issuer": OIDC_ISSUER, "jwks_uri": f"{OIDC_ISSUER}/v1/keys",
                                       "userinfo_endpoint": f"{OIDC_ISSUER}/v1/userinfo"}).encode())
        if url == f"{OIDC_ISSUER}/v1/keys":
            return BytesIO(json.dumps({"keys": [JWK]}).encode())
        if url == f"{OIDC_ISSUER}/v1/userinfo":
            claims = jwt.decode(req.headers["Authorization"].split()[1], options={"verify_signature": False})
            return BytesIO(json.dumps(PEOPLE[claims["sub"]]).encode())
        raise AssertionError(url)


def oidc_token(**over):
    now = int(time.time())
    claims = {"iss": OIDC_ISSUER, "sub": "sub-ana", "client_id": "mcp-client", "iat": now, "exp": now + 600, **over}
    return jwt.encode({k: v for k, v in claims.items() if v is not None}, KEY, algorithm="RS256", headers={"kid": "k1"})


def test_any_oidc_provider_with_jwt_access_tokens_can_serve_mcp(monkeypatch):
    """ADR 0010: MCP doesn't need Cognito; an installation on another cloud uses its own provider."""
    monkeypatch.setenv("VALXEL_MCP_CLIENT_IDS", "mcp-client")
    auth = AuthConfig(mode="oidc", secret="x" * 40, public_url="https://testserver", oidc_issuer=OIDC_ISSUER + "/")
    s = mcp_api.settings_from_env(auth)
    assert (s["issuer"], s["kind"]) == (OIDC_ISSUER, "oidc")
    v = mcp_api.PersonVerifier(s["issuer"], s["domain"], s["client_ids"], opener=FakeOidc(), kind="oidc")
    assert v.person(oidc_token())["email"] == "ana@example.com"
    assert v.claims(oidc_token(client_id=None, azp="mcp-client"))["sub"] == "sub-ana"
    for bad, says in ((oidc_token(client_id="other"), "isn't one of"),
                      (oidc_token(sub="mcp-client"), "not issued to a person")):
        with pytest.raises(mcp_api.NotAPerson, match=says):
            v.claims(bad)
    auth.oidc_profile = "google"
    with pytest.raises(RuntimeError, match="opaque"):
        mcp_api.settings_from_env(auth)

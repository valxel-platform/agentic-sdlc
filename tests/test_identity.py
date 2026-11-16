"""Google, email and passkey sign-in through Cognito (the sign-in design, A4).

A fake user pool signs real RS256 ID tokens, so the flow is checked end to end: PKCE, state and
nonce; the token's signature, issuer, audience and expiry; verified email only; and what a passkey
lets someone do that an email code doesn't."""

import base64
import hashlib
import json
import time
from urllib.parse import parse_qs, urlparse

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from valxel_sdlc.api.app import create_app
from valxel_sdlc.web.auth import AuthConfig, SessionSigner, User, can_decide, session_payload
from valxel_sdlc.web.identity import CognitoProvider, SignInError

ISSUER = "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_TEST"
CLIENT = "client-123"
WS = "wrkspc-shared"
SECRET = "k" * 48


class Resp:
    def __init__(self, body: dict):
        self.body = json.dumps(body).encode()

    def read(self):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakePool:
    """Cognito's token endpoint and published keys. `claims` are what the next ID token says."""

    def __init__(self):
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.kid = "key-1"
        self.challenge = ""
        self.nonce = ""
        self.claims: dict = {}
        self.calls = []

    def jwk(self):
        pub = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(self.key.public_key()))
        return {**pub, "kid": self.kid, "alg": "RS256", "use": "sig"}

    def token(self, **over):
        now = int(time.time())
        claims = {"sub": "u-1", "iss": ISSUER, "aud": CLIENT, "token_use": "id", "iat": now, "exp": now + 3600,
                  "auth_time": now, "nonce": self.nonce, "email": "Ben@Farm.example", "email_verified": True,
                  "name": "Ben Ortiz", "amr": ["email_otp"], "acr": "urn:cognito:loa:2", **self.claims, **over}
        claims = {k: v for k, v in claims.items() if v is not None}
        return jwt.encode(claims, self.key, algorithm="RS256", headers={"kid": self.kid})

    def open(self, req, timeout=None):
        url = req.full_url
        self.calls.append(url)
        if url.endswith("/.well-known/jwks.json"):
            return Resp({"keys": [self.jwk()]})
        if url.endswith("/oauth2/token"):
            assert req.headers["Authorization"] == "Basic " + base64.b64encode(f"{CLIENT}:shh".encode()).decode()
            form = parse_qs(req.data.decode())
            digest = hashlib.sha256(form["code_verifier"][0].encode()).digest()
            assert base64.urlsafe_b64encode(digest).decode().rstrip("=") == self.challenge  # PKCE
            assert form["code"] == ["the-code"]
            return Resp({"id_token": self.token(), "access_token": "at", "token_type": "Bearer"})
        raise AssertionError(url)


def auth_cfg(mode="cognito"):
    return AuthConfig(mode=mode, secret=SECRET, public_url="https://sdlc.valxel.dev", cognito_domain="auth.valxel.dev",
                      cognito_client_id=CLIENT, cognito_client_secret="shh", cognito_issuer=ISSUER)


@pytest.fixture
def pool(monkeypatch):
    fake = FakePool()
    real = AuthConfig.cognito
    monkeypatch.setattr(AuthConfig, "cognito", lambda self, opener=None: real(self, opener=fake))
    return fake


@pytest.fixture
def client(engine, pool):
    with TestClient(create_app(engine, auth=auth_cfg(), web_dist=None), base_url="https://sdlc.valxel.dev") as c:
        yield c


def sign_in(client, pool, option="email", next_path="/", **claims):
    r = client.get("/api/auth/login", params={"provider": option, "next": next_path}, follow_redirects=False)
    assert r.status_code == 302
    q = parse_qs(urlparse(r.headers["location"]).query)
    pool.challenge, pool.nonce, pool.claims = q["code_challenge"][0], q["nonce"][0], claims
    return client.get("/api/auth/oidc/callback", params={"code": "the-code", "state": q["state"][0]},
                      follow_redirects=False), q


def test_the_sign_in_page_offers_github_google_and_email(engine, pool):
    cfg = auth_cfg("github")
    cfg.client_id, cfg.client_secret = "gh", "ghs"
    with TestClient(create_app(engine, auth=cfg, web_dist=None)) as c:
        assert [p["id"] for p in c.get("/api/auth/config").json()["providers"]] == ["github", "google", "email"]
    with TestClient(create_app(engine, auth=auth_cfg(), web_dist=None)) as c:
        assert [p["id"] for p in c.get("/api/auth/config").json()["providers"]] == ["google", "email"]
        assert c.get("/api/auth/login", params={"provider": "github"}, follow_redirects=False).status_code == 404


def test_an_email_sign_in_runs_the_code_flow_with_pkce_and_lands_where_it_started(client, pool):
    r, q = sign_in(client, pool, next_path="/join/wrkspc-shared/tok")
    assert q["response_type"] == ["code"] and q["code_challenge_method"] == ["S256"]
    assert q["scope"] == ["openid email profile"] and q["redirect_uri"] == ["https://sdlc.valxel.dev/api/auth/oidc/callback"]
    assert "identity_provider" not in q and "acr_values" not in q
    assert r.status_code == 302 and r.headers["location"] == "/join/wrkspc-shared/tok"
    me = client.get("/api/me").json()
    assert me["login"] == me["email"] == "ben@farm.example" and me["idp"] == "cognito"
    assert me["name"] == "Ben Ortiz" and me["strong"] is False  # an email code is one factor
    assert any(u.endswith("/.well-known/jwks.json") for u in pool.calls)


def test_google_goes_straight_to_google_and_a_passkey_asks_for_level_3(client, pool):
    r = client.get("/api/auth/login", params={"provider": "google"}, follow_redirects=False)
    assert parse_qs(urlparse(r.headers["location"]).query)["identity_provider"] == ["Google"]
    r = client.get("/api/auth/login", params={"provider": "passkey"}, follow_redirects=False)
    q = parse_qs(urlparse(r.headers["location"]).query)
    assert q["acr_values"] == ["urn:cognito:loa:3"] and q["prompt"] == ["login"] and "identity_provider" not in q
    r, _ = sign_in(client, pool, "passkey", amr=["swk"], acr="urn:cognito:loa:3")
    assert client.get("/api/me").json()["strong"] is True


@pytest.mark.parametrize("bad, why", [
    ({"nonce": "someone-elses"}, "wasn't issued for this sign-in"),
    ({"aud": "another-client"}, "didn't check out"),
    ({"iss": "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_OTHER"}, "didn't check out"),
    ({"exp": int(time.time()) - 600}, "didn't check out"),
    ({"token_use": "access"}, "isn't an ID token"),
    ({"email_verified": False}, "isn't verified"),
    ({"email_verified": "false"}, "isn't verified"),
    ({"email": None}, "isn't verified"),
])
def test_a_token_that_doesnt_check_out_signs_no_one_in(client, pool, bad, why):
    r, _ = sign_in(client, pool, **bad)
    assert r.status_code == 401 and why in r.json()["detail"]
    assert client.get("/api/me").status_code == 401


def test_a_forged_signature_or_unknown_key_is_refused(pool):
    p = CognitoProvider(domain="auth.valxel.dev", client_id=CLIENT, client_secret="shh", issuer=ISSUER,
                        redirect_uri="https://x/cb", opener=pool)
    pool.nonce = "n"
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    forged = jwt.encode(jwt.decode(pool.token(), options={"verify_signature": False}), other, algorithm="RS256",
                        headers={"kid": pool.kid})
    with pytest.raises(SignInError, match="didn't check out"):
        p.verify(forged, "n")
    pool.kid = "key-2"  # a token from a key the pool doesn't publish
    token = pool.token()
    pool.kid = "key-1"
    with pytest.raises(SignInError, match="doesn't publish"):
        p.verify(token, "n")


def test_state_must_match_the_sign_in_it_started(client, pool):
    client.get("/api/auth/login", params={"provider": "email"}, follow_redirects=False)
    r = client.get("/api/auth/oidc/callback", params={"code": "the-code", "state": "forged"}, follow_redirects=False)
    assert r.status_code == 400
    r = client.get("/api/auth/oidc/callback", params={"error": "access_denied"}, follow_redirects=False)
    assert r.headers["location"] == "/?signin=failed"


def test_group_approvers_and_admins_need_a_passkey_and_teams_dont(client, engine, pool):
    w = engine.services.workspaces
    w.bootstrap_owner(WS, {"name": "Ben", "email": "ben@farm.example"})
    w.set_mapping("FARM", {"g1": ["product"]}, actor="ben")
    fid = engine.start(product_id="FARM", title="Offline crop log", description="x")["feature_id"]
    [item] = [p for p in engine.pending(fid) if p.get("gate") == "g1"]

    sign_in(client, pool)  # an email code
    [mine] = [i for i in client.get("/api/inbox").json() if i["feature_id"] == fid]
    assert mine["can_decide"] is False and mine["needs_passkey"] is True
    r = client.post(f"/api/features/{fid}/decisions/{item['interrupt_id']}", json={"decision": "approve"})
    assert r.status_code == 403 and "passkey" in r.json()["detail"]
    r = client.post(f"/api/workspaces/{WS}/groups", json={"name": "QA"})
    assert r.status_code == 403 and "passkey" in r.json()["detail"]

    sign_in(client, pool, "passkey", amr=["swk"], acr="urn:cognito:loa:3")
    assert client.post(f"/api/workspaces/{WS}/groups", json={"name": "QA"}).status_code == 201
    r = client.post(f"/api/features/{fid}/decisions/{item['interrupt_id']}", json={"decision": "approve"})
    assert r.status_code == 202

    # A GitHub team approves as before, whatever the sign-in (GitHub requires two factors itself).
    assert can_decide(User(login="x", teams=["acme-farm/product-owners"], strong=False),
                      {"approvers": ["acme-farm/product-owners", f"group:{WS}/product"]})
    assert not can_decide(User(login="x", groups=[f"{WS}/product"], strong=False), {"approvers": [f"group:{WS}/product"]})


def test_approvers_and_admins_sign_in_again_after_4_hours(engine, pool):
    w = engine.services.workspaces
    w.bootstrap_owner(WS, {"email": "ben@farm.example"})
    _, token = w.invite(WS, {"email": "vic@farm.example"}, role="member", actor="ben")
    w.accept(WS, token, login="vic@farm.example", email="vic@farm.example")
    signer = SessionSigner(SECRET)
    with TestClient(create_app(engine, auth=auth_cfg(), web_dist=None), base_url="https://sdlc.valxel.dev") as c:
        five_hours_ago = int(time.time()) - 5 * 3600
        for email, ok in (("ben@farm.example", False), ("vic@farm.example", True)):
            cookie = signer.dumps(session_payload(User(login=email, email=email, idp="cognito",
                                                       signed_in_at=five_hours_ago)))
            r = c.get("/api/me", headers={"Cookie": f"valxel_session={cookie}"})
            assert (r.status_code == 200) is ok, email  # the owner's session ended; a member's lasts 12 hours
            if not ok:
                assert "every 4 hours" in r.json()["detail"]


def test_adding_a_passkey_goes_to_cognitos_page(client, pool):
    sign_in(client, pool)
    r = client.get("/api/auth/passkey", follow_redirects=False)
    url = urlparse(r.headers["location"])
    assert url.netloc == "auth.valxel.dev" and url.path == "/passkeys/add"
    assert parse_qs(url.query) == {"client_id": [CLIENT], "redirect_uri": ["https://sdlc.valxel.dev/api/auth/oidc/callback"]}


def test_cognito_mode_needs_its_settings(monkeypatch):
    monkeypatch.setenv("VALXEL_AUTH", "cognito")
    monkeypatch.setenv("VALXEL_SESSION_SECRET", __import__("secrets").token_hex(24))  # made here, never in the repo
    for k in ("VALXEL_COGNITO_DOMAIN", "VALXEL_COGNITO_CLIENT_ID", "VALXEL_COGNITO_CLIENT_SECRET", "VALXEL_COGNITO_ISSUER"):
        monkeypatch.delenv(k, raising=False)
    with pytest.raises(RuntimeError, match="Cognito sign-in needs"):
        AuthConfig.from_env()
    monkeypatch.setenv("VALXEL_COGNITO_DOMAIN", "auth.valxel.dev")
    monkeypatch.setenv("VALXEL_COGNITO_CLIENT_ID", CLIENT)
    monkeypatch.setenv("VALXEL_COGNITO_CLIENT_SECRET", "shh")
    monkeypatch.setenv("VALXEL_COGNITO_ISSUER", ISSUER)
    cfg = AuthConfig.from_env()
    assert cfg.cognito_enabled and not cfg.github_enabled and cfg.cognito() is not None

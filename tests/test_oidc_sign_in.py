"""Sign-in through the team's own OpenID Connect provider (T-148): Okta, Entra ID, Google Workspace,
Keycloak and the like.

A fake provider publishes a discovery document and keys and signs real RS256 ID tokens, so the flow
is checked end to end: discovery, PKCE, state and nonce, the token's signature, issuer, audience and
expiry, verified email, two factors from `amr` or `acr`, and groups as approvers."""

import base64
import hashlib
import json
import secrets
import time
from urllib.parse import parse_qs, urlparse

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from valxel_sdlc.api.app import create_app
from valxel_sdlc.web.auth import AuthConfig, User, can_decide, needs_passkey
from valxel_sdlc.web.identity import OIDCProvider, SignInError

ISSUER = "https://login.acme.example/oauth2/default"
CLIENT = "valxel-acme"
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


class FakeIdP:
    """Discovery, keys and the token endpoint. `claims` are what the next ID token says."""

    def __init__(self, *, issuer=ISSUER, auth_methods=("client_secret_basic",)):
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.issuer, self.auth_methods = issuer, list(auth_methods)
        self.challenge = self.nonce = ""
        self.claims: dict = {}
        self.calls: list[str] = []
        self.form: dict = {}

    def token(self, **over):
        now = int(time.time())
        claims = {"sub": "00u1", "iss": ISSUER, "aud": CLIENT, "iat": now, "exp": now + 3600, "nonce": self.nonce,
                  "email": "Ana@Acme.example", "email_verified": True, "name": "Ana Ruiz", "amr": ["pwd"],
                  **self.claims, **over}
        claims = {k: v for k, v in claims.items() if v is not None}
        return jwt.encode(claims, self.key, algorithm="RS256", headers={"kid": "k1"})

    def open(self, req, timeout=None):
        url = req.full_url
        self.calls.append(url)
        if url == f"{ISSUER}/.well-known/openid-configuration":
            return Resp({"issuer": self.issuer, "authorization_endpoint": f"{ISSUER}/v1/authorize",
                         "token_endpoint": f"{ISSUER}/v1/token", "jwks_uri": f"{ISSUER}/v1/keys",
                         "token_endpoint_auth_methods_supported": self.auth_methods})
        if url == f"{ISSUER}/v1/keys":
            pub = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(self.key.public_key()))
            return Resp({"keys": [{**pub, "kid": "k1", "alg": "RS256", "use": "sig"}]})
        if url == f"{ISSUER}/v1/token":
            self.form = parse_qs(req.data.decode())
            if "client_secret_basic" in self.auth_methods:
                assert req.headers["Authorization"] == "Basic " + base64.b64encode(f"{CLIENT}:shh".encode()).decode()
            else:
                assert self.form["client_secret"] == ["shh"] and "Authorization" not in req.headers
            digest = hashlib.sha256(self.form["code_verifier"][0].encode()).digest()
            assert base64.urlsafe_b64encode(digest).decode().rstrip("=") == self.challenge  # PKCE
            return Resp({"id_token": self.token(), "access_token": "at", "token_type": "Bearer"})
        raise AssertionError(url)


def cfg(mode="oidc", **over):
    return AuthConfig(mode=mode, secret=SECRET, public_url="https://sdlc.acme.example", oidc_issuer=ISSUER,
                      oidc_client_id=CLIENT, oidc_client_secret="shh", oidc_label="Sign in with Okta",
                      oidc_groups_claim="groups", **over)


@pytest.fixture
def idp(monkeypatch):
    fake = FakeIdP()
    real = AuthConfig.oidc
    monkeypatch.setattr(AuthConfig, "oidc", lambda self, opener=None: real(self, opener=fake))
    return fake


def app_client(engine, auth):
    return TestClient(create_app(engine, auth=auth, web_dist=None), base_url="https://sdlc.acme.example")


def sign_in(client, idp, provider="oidc", **claims):
    r = client.get("/api/auth/login", params={"provider": provider, "next": "/inbox"}, follow_redirects=False)
    assert r.status_code == 302, r.text
    q = parse_qs(urlparse(r.headers["location"]).query)
    idp.challenge, idp.nonce, idp.claims = q["code_challenge"][0], q["nonce"][0], claims
    return client.get("/api/auth/oidc/callback", params={"code": "the-code", "state": q["state"][0]},
                      follow_redirects=False), r.headers["location"], q


def test_the_sign_in_page_offers_the_teams_provider(engine, idp):
    with app_client(engine, cfg()) as c:
        assert c.get("/api/auth/config").json()["providers"] == [{"id": "oidc", "label": "Sign in with Okta"}]
    both = cfg("github", client_id="gh", client_secret="ghs")
    with app_client(engine, both) as c:
        assert [p["id"] for p in c.get("/api/auth/config").json()["providers"]] == ["github", "oidc"]


def test_a_sign_in_finds_the_endpoints_by_discovery_and_runs_the_code_flow(engine, idp):
    with app_client(engine, cfg()) as c:
        r, location, q = sign_in(c, idp, groups=["Release Managers", "eng"])
        assert location.startswith(f"{ISSUER}/v1/authorize?")
        assert q["code_challenge_method"] == ["S256"] and q["scope"] == ["openid email profile"]
        assert q["redirect_uri"] == ["https://sdlc.acme.example/api/auth/oidc/callback"] and "prompt" not in q
        assert r.status_code == 302 and r.headers["location"] == "/inbox"
        me = c.get("/api/me").json()
        assert me["login"] == me["email"] == "ana@acme.example" and me["idp"] == "oidc"
        assert me["strong"] is False  # a password alone is one factor
        assert sorted(me["teams"]) == ["idp:Release Managers", "idp:eng"]
        assert idp.form["grant_type"] == ["authorization_code"] and idp.form["code"] == ["the-code"]


def test_two_factors_come_from_amr_or_a_named_acr(engine, idp):
    with app_client(engine, cfg()) as c:
        sign_in(c, idp, amr=["pwd", "mfa"])
        assert c.get("/api/me").json()["strong"] is True
    with app_client(engine, cfg(oidc_strong_acr=("urn:acme:loa:2fa",))) as c:
        sign_in(c, idp, amr=None, acr="urn:acme:loa:2fa")
        assert c.get("/api/me").json()["strong"] is True
        sign_in(c, idp, amr=None, acr="urn:acme:loa:1")
        assert c.get("/api/me").json()["strong"] is False


def test_stepping_up_asks_the_provider_to_sign_in_again(engine, idp):
    """Without Cognito, 'Use a passkey' (the decision page's step-up link) goes to the team's provider."""
    with app_client(engine, cfg(oidc_step_up_acr="urn:acme:loa:2fa")) as c:
        _, _, q = sign_in(c, idp, provider="passkey", acr="urn:acme:loa:2fa")
        assert q["prompt"] == ["login"] and q["max_age"] == ["0"] and q["acr_values"] == ["urn:acme:loa:2fa"]
        assert c.get("/api/me").json()["strong"] is True


@pytest.mark.parametrize("claims, why", [
    ({"email_verified": False}, "isn't verified"),
    ({"email_verified": None}, "isn't verified"),
    ({"email": None}, "isn't verified"),
    ({"aud": "someone-else"}, "didn't check out"),
    ({"iss": "https://evil.example"}, "didn't check out"),
    ({"exp": int(time.time()) - 600}, "didn't check out"),
    ({"aud": [CLIENT, "other"], "azp": "other"}, "another application"),
])
def test_a_token_that_doesnt_check_out_signs_no_one_in(engine, idp, claims, why):
    with app_client(engine, cfg()) as c:
        r, _, _ = sign_in(c, idp, **claims)
        assert r.status_code == 401 and why in r.json()["detail"]
        assert c.get("/api/me").status_code == 401


def test_a_provider_without_email_verified_is_trusted_only_when_told(engine, idp):
    """Entra ID sends no email_verified; a single-tenant issuer the team controls can be trusted."""
    with app_client(engine, cfg(oidc_email_verified="issuer")) as c:
        r, _, _ = sign_in(c, idp, email_verified=None)
        assert r.status_code == 302 and c.get("/api/me").json()["email"] == "ana@acme.example"


def test_the_nonce_must_be_this_sign_ins():
    fake = FakeIdP()
    p = OIDCProvider(issuer=ISSUER, client_id=CLIENT, client_secret="shh", redirect_uri="https://x/cb", opener=fake)
    fake.nonce = "one"
    with pytest.raises(SignInError, match="wasn't issued for this sign-in"):
        p.verify(fake.token(), "another")


def test_a_discovery_document_for_another_issuer_is_refused(engine, monkeypatch):
    fake = FakeIdP(issuer="https://evil.example")
    real = AuthConfig.oidc
    monkeypatch.setattr(AuthConfig, "oidc", lambda self, opener=None: real(self, opener=fake))
    with app_client(engine, cfg()) as c:
        r = c.get("/api/auth/login", params={"provider": "oidc"}, follow_redirects=False)
        assert r.status_code == 502


def test_a_provider_that_wants_the_secret_in_the_form_gets_it_there(engine, monkeypatch):
    fake = FakeIdP(auth_methods=("client_secret_post",))
    real = AuthConfig.oidc
    monkeypatch.setattr(AuthConfig, "oidc", lambda self, opener=None: real(self, opener=fake))
    with app_client(engine, cfg()) as c:
        r, _, _ = sign_in(c, fake)
        assert r.status_code == 302


def test_identity_provider_groups_approve_only_from_two_factors():
    gate = {"approvers": ["idp:Release Managers"]}
    one = User(login="ana@acme.example", idp="oidc", teams=["idp:Release Managers"], strong=False)
    two = User(login="ana@acme.example", idp="oidc", teams=["idp:Release Managers"], strong=True)
    assert not can_decide(one, gate) and needs_passkey(one, gate["approvers"])
    assert can_decide(two, gate)
    assert not can_decide(User(login="x", teams=["idp:eng"], strong=True), gate)


def test_oidc_mode_needs_its_settings(monkeypatch):
    monkeypatch.setenv("VALXEL_AUTH", "oidc")
    monkeypatch.setenv("VALXEL_SESSION_SECRET", secrets.token_hex(24))
    monkeypatch.setenv("VALXEL_PUBLIC_URL", "https://sdlc.acme.example")
    with pytest.raises(RuntimeError, match="VALXEL_OIDC_ISSUER"):
        AuthConfig.from_env()
    monkeypatch.setenv("VALXEL_OIDC_ISSUER", ISSUER)
    monkeypatch.setenv("VALXEL_OIDC_CLIENT_ID", CLIENT)
    monkeypatch.setenv("VALXEL_OIDC_CLIENT_SECRET", "shh")
    monkeypatch.setenv("VALXEL_OIDC_STRONG_ACR", "a, b")
    got = AuthConfig.from_env()
    assert got.oidc_enabled and got.oidc_strong_acr == ("a", "b") and got.oidc_email_verified == "claim"
    with pytest.raises(ValueError, match="https"):
        OIDCProvider(issuer="http://idp.example", client_id="c", client_secret="s", redirect_uri="r")


def test_the_google_profile_pins_the_workspace_domain(monkeypatch):
    """ADR 0010: on Google Cloud, sign-in is Google Workspace through the google profile."""
    monkeypatch.setenv("VALXEL_AUTH", "oidc")
    monkeypatch.setenv("VALXEL_SESSION_SECRET", secrets.token_hex(24))
    monkeypatch.setenv("VALXEL_PUBLIC_URL", "https://sdlc.acme.example")
    monkeypatch.setenv("VALXEL_OIDC_CLIENT_ID", CLIENT)
    monkeypatch.setenv("VALXEL_OIDC_CLIENT_SECRET", "shh")
    monkeypatch.setenv("VALXEL_OIDC_PROFILE", "google")
    with pytest.raises(RuntimeError, match="VALXEL_OIDC_HOSTED_DOMAIN"):
        AuthConfig.from_env()
    monkeypatch.setenv("VALXEL_OIDC_HOSTED_DOMAIN", "Acme.example")
    got = AuthConfig.from_env()
    assert (got.oidc_issuer, got.oidc_label, got.oidc_hosted_domain) == (
        "https://accounts.google.com", "Sign in with Google", "acme.example")
    monkeypatch.setenv("VALXEL_OIDC_PROFILE", "okta")
    with pytest.raises(RuntimeError, match="google, entra"):
        AuthConfig.from_env()


def test_the_entra_profile_trusts_its_tenants_addresses_unless_told_otherwise():
    cfg_ = AuthConfig(mode="oidc", secret="x" * 40, oidc_profile="entra")
    cfg_.apply_profile()
    assert (cfg_.oidc_email_verified, cfg_.oidc_groups_claim) == ("issuer", "groups")
    cfg_ = AuthConfig(mode="oidc", secret="x" * 40, oidc_profile="entra", oidc_groups_claim="roles")
    cfg_.apply_profile({"VALXEL_OIDC_GROUPS_CLAIM"})
    assert cfg_.oidc_groups_claim == "roles"


def test_a_hosted_domain_refuses_other_accounts():
    fake = FakeIdP()
    p = OIDCProvider(issuer=ISSUER, client_id=CLIENT, client_secret="shh", redirect_uri="https://x/cb", opener=fake,
                     hosted_domain="acme.example")
    url = p.authorize_url(state="s", nonce="n", verifier="v" * 43)
    assert parse_qs(urlparse(url).query)["hd"] == ["acme.example"]

    def identity(**claims):
        fake.nonce, fake.challenge = "n", base64.urlsafe_b64encode(
            hashlib.sha256(b"v" * 43).digest()).decode().rstrip("=")
        fake.claims = claims
        return p.identity("code", verifier="v" * 43, nonce="n")

    assert identity(hd="acme.example").email == "ana@acme.example"
    for bad in ({"hd": None}, {"hd": "evil.example"}, {"hd": "acme.example", "email": "ana@evil.example"}):
        with pytest.raises(SignInError, match=r"acme\.example account"):
            identity(**bad)

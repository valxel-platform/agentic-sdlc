"""Tools that connect through the vendor's OAuth consent (the sign-in design, A8): Jira, Lucid,
Sentry and PagerDuty. Consent and the code exchange, what each token reaches, tokens kept in the
vault rather than the database, renewal (Atlassian's refresh tokens work once), health, and
disconnecting."""

import json
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

from valxel_sdlc.api.app import create_app
from valxel_sdlc.connections import Connections
from valxel_sdlc.oauth_connections import (Client, FileVault, MemoryVault, OAuthError, OAuthTools, clients_from_env,
                                           vault_path)
from valxel_sdlc.web.auth import AuthConfig

WS = "wrkspc-shared"
CLIENTS = {"jira": Client("jira-id", "jira-secret"), "lucid": Client("lucid-id", "lucid-secret"),
           "sentry": Client("sentry-id", "sentry-secret", "valxel"), "pagerduty": Client("pd-id", "pd-secret")}
REDIRECT = "https://valxel.example/api/connections/oauth/callback"


class Resp:
    def __init__(self, body):
        self.body = json.dumps(body).encode()

    def read(self):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeVendors:
    """The four vendors' token endpoints and who-am-I reads. Refresh tokens work once, as Atlassian's do."""

    def __init__(self):
        self.calls, self.n, self.live_refresh, self.revoked = [], 0, set(), False

    def _tokens(self, life=3600):
        self.n += 1
        rt = f"rt-{self.n}"
        self.live_refresh.add(rt)
        return {"access_token": f"at-{self.n}", "refresh_token": rt, "expires_in": life}

    def open(self, req, timeout=None):
        url = urlparse(req.full_url)
        self.calls.append((req.get_method(), url.netloc + url.path))
        if req.get_method() == "POST":
            body = (json.loads(req.data) if req.headers.get("Content-type") == "application/json"
                    else {k: v[0] for k, v in parse_qs(req.data.decode()).items()})
            if body["grant_type"] == "refresh_token":
                if body["refresh_token"] not in self.live_refresh:
                    raise HTTPError(req.full_url, 400, "invalid_grant", {}, None)
                self.live_refresh.discard(body["refresh_token"])
            elif body["code"] != "good-code":
                raise HTTPError(req.full_url, 400, "invalid_grant", {}, None)
            if url.netloc == "identity.pagerduty.com" and body["grant_type"] == "authorization_code":
                assert body["code_verifier"]  # PKCE
            if url.netloc == "sentry.io":
                t = self._tokens()
                return Resp({"token": t["access_token"], "refreshToken": t["refresh_token"],
                             "expiresAt": "2099-01-01T00:00:00Z"})
            return Resp(self._tokens())
        if self.revoked:
            raise HTTPError(req.full_url, 401, "revoked", {}, None)
        if url.path == "/oauth/token/accessible-resources":
            return Resp([{"id": "c1", "url": "https://acme-farm.atlassian.net", "name": "acme-farm",
                          "scopes": ["read:jira-work", "write:jira-work"]}])
        if url.path == "/users/me/profile":
            assert req.headers["Lucid-api-version"] == "1"
            return Resp({"email": "asha@farm.example", "accountId": 42})
        if url.path == "/api/0/organizations/acme-farm/":
            return Resp({"slug": "acme-farm", "name": "Acme Farm"})
        if url.path == "/users/me":
            return Resp({"user": {"html_url": "https://acmefarm.pagerduty.com/users/P1", "email": "asha@farm.example"}})
        raise AssertionError(req.full_url)


class Racy:
    """A vault that another process changed after this one read it."""

    def __init__(self, vault, stale, stale_reads):
        self.vault, self.stale, self.left = vault, stale, stale_reads

    def get(self, path):
        if self.left:
            self.left -= 1
            return dict(self.stale)
        return self.vault.get(path)

    def put(self, path, value):
        self.vault.put(path, value)


class Clock:
    def __init__(self):
        self.t = 1_000_000.0

    def __call__(self):
        return self.t


@pytest.fixture
def vendors():
    return FakeVendors()


@pytest.fixture
def vault():
    return MemoryVault()


@pytest.fixture
def client(engine, vendors, vault):
    svc = engine.services
    oauth = OAuthTools(CLIENTS, redirect_uri=REDIRECT, vault=vault, opener=vendors)
    svc.connections = Connections(svc.workspaces, oauth=oauth, health={t: oauth.health(t) for t in CLIENTS})
    svc.workspaces.bootstrap_owner(WS, {"name": "Asha", "github_login": "asha", "email": "asha@farm.example"})
    with TestClient(create_app(engine, auth=AuthConfig(mode="dev", secret="x" * 40), web_dist=None)) as c:
        c.post("/api/auth/dev-login", json={"login": "asha", "email": "asha@farm.example"})
        yield c


def connect(client, tool, code="good-code", **extra):
    r = client.get(f"/api/workspaces/{WS}/connections/{tool}/install", follow_redirects=False)
    assert r.status_code == 302, r.text
    url = urlparse(r.headers["location"])
    q = parse_qs(url.query)
    if tool == "sentry":
        back = client.get("/api/connections/sentry/setup", follow_redirects=False,
                          params={"code": code, "installationId": "inst-1", "orgSlug": "acme-farm", **extra})
    else:
        back = client.get("/api/connections/oauth/callback", follow_redirects=False,
                          params={"state": q["state"][0], "code": code, **extra})
    return url, q, back


@pytest.mark.parametrize("tool, host, account", [
    ("jira", "auth.atlassian.com", "acme-farm.atlassian.net"),
    ("lucid", "lucid.app", "asha@farm.example"),
    ("sentry", "sentry.io", "acme-farm"),
    ("pagerduty", "identity.pagerduty.com", "acmefarm"),
])
def test_each_tool_connects_through_its_consent_screen(client, vault, tool, host, account):
    url, q, back = connect(client, tool)
    assert url.netloc == host
    if tool != "sentry":
        assert q["client_id"] == [CLIENTS[tool].client_id] and q["redirect_uri"] == [REDIRECT]
    if tool == "jira":
        assert q["audience"] == ["api.atlassian.com"] and "offline_access" in q["scope"][0]
    if tool == "pagerduty":
        assert q["code_challenge_method"] == ["S256"]
    if tool == "sentry":
        assert url.path == "/sentry-apps/valxel/external-install/"
    loc = urlparse(back.headers["location"])
    assert loc.path == f"/workspaces/{WS}" and parse_qs(loc.query) == {"tab": ["connections"], "connected": [account]}
    row = {t["tool"]: t for t in client.get(f"/api/workspaces/{WS}/connections").json()}[tool]
    assert row["status"] == "connected" and row["available"] is True
    [c] = row["connections"]
    assert c["account"] == account and "access_token" not in json.dumps(c)  # tokens stay out of the database
    held = vault.get(vault_path(WS, tool, account))
    assert held["access_token"].startswith("at-") and held["refresh_token"].startswith("rt-")
    assert vault_path(WS, tool, account).startswith(f"valxel/workspaces/{WS}/{tool}/")


def test_a_declined_or_forged_consent_records_nothing(client):
    _, _, back = connect(client, "jira", code="stolen")
    assert "error=" in back.headers["location"]
    back = client.get("/api/connections/oauth/callback", params={"error": "access_denied"}, follow_redirects=False)
    assert "wasn't made" in parse_qs(urlparse(back.headers["location"]).query)["error"][0]
    # A state from another browser (no cookie of its own) is refused.
    r = client.get(f"/api/workspaces/{WS}/connections/lucid/install", follow_redirects=False)
    state = parse_qs(urlparse(r.headers["location"]).query)["state"][0]
    client.cookies.clear()
    client.post("/api/auth/dev-login", json={"login": "asha", "email": "asha@farm.example"})
    back = client.get("/api/connections/oauth/callback", params={"state": state, "code": "good-code"},
                      follow_redirects=False)
    assert "another browser" in parse_qs(urlparse(back.headers["location"]).query)["error"][0]
    rows = client.get(f"/api/workspaces/{WS}/connections").json()
    assert all(not t["connections"] for t in rows)


def test_tokens_renew_once_and_concurrent_renewals_use_the_newest(vendors, vault):
    clock = Clock()
    a = OAuthTools(CLIENTS, redirect_uri=REDIRECT, vault=vault, opener=vendors, clock=clock)
    b = OAuthTools(CLIENTS, redirect_uri=REDIRECT, vault=vault, opener=vendors, clock=clock)  # another process
    path = vault_path(WS, "jira", "acme-farm.atlassian.net")
    vault.put(path, {**vendors._tokens(), "expires_at": clock.t + 3600})
    assert a.access_token(WS, "jira", "acme-farm.atlassian.net") == "at-1"  # not due yet
    clock.t += 3400  # within five minutes of expiry
    stale = vault.get(path)
    assert a.access_token(WS, "jira", "acme-farm.atlassian.net") == "at-2"
    assert vault.get(path)["refresh_token"] == "rt-2"
    # Process b read the vault before a refreshed: its refresh token is spent and refused, so it reads
    # the vault again and goes on from the newest one.
    b.vault = Racy(vault, stale, stale_reads=2)
    clock.t += 3400
    assert b.access_token(WS, "jira", "acme-farm.atlassian.net") == "at-3"


def test_a_revoked_connection_needs_attention(client, engine, vendors):
    connect(client, "pagerduty")
    assert engine.services.connections.check(WS) == []
    vendors.revoked = True
    [c] = engine.services.connections.check(WS)
    assert c["status"] == "needs_attention" and "api.pagerduty.com said 401" in c["last_error"]


def test_disconnecting_deletes_the_tokens(client, vault):
    connect(client, "lucid")
    path = vault_path(WS, "lucid", "asha@farm.example")
    assert vault.get(path)
    r = client.delete(f"/api/workspaces/{WS}/connections/lucid/asha@farm.example", headers={"X-Valxel-Client": "web"})
    assert "Revoke Valxel under App integrations in Lucid" in r.json()["revoke"]
    assert vault.get(path) is None


def test_clients_come_from_the_environment_and_unset_ones_arent_offered():
    env = {"VALXEL_JIRA_OAUTH_CLIENT_ID": "j", "VALXEL_JIRA_OAUTH_CLIENT_SECRET": "js",
           "VALXEL_SENTRY_OAUTH_CLIENT_ID": "s", "VALXEL_SENTRY_OAUTH_CLIENT_SECRET": "ss"}  # Sentry lacks its slug
    assert set(clients_from_env(env)) == {"jira"}
    tools = OAuthTools(clients_from_env(env), redirect_uri=REDIRECT)
    assert tools.available("jira") and not tools.available("lucid")
    with pytest.raises(OAuthError, match="Lucid isn't set up"):
        tools.start(None, workspace=WS, tool="lucid", actor="asha")


def test_the_file_vault_keeps_tokens_private(tmp_path):
    v = FileVault(tmp_path)
    v.put("valxel/workspaces/w/jira/site", {"access_token": "a"})
    f = tmp_path / "valxel/workspaces/w/jira/site.json"
    assert v.get("valxel/workspaces/w/jira/site") == {"access_token": "a"} and oct(f.stat().st_mode & 0o777) == "0o600"
    v.delete("valxel/workspaces/w/jira/site")
    assert v.get("valxel/workspaces/w/jira/site") is None

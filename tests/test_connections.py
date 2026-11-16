"""A workspace's connections (the sign-in design, A5 and A6): installing Valxel's GitHub Apps from
the connections page, checking each install is the installer's own, what a product still lacks,
the hourly health check, and a self-serve feature waiting for GitHub before it touches the repos."""

import json
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

from valxel_sdlc.api.app import create_app
from valxel_sdlc.connections import (ConnectionError_, Connections, GitHubAppSettings, GitHubInstalls,
                                     connection_wait)
from valxel_sdlc.web.auth import AuthConfig

WS = "wrkspc-shared"  # FARM and SHOP's workspace
APPS = {"delivery": GitHubAppSettings(slug="valxel-delivery", app_id="101", client_id="Iv1.d", client_secret="ds"),
        "provisioner": GitHubAppSettings(slug="valxel-provisioning", app_id="202", client_id="Iv1.p", client_secret="ps")}


class Resp:
    def __init__(self, body):
        self.body = json.dumps(body).encode()

    def read(self):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeGitHub:
    """github.com's code exchange, and the API's view of the installing person's installations."""

    def __init__(self):
        self.installs = {7001: {"id": 7001, "app_id": 101, "account": {"login": "acme-farm", "type": "Organization"},
                                "repository_selection": "selected", "permissions": {"contents": "write"}},
                         7002: {"id": 7002, "app_id": 202, "account": {"login": "acme-farm", "type": "Organization"},
                                "repository_selection": "all", "permissions": {"administration": "write"}}}
        self.repos = {7001: ["acme-farm/farm-field-api", "acme-farm/farm-web"]}
        self.codes = {"code-asha": "ghu_asha"}
        self.mine = {"ghu_asha": [7001, 7002]}  # the installations each person can see

    def open(self, req, timeout=None):
        url = urlparse(req.full_url)
        if url.path == "/login/oauth/access_token":
            form = parse_qs(req.data.decode())
            assert form["client_id"][0] in ("Iv1.d", "Iv1.p")
            return Resp({"access_token": self.codes.get(form["code"][0], "")})
        token = req.headers["Authorization"].split(" ", 1)[1]
        if url.path == "/user/installations":
            return Resp({"installations": [self.installs[i] for i in self.mine.get(token, [])]})
        if url.path.startswith("/user/installations/") and url.path.endswith("/repositories"):
            iid = int(url.path.split("/")[3])
            assert iid in self.mine.get(token, [])
            return Resp({"repositories": [{"full_name": r} for r in self.repos.get(iid, [])]})
        raise AssertionError(req.full_url)


@pytest.fixture
def gh():
    return FakeGitHub()


@pytest.fixture
def client(engine, gh):
    svc = engine.services
    svc.connections = Connections(svc.workspaces, github=GitHubInstalls(APPS, api="https://api.github.com",
                                                                         web="https://github.com", opener=gh))
    svc.workspaces.bootstrap_owner(WS, {"name": "Asha", "github_login": "asha", "email": "asha@farm.example"})
    with TestClient(create_app(engine, auth=AuthConfig(mode="dev", secret="x" * 40), web_dist=None)) as c:
        c.post("/api/auth/dev-login", json={"login": "asha", "email": "asha@farm.example"})
        yield c


def install(client, tool="github", installation_id=7001, code="code-asha"):
    r = client.get(f"/api/workspaces/{WS}/connections/{tool}/install", follow_redirects=False)
    assert r.status_code == 302, r.text
    url = urlparse(r.headers["location"])
    state = parse_qs(url.query)["state"][0]
    back = client.get("/api/connections/github/callback", params={"state": state, "code": code,
                      "installation_id": installation_id, "setup_action": "install"}, follow_redirects=False)
    return url, back


def test_installing_both_apps_records_what_each_can_reach(client, engine):
    url, back = install(client)
    assert url.netloc == "github.com" and url.path == "/apps/valxel-delivery/installations/new"
    assert back.headers["location"] == f"/workspaces/{WS}?tab=connections&connected=acme-farm"
    _, back = install(client, "github-provisioning", 7002)
    assert "connected=acme-farm" in back.headers["location"]
    page = {t["tool"]: t for t in client.get(f"/api/workspaces/{WS}/connections").json()}
    gh_row = page["github"]
    assert gh_row["status"] == "connected" and gh_row["available"] is True
    [c] = gh_row["connections"]
    assert c["reach"]["selection"] == "selected" and c["reach"]["repos"] == ["acme-farm/farm-field-api", "acme-farm/farm-web"]
    assert c["connected_by"] == "asha" and c["reach"]["installation_id"] == 7001
    assert page["github-provisioning"]["connections"][0]["reach"]["selection"] == "all"
    assert page["aws"]["status"] == "not_connected" and page["aws"]["available"] is False
    actions = [a["action"] for a in client.get(f"/api/workspaces/{WS}/audit").json()]
    assert actions.count("connection.add") == 2
    install(client)  # again: a reconnect, not a second row
    assert len(client.get(f"/api/workspaces/{WS}/connections").json()[0]["connections"]) == 1
    assert "connection.reconnect" in [a["action"] for a in client.get(f"/api/workspaces/{WS}/audit").json()]


@pytest.mark.parametrize("change, why", [
    ({"installation_id": 9999}, "isn't one you can see"),           # someone else's installation
    ({"installation_id": 7002}, "of another GitHub App"),           # the Provisioning App's, for Delivery
    ({"code": ""}, "didn't confirm who installed"),                 # the App doesn't ask for authorization
    ({"code": "forged"}, "didn't accept"),
])
def test_an_install_that_isnt_the_installers_own_is_refused(client, change, why):
    kw = {"installation_id": 7001, "code": "code-asha", **change}
    _, back = install(client, **kw)
    loc = back.headers["location"]
    assert "error=" in loc and why in parse_qs(urlparse(loc).query)["error"][0]
    assert client.get(f"/api/workspaces/{WS}/connections").json()[0]["status"] == "not_connected"


def test_the_state_must_be_this_workspaces_and_this_persons(client, engine):
    r = client.get("/api/connections/github/callback", params={"state": "forged", "code": "code-asha",
                   "installation_id": 7001}, follow_redirects=False)
    assert "expired or was started elsewhere" in parse_qs(urlparse(r.headers["location"]).query)["error"][0]
    state = parse_qs(urlparse(client.get(f"/api/workspaces/{WS}/connections/github/install",
                                         follow_redirects=False).headers["location"]).query)["state"][0]
    # Ben, an admin too, can't finish Asha's install.
    _, token = engine.services.workspaces.invite(WS, {"github_login": "ben"}, role="admin", actor="asha")
    engine.services.workspaces.accept(WS, token, login="ben")
    client.post("/api/auth/dev-login", json={"login": "ben"})
    r = client.get("/api/connections/github/callback", params={"state": state, "code": "code-asha",
                   "installation_id": 7001}, follow_redirects=False)
    assert "Someone else started this install" in parse_qs(urlparse(r.headers["location"]).query)["error"][0]
    r = client.get("/api/connections/github/callback", params={"state": state, "setup_action": "request"},
                   follow_redirects=False)
    assert "note=" in r.headers["location"]  # an org owner must approve; nothing recorded


def test_only_admins_connect_and_disconnect(client, engine):
    install(client)
    _, token = engine.services.workspaces.invite(WS, {"github_login": "vic"}, role="member", actor="asha")
    engine.services.workspaces.accept(WS, token, login="vic")
    client.post("/api/auth/dev-login", json={"login": "vic"})
    assert client.get(f"/api/workspaces/{WS}/connections").status_code == 200  # members see it
    assert client.get(f"/api/workspaces/{WS}/connections/github/install", follow_redirects=False).status_code == 403
    web = {"X-Valxel-Client": "web"}
    assert client.delete(f"/api/workspaces/{WS}/connections/github/acme-farm", headers=web).status_code == 403
    client.post("/api/auth/dev-login", json={"login": "asha", "email": "asha@farm.example"})
    r = client.delete(f"/api/workspaces/{WS}/connections/github/acme-farm", headers=web).json()
    assert "Uninstall the Valxel App in GitHub" in r["revoke"]
    assert client.get(f"/api/workspaces/{WS}/connections").json()[0]["status"] == "not_connected"
    assert client.get(f"/api/workspaces/{WS}/connections/jira/install", follow_redirects=False).status_code == 404


def test_what_a_product_still_lacks(engine, gh):
    c = Connections(engine.services.workspaces)
    assert c.github_missing(WS, ["acme-farm"]) == ["the Valxel GitHub App isn't installed on acme-farm"]
    c.record(WS, "github", "acme-farm", actor="asha", reach={"selection": "selected", "repos": ["acme-farm/farm-web"]})
    assert c.github_missing(WS, ["acme-farm"], ["acme-farm/farm-web", "acme-farm/farm-field-api"]) == [
        "acme-farm/farm-field-api isn't in the GitHub App's repos"]
    c.record(WS, "github", "acme-farm", actor="asha", reach={"selection": "all"})
    assert c.github_missing(WS, ["acme-farm"], ["acme-farm/farm-field-api"]) == []


def test_the_health_check_flags_a_broken_connection_and_emails_admins_once(engine):
    sent, broken = [], {"on": True}
    c = Connections(engine.services.workspaces, notify=lambda to, subject, text: sent.append((to, subject, text)),
                    health={"github": lambda rec: "the GitHub App was uninstalled from acme-farm" if broken["on"] else ""})
    engine.services.workspaces.bootstrap_owner(WS, {"github_login": "asha", "email": "asha@farm.example"})
    c.record(WS, "github", "acme-farm", actor="asha", reach={"selection": "all", "installation_id": 7001})
    assert [x["status"] for x in c.check(WS)] == ["needs_attention"]
    assert c.check(WS) == []  # still broken: no second email
    [(to, subject, text)] = sent
    assert to == "asha@farm.example" and subject == "GitHub needs attention in Valxel" and "uninstalled from acme-farm" in text
    assert c.page(WS)[0]["status"] == "needs_attention"
    broken["on"] = False
    assert [x["status"] for x in c.check(WS)] == ["connected"]
    actions = [a["action"] for a in engine.services.workspaces.store.audit_log(WS)]
    assert "connection.failed" in actions and "connection.recovered" in actions


def test_a_self_serve_feature_waits_for_github_then_carries_on(engine):
    from valxel_sdlc.engine import FeatureEngine  # noqa: F401 - the fixture's engine

    w = engine.services.workspaces
    ws = w.ensure(WS, "asha") or w.get(WS)
    w.store.put("workspace", WS, "", {**(w.get(WS) or {"id": WS}), "self_serve": True})
    engine.services.connections = Connections(w)
    fid = engine.start(product_id="FARM", title="Offline crop log", description="x")["feature_id"]
    for _ in range(10):  # approve the gates before architecture
        idle(engine, fid)
        p = engine.pending(fid)
        if not p or p[0]["type"] != "gate":
            break
        engine.decide(fid, p[0]["interrupt_id"], {"decision": "approve", "approver": "lead"})
    idle(engine, fid)
    [wait] = engine.pending(fid)
    assert wait["type"] == "external" and wait["wait"] == "connection" and wait["phase"] == "architecture"
    assert wait["label"].startswith("Waiting for: connect GitHub (the Valxel GitHub App isn't installed on acme-farm")
    assert not engine.poke(fid)  # still missing
    engine.services.connections.record(WS, "github", "acme-farm", actor="asha", reach={"selection": "all"})
    assert engine.poke(fid)
    idle(engine, fid)
    assert "g4" in [p.get("gate") for p in engine.pending(fid)]  # architecture ran; design may wait at G3 beside it
    assert ws is None or ws


def idle(engine, fid):
    import time

    time.sleep(0.05)
    for _ in range(400):
        if not engine.is_running(fid):
            return
        time.sleep(0.05)


def test_workspaces_set_up_by_hand_never_wait(engine):
    engine.services.connections = Connections(engine.services.workspaces)
    state = {"product_id": "FARM", "feature_id": "FARM-FEAT-1"}
    assert connection_wait(engine.services, state) is None  # no workspace record at all
    engine.services.workspaces.ensure(WS, "asha")
    assert connection_wait(engine.services, state) is None  # a deployment's own workspace


def test_the_app_finds_a_founders_personal_account_install():
    from valxel_sdlc.governance import GovernanceError
    from valxel_sdlc.governance.sources import GitHubAppTokenProvider

    p = GitHubAppTokenProvider("1", "unused")
    calls = []

    def fake(method, path, bearer, body=None):
        calls.append(path)
        if path.startswith("/orgs/"):
            raise GovernanceError("GitHub App GET /orgs/asha/installation failed with 404: Not Found")
        return {"id": 55}

    p._call = fake
    assert p.installation("asha", "jwt") == {"id": 55}
    assert calls == ["/orgs/asha/installation", "/users/asha/installation"]


def test_installs_need_the_apps_settings(engine):
    c = Connections(engine.services.workspaces, github=GitHubInstalls({"delivery": None, "provisioner": None}))
    assert c.page(WS)[0]["available"] is False
    with pytest.raises(ConnectionError_, match="isn't set up"):
        c.github.install_url("github", "s")
    assert GitHubAppSettings.from_env("delivery", env={"VALXEL_GITHUB_APP_SLUG": "x"}) is None
    full = {"VALXEL_GITHUB_APP_SLUG": "valxel", "VALXEL_GITHUB_APP_ID": "1", "VALXEL_GITHUB_APP_CLIENT_ID": "c",
            "VALXEL_GITHUB_APP_CLIENT_SECRET": "s"}
    assert GitHubAppSettings.from_env("delivery", env=full).slug == "valxel"

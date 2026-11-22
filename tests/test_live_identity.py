"""Identity checked when it matters (engineering review finding 5, T-134): GitHub teams at decision
time, the 4-hour sign-in for every decider, and sessions revoked on the server."""

import os
import time

import pytest
from fastapi.testclient import TestClient
from test_api import CLN_TEAMS, SECRET, sign_in, wait_idle

from valxel_sdlc.api.app import create_app
from valxel_sdlc.web.auth import SESSION_COOKIE, AuthConfig, SessionSigner, User, session_payload
from valxel_sdlc.web.sessions import MemorySessions


@pytest.fixture
def client(engine):
    with TestClient(create_app(engine, auth=AuthConfig(mode="dev", secret=SECRET), web_dist=None)) as c:
        yield c


def g1_for_maria(client, engine):
    """A CLN feature at G1, started by someone else (CLN holds PHI: its author doesn't approve it)."""
    sign_in(client, "lena", teams=CLN_TEAMS)
    fid = client.post("/api/features", json={"product_id": "CLN", "title": "Consent check"}).json()["feature_id"]
    wait_idle(engine, fid)
    sign_in(client, "maria", teams=CLN_TEAMS)
    [item] = client.get("/api/inbox?mine=true").json()
    return fid, f"/api/features/{fid}/decisions/{item['interrupt_id']}"


def test_someone_removed_from_the_team_can_no_longer_decide(client, engine):
    _fid, url = g1_for_maria(client, engine)
    engine.services.github.removed_from_team["acme-clinic/product-owners"] = {"maria"}
    r = client.post(url, json={"decision": "approve"})
    assert r.status_code == 403 and "no longer in acme-clinic/product-owners" in r.json()["detail"]
    assert ("acme-clinic", "product-owners", "maria") in engine.services.github.team_checks
    engine.services.github.removed_from_team.clear()  # added back: works at once (a "no" isn't kept)
    assert client.post(url, json={"decision": "approve"}).status_code == 202


def test_when_github_cant_say_the_decision_waits(client, engine):
    _fid, url = g1_for_maria(client, engine)
    gh = engine.services.github
    gh.teams_unknown = True
    r = client.post(url, json={"decision": "approve"})
    assert r.status_code == 503 and "can't confirm" in r.json()["detail"]
    gh.teams_unknown = False
    assert client.post(url, json={"decision": "approve"}).status_code == 202


def test_deciding_through_a_login_asks_github_nothing(client, engine):
    from valxel_sdlc.api.context import make_context

    ctx = make_context(engine, AuthConfig(mode="dev", secret=SECRET), None)
    me = User(login="maria", teams=["acme-clinic/product-owners"], idp="dev", signed_in_at=int(time.time()))
    gh = engine.services.github
    ctx.confirm_decider(me, ["Maria", "acme-clinic/product-owners"])
    assert gh.team_checks == []
    ctx.confirm_decider(me, ["acme-clinic/product-owners"])
    ctx.confirm_decider(me, ["acme-clinic/product-owners"])  # GitHub's "yes" holds for a few minutes
    assert gh.team_checks == [("acme-clinic", "product-owners", "maria")]


def test_every_decider_needs_a_sign_in_from_the_last_four_hours(client, engine):
    _fid, url = g1_for_maria(client, engine)
    old = User(login="maria", orgs=["acme-clinic"], teams=CLN_TEAMS, idp="dev",
               signed_in_at=int(time.time()) - 5 * 3600)
    client.cookies.clear()
    client.cookies.set(SESSION_COOKIE, SessionSigner(SECRET).dumps(session_payload(old)))
    assert client.get("/api/inbox").status_code == 200  # reading is fine: maria has no workspace role
    r = client.post(url, json={"decision": "approve"})
    assert r.status_code == 401 and "sign in again" in r.json()["detail"]


def test_a_copied_cookie_stops_working_after_sign_out(client):
    sign_in(client, "maria", teams=CLN_TEAMS)
    stolen = client.cookies.get(SESSION_COOKIE)
    assert client.post("/api/auth/logout", json={}).status_code == 200
    client.cookies.clear()
    client.cookies.set(SESSION_COOKIE, stolen)
    assert client.get("/api/me").status_code == 401


def test_sign_out_everywhere_ends_every_session(client, engine):
    sign_in(client, "maria", teams=CLN_TEAMS)
    laptop = client.cookies.get(SESSION_COOKIE)
    time.sleep(0.01)
    sign_in(client, "maria", teams=CLN_TEAMS)  # a phone
    assert client.post("/api/auth/logout-everywhere", json={}).status_code == 200
    for cookie in (laptop,):
        client.cookies.clear()
        client.cookies.set(SESSION_COOKIE, cookie)
        assert client.get("/api/me").status_code == 401
    time.sleep(1.1)  # a new sign-in afterwards works
    sign_in(client, "maria", teams=CLN_TEAMS)
    assert client.get("/api/me").status_code == 200


def test_memory_sessions():
    s = MemorySessions()
    now = int(time.time())
    assert not s.revoked({"sid": "a", "login": "Ann", "signed_in_at": now})
    s.revoke("a")
    assert s.revoked({"sid": "a", "login": "ann", "signed_in_at": now})
    assert not s.revoked({"sid": "b", "login": "ann", "signed_in_at": now})
    s.revoke_login("ANN")
    assert s.revoked({"sid": "b", "login": "ann", "signed_in_at": now})
    assert not s.revoked({"sid": "c", "login": "bob", "signed_in_at": now})


@pytest.mark.skipif(not os.environ.get("VALXEL_TEST_DATABASE_URL"), reason="needs VALXEL_TEST_DATABASE_URL")
def test_postgres_sessions():
    from psycopg.rows import dict_row
    from psycopg_pool import ConnectionPool

    from valxel_sdlc.web.sessions import PostgresSessions

    with ConnectionPool(os.environ["VALXEL_TEST_DATABASE_URL"], open=True,
                        kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row}) as pool:
        check_postgres_sessions(PostgresSessions(pool))


def check_postgres_sessions(s):
    now = int(time.time())
    login = f"ann-{now}"
    assert not s.revoked({"sid": f"s-{now}", "login": login, "signed_in_at": now})
    s.revoke(f"s-{now}", login)
    assert s.revoked({"sid": f"s-{now}", "login": login, "signed_in_at": now})
    assert not s.revoked({"sid": f"t-{now}", "login": login, "signed_in_at": now})
    s.revoke_login(login.upper())
    assert s.revoked({"sid": f"t-{now}", "login": login, "signed_in_at": now})
    assert not s.revoked({"sid": "", "login": login, "signed_in_at": now + 5})


def test_the_cli_revokes(capsys):
    from valxel_sdlc.cli import _revoke_sessions

    s = MemorySessions()
    assert _revoke_sessions("ann", s) == 0 and "Signed ann out everywhere" in capsys.readouterr().out
    assert s.revoked({"login": "ann", "signed_in_at": int(time.time())})

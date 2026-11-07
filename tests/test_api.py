import json
import time
from io import BytesIO
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

from vrixel_sdlc.api.app import create_app
from vrixel_sdlc.web.auth import AuthConfig, GitHubOAuth

SECRET = "x" * 40
CLN_TEAMS = ["acme-clinic/product-owners", "acme-clinic/architects", "acme-clinic/design",
             "acme-clinic/tech-leads", "acme-clinic/release-managers"]


def wait_idle(engine, fid, timeout=10.0):
    end = time.time() + timeout
    while time.time() < end:
        if fid not in engine.running:
            try:
                v = engine.view(fid)
                if v["pending"] or v["status"] in ("done", "aborted"):
                    return v
            except KeyError:
                pass
        time.sleep(0.02)
    raise TimeoutError(f"{fid} did not settle")


@pytest.fixture
def client(engine):
    app = create_app(engine, auth=AuthConfig(mode="dev", secret=SECRET), web_dist=None)
    with TestClient(app) as c:
        yield c


def sign_in(client, login, teams=(), orgs=()):
    r = client.post("/api/auth/dev-login", json={"login": login, "teams": list(teams), "orgs": list(orgs)})
    assert r.status_code == 200
    return r


def test_api_requires_sign_in(client):
    assert client.get("/api/features").status_code == 401
    assert client.get("/api/healthz").json()["auth"] == "dev"


def test_only_listed_approvers_can_decide_and_decisions_record_the_signed_in_user(client, engine):
    sign_in(client, "maria", teams=CLN_TEAMS)
    r = client.post("/api/features", json={"product_id": "CLN", "title": "Consent check on intake"})
    assert r.status_code == 201
    fid = r.json()["feature_id"]
    wait_idle(engine, fid)

    [item] = client.get("/api/inbox?mine=true").json()
    assert item["gate"] == "g1" and item["can_decide"] and item["approvers"] == ["acme-clinic/product-owners"]

    sign_in(client, "dev-without-teams", orgs=["acme-clinic"])
    assert client.get("/api/inbox?mine=true").json() == []
    [seen] = client.get("/api/inbox").json()
    assert seen["can_decide"] is False
    denied = client.post(f"/api/features/{fid}/decisions/{item['interrupt_id']}", json={"decision": "approve"})
    assert denied.status_code == 403

    sign_in(client, "maria", teams=CLN_TEAMS)
    ok = client.post(f"/api/features/{fid}/decisions/{item['interrupt_id']}",
                     json={"decision": "approve", "active_minutes": 4, "approver": "someone-else"})
    assert ok.status_code == 202
    v = wait_idle(engine, fid)
    assert v["gate_decisions"][0]["approver"] == "maria"  # the server records the signed-in user
    assert [p["gate"] for p in v["pending"]] == ["g2"]


def test_intents_need_membership_of_the_lane_org(client):
    sign_in(client, "farm-person", orgs=["acme-farm"])
    me = client.get("/api/me").json()
    assert [lane["id"] for lane in me["lanes"]] == ["FARM"]
    r = client.post("/api/features", json={"product_id": "CLN", "title": "Not my product"})
    assert r.status_code == 403


def test_board_and_feature_detail_show_the_rail(client, engine):
    sign_in(client, "maria", teams=CLN_TEAMS)
    fid = client.post("/api/features", json={"product_id": "CLN", "title": "Allergy list"}).json()["feature_id"]
    wait_idle(engine, fid)
    [row] = client.get("/api/features").json()
    assert row["feature_id"] == fid and row["created_by"] == "maria"
    states = {s["key"]: s["state"] for s in row["rail"]}
    assert states["discovery"] == "done" and states["g1"] == "waiting" and states["spec"] == "todo"
    detail = client.get(f"/api/features/{fid}").json()
    assert detail["pending"][0]["can_decide"] is True
    opp = client.get(f"/api/features/{fid}/artifacts/OPP").json()
    assert opp["content"].startswith("# Opportunity brief: Allergy list")


def test_guided_intake_conversation_submits_a_feature(client, engine):
    sign_in(client, "priya", orgs=["acme-farm", "acme-shop"])
    s = client.post("/api/intake", json={}).json()
    assert s["messages"][0]["role"] == "agent" and s["lanes"] == ["FARM", "SHOP"]

    s = client.post(f"/api/intake/{s['id']}/messages",
                    json={"text": "Farmers need to log field observations while offline."}).json()
    assert s["draft"]["problem"].startswith("Farmers need") and s["draft"]["title"]
    assert "Which product" in s["messages"][-1]["text"] and s["draft"]["product_id"] == ""

    s = client.post(f"/api/intake/{s['id']}/messages", json={"text": "It's for Farm app"}).json()
    assert s["draft"]["product_id"] == "FARM" and "Who will use" in s["messages"][-1]["text"]
    s = client.post(f"/api/intake/{s['id']}/messages", json={"text": "Smallholder farmers on low-end phones"}).json()
    assert not s["ready"]
    early = client.post(f"/api/intake/{s['id']}/submit", json={})
    assert early.status_code == 422 and "success_metric" in early.json()["detail"]

    s = client.post(f"/api/intake/{s['id']}/messages", json={"text": "60% of field visits logged within a week"}).json()
    assert s["ready"] and "Submit it when it looks right" in s["messages"][-1]["text"]

    s = client.patch(f"/api/intake/{s['id']}/draft", json={"title": "Offline field observations"}).json()
    assert s["draft"]["title"] == "Offline field observations"
    assert client.patch(f"/api/intake/{s['id']}/draft", json={"product_id": "CLN"}).status_code == 403

    s = client.post(f"/api/intake/{s['id']}/submit", json={}).json()
    fid = s["feature_id"]
    assert fid.startswith("FARM-FEAT-")
    v = wait_idle(engine, fid)
    assert v["title"] == "Offline field observations" and v["created_by"] == "priya"
    assert "Success measure: 60% of field visits" in v["description"]
    assert client.post(f"/api/intake/{s['id']}/submit", json={}).status_code == 409

    sign_in(client, "someone-else", orgs=["acme-farm"])
    assert client.get(f"/api/intake/{s['id']}").status_code == 404  # conversations are private


def test_intake_stops_a_duplicate_until_the_creator_says_how_it_differs(client, engine):
    sign_in(client, "maria", orgs=["acme-clinic"])
    first = engine.create(product_id="CLN", title="Faster patient intake for walk-ins",
                          description="Front desk needs faster patient intake for walk-ins")
    s = client.post("/api/intake", json={}).json()
    s = client.post(f"/api/intake/{s['id']}/messages",
                    json={"text": "Front desk needs faster patient intake for walk-ins"}).json()
    assert s["draft"]["product_id"] == "CLN"  # only one lane: chosen without asking
    for text in ("Front-desk staff at busy clinics", "Check-in time under 3 minutes"):
        s = client.post(f"/api/intake/{s['id']}/messages", json={"text": text}).json()
    assert s["draft"]["similar"][0]["id"] == first and s["draft"]["similar"][0]["duplicate"]
    assert f"This looks like {first}" in s["messages"][-1]["text"] and s["ready"] is False
    r = client.post(f"/api/intake/{s['id']}/submit", json={})
    assert r.status_code == 409 and r.json()["detail"]["similar"][0]["id"] == first

    s = client.post(f"/api/intake/{s['id']}/messages",
                    json={"text": "This one is for walk-ins without an appointment; the other is pre-registration"}).json()
    assert s["ready"] is True and "without an appointment" in s["messages"][-1]["text"]
    s = client.post(f"/api/intake/{s['id']}/submit", json={}).json()
    v = wait_idle(engine, s["feature_id"])
    assert "Not a duplicate because: This one is for walk-ins" in v["description"]
    g1 = next(p for p in v["pending"] if p.get("gate") == "g1")
    assert g1["related_features"][0]["id"] == first and "walk-ins" in g1["not_duplicate_reason"]


def test_creating_a_duplicate_feature_needs_a_reason(client, engine):
    sign_in(client, "maria", orgs=["acme-clinic"])
    body = {"product_id": "CLN", "title": "Lab results viewer", "description": "Show lab results to clinicians"}
    first = client.post("/api/features", json=body).json()["feature_id"]
    wait_idle(engine, first)
    again = client.post("/api/features", json=body)
    assert again.status_code == 409 and again.json()["detail"]["similar"][0]["id"] == first
    ok = client.post("/api/features", json={**body, "not_duplicate_reason": "For patients, not clinicians"})
    assert ok.status_code == 201
    unrelated = client.post("/api/features", json={"product_id": "CLN", "title": "Referral triage",
                                                   "description": "Route incoming referrals to the right clinic"})
    assert unrelated.status_code == 201


def test_mutations_must_be_json(client):
    sign_in(client, "maria", teams=CLN_TEAMS)
    r = client.post("/api/intake", content="x", headers={"content-type": "application/x-www-form-urlencoded"})
    assert r.status_code == 415


def test_events_stream_reports_feature_progress(engine):
    import asyncio

    async def collect():
        q = engine.bus.subscribe()
        await asyncio.get_running_loop().run_in_executor(
            None, lambda: engine.create(product_id="FARM", title="Rain gauge import"))
        out = []
        while not q.empty():
            out.append(q.get_nowait())
        engine.bus.unsubscribe(q)
        return out

    events = asyncio.run(collect())
    kinds = [e["type"] for e in events]
    assert kinds[0] == "feature.created" and kinds[-1] == "feature.updated"
    assert {"intake", "discovery"} <= {e.get("node") for e in events if e["type"] == "feature.node"}


class FakeResp(BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeGitHub:
    def __init__(self):
        self.calls = []

    def open(self, req, timeout=None):
        url = req.full_url
        self.calls.append((req.get_method(), url))
        if url.endswith("/login/oauth/access_token"):
            body = parse_qs(req.data.decode())
            assert body["code"] == ["the-code"]
            return FakeResp(json.dumps({"access_token": "gho_abc"}).encode())
        assert req.headers["Authorization"] == "Bearer gho_abc"
        if url.endswith("/user"):
            return FakeResp(json.dumps({"login": "maria", "name": "Maria", "avatar_url": "a"}).encode())
        if "/user/orgs" in url:
            return FakeResp(json.dumps([{"login": "acme-clinic"}]).encode())
        if "/user/teams" in url:
            return FakeResp(json.dumps([{"slug": "architects", "organization": {"login": "acme-clinic"}}]).encode())
        raise AssertionError(url)


def test_github_sign_in_flow(engine):
    cfg = AuthConfig(mode="github", secret=SECRET, client_id="cid", client_secret="cs",
                     public_url="https://sdlc.vrixel.dev")
    fake = FakeGitHub()
    app = create_app(engine, auth=cfg, oauth=GitHubOAuth(cfg, opener=fake), web_dist=None)
    with TestClient(app, base_url="https://sdlc.vrixel.dev") as client:
        assert client.post("/api/auth/dev-login", json={"login": "x"}).status_code == 404
        r = client.get("/api/auth/login", follow_redirects=False)
        q = parse_qs(urlparse(r.headers["location"]).query)
        assert q["scope"] == ["read:org"] and q["redirect_uri"] == ["https://sdlc.vrixel.dev/api/auth/callback"]

        bad = client.get("/api/auth/callback?code=the-code&state=forged", follow_redirects=False)
        assert bad.status_code == 400  # state must match the cookie set at login

        r = client.get(f"/api/auth/callback?code=the-code&state={q['state'][0]}", follow_redirects=False)
        assert r.status_code == 302 and "secure" in r.headers["set-cookie"].lower()
        me = client.get("/api/me").json()
        assert me["login"] == "maria" and me["teams"] == ["acme-clinic/architects"]
        assert [lane["id"] for lane in me["lanes"]] == ["CLN"]
        assert "gho_abc" not in r.headers["set-cookie"]  # the GitHub token is never stored


def test_tampered_session_cookies_are_rejected(client):
    sign_in(client, "maria", teams=CLN_TEAMS)
    token = client.cookies.get("vrixel_session")
    # Change the first character of the signature: the last one of a base64 string can carry
    # unused bits, so changing it doesn't always change the signature.
    payload, sig = token.rsplit(".", 1)
    for forged in (f"{payload}.{'B' if sig[0] != 'B' else 'C'}{sig[1:]}", f"{payload[:-2]}xx.{sig}"):
        # Replace the cookie outright: setting it again can add a second cookie for another
        # domain, and some httpx versions send the original one.
        client.cookies.clear()
        assert client.get("/api/me", headers={"Cookie": f"vrixel_session={forged}"}).status_code == 401

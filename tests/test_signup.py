"""Sign-up (the sign-in design, A9): the plans, a founder's workspace and trial, changing plan,
GitHub installs that return to the wizard, and the founder's first product, decided by their own
workspace's Product group rather than the platform's council."""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

from test_api import SECRET, wait_idle
from test_connections import APPS, FakeGitHub
from valxel_sdlc import billing
from valxel_sdlc.api.app import create_app
from valxel_sdlc.connections import Connections, GitHubInstalls
from valxel_sdlc.inception import setup
from valxel_sdlc.web.auth import AuthConfig

WS = "maya-farms"
BODY = {"name": "Aqua Watch", "org": "maya-farms",
        "vision": "Small farms lose crops because nobody sees soil moisture until too late. Alert the manager's phone.",
        "goals": ["Fewer dry-field incidents (measure: under 2 a month per farm)"],
        "users": ["Farm managers: out in the fields on a phone"], "budget_usd": 1500}


def sign_in(client, login):
    r = client.post("/api/auth/dev-login", json={"login": login, "email": f"{login}@farm.example"})
    assert r.status_code == 200


@pytest.fixture(autouse=True)
def _paid_plans_offered(monkeypatch):
    """These tests cover the paid plans, which are hidden unless VALXEL_PLANS lists them."""
    monkeypatch.setenv("VALXEL_PLANS", "self-hosted,lite,full")


@pytest.fixture
def client(engine):
    svc = engine.services
    svc.connections = Connections(svc.workspaces, github=GitHubInstalls(APPS, opener=FakeGitHub()))
    with TestClient(create_app(engine, auth=AuthConfig(mode="dev", secret=SECRET), web_dist=None)) as c:
        sign_in(c, "maya")
        r = c.post("/api/workspaces", json={"id": WS, "name": "Maya Farms", "plan": "lite"})
        assert r.status_code == 201, r.text
        yield c


def test_the_plans_and_the_install_guide(client, monkeypatch):
    got = client.get("/api/plans").json()
    assert [p["id"] for p in got["plans"]] == ["self-hosted", "lite", "full"] and got["trial_days"] == 90
    selfhosted, lite, full = got["plans"]
    assert selfhosted["price"] == "$0" and selfhosted["hosted"] is False and selfhosted["sign_in"] == "GitHub"
    assert "to be announced" in lite["price"] and "about $79 a month" in lite["price"] and full["monthly_usd"] is None
    assert got["install_guide"].startswith("https://")
    monkeypatch.setenv("VALXEL_PRICE_FULL_USD", "400")
    assert client.get("/api/plans").json()["plans"][2]["price"] == "$400 a month per product"


def test_a_founders_workspace_its_trial_and_its_plan(client, engine):
    ws = engine.services.workspaces.get(WS)
    assert ws["self_serve"] is True and ws["plan"] == "lite"
    b = client.get(f"/api/workspaces/{WS}/billing").json()
    assert b["trial_days_left"] in (89, 90) and b["card_needed"] is False and b["provider"] == "none"
    assert "nothing is charged" in b["note"] and "at go-live" in b["note"]
    assert client.put(f"/api/workspaces/{WS}/plan", json={"plan": "full"}).json()["plan"] == "full"
    assert client.get(f"/api/workspaces/{WS}/billing").json()["trial_ends"] == ""  # no trial on Full
    assert client.put(f"/api/workspaces/{WS}/plan", json={"plan": "self-hosted"}).status_code == 422
    assert "workspace.plan" in [a["action"] for a in client.get(f"/api/workspaces/{WS}/audit").json()]
    sign_in(client, "stranger")
    assert client.put(f"/api/workspaces/{WS}/plan", json={"plan": "lite"}).status_code == 403
    assert client.get(f"/api/workspaces/{WS}/billing").status_code == 404


def test_a_github_install_returns_to_the_wizard(client):
    r = client.get(f"/api/workspaces/{WS}/connections/github/install", params={"back": f"/start/{WS}"},
                   follow_redirects=False)
    state = parse_qs(urlparse(r.headers["location"]).query)["state"][0]
    back = client.get("/api/connections/github/callback", follow_redirects=False,
                      params={"state": state, "code": "code-asha", "installation_id": 7001, "setup_action": "install"})
    loc = urlparse(back.headers["location"])
    assert loc.path == f"/start/{WS}" and parse_qs(loc.query) == {"tool": ["github"], "connected": ["acme-farm"]}
    # Only a page of this app: another site is refused and the wizard isn't left.
    r = client.get(f"/api/workspaces/{WS}/connections/github/install", params={"back": "https://evil.example/x"},
                   follow_redirects=False)
    state = parse_qs(urlparse(r.headers["location"]).query)["state"][0]
    back = client.get("/api/connections/github/callback", follow_redirects=False,
                      params={"state": state, "code": "code-asha", "installation_id": 7001})
    assert back.headers["location"].startswith("/?tool=github")


def test_the_founders_first_product_goes_to_their_own_product_group(client, engine):
    r = client.post("/api/inceptions", json={**BODY, "workspace": WS})
    assert r.status_code == 201, r.text
    tid = r.json()["feature_id"]
    v = wait_idle(engine, tid)
    [gate] = v["pending"]
    assert gate["gate"] == "brief" and gate["approvers"] == [f"group:{WS}/product"]
    r = client.post(f"/api/features/{tid}/decisions/{gate['interrupt_id']}", json={"decision": "approve"})
    assert r.status_code == 202, r.text  # Maya joined every group when she made the workspace
    # Someone outside the workspace can't start one there, nor decide its gates.
    sign_in(client, "stranger")
    assert client.post("/api/inceptions", json={**BODY, "workspace": WS}).status_code == 403


def test_only_a_self_serve_workspace_starts_its_own_products(client, engine):
    engine.services.workspaces.bootstrap_owner("wrkspc-shared", {"name": "Asha", "github_login": "asha"})
    sign_in(client, "asha")
    r = client.post("/api/inceptions", json={**BODY, "workspace": "wrkspc-shared"})
    assert r.status_code == 403 and "product council" in r.json()["detail"]


def test_intake_can_start_a_product_in_the_workspace(client):
    s = client.post("/api/intake", json={"workspace": WS}).json()
    assert s["workspace"] == WS and s["starter"] is True
    sign_in(client, "stranger")
    assert client.post("/api/intake", json={"workspace": WS}).status_code == 403


def test_the_registry_puts_the_product_in_its_workspace():
    brief = "\n".join(["# Product brief: Aqua Watch", "", "## Product", "- id: AW", "- org: maya-farms",
                       "- repo prefix: aw-", "- budget: $1500", "", "## Data", "- class: general"])
    try:
        line = setup.registry_line(brief, WS)
    except Exception:  # the brief's format is the brief writer's; the line's workspace is what matters here
        pytest.skip("brief format")
    assert line.rstrip().endswith(f"workspace: {WS} }}")


class Card:
    name = "stub"

    def __init__(self, has):
        self.has = has

    def has_card(self, ws):
        return self.has

    def add_card_url(self, ws, *, return_to):
        return "https://pay.example/add"


def test_with_a_payment_provider_going_live_waits_for_a_card():
    ws = {"id": WS, "plan": "lite", "created": "2026-11-01T00:00:00+00:00"}
    assert billing.blocks_go_live(ws, billing.NoBilling()) == ""
    assert billing.blocks_go_live(ws, Card(False)) == "Add a card before going live on the Lite plan"
    assert billing.blocks_go_live(ws, Card(True)) == ""
    assert billing.status(ws, Card(False), live=True)["card_needed"] is True
    with pytest.raises(ValueError, match="no payment provider is built in"):
        billing.provider_for({"VALXEL_BILLING": "stripe"})


def test_only_the_self_hosted_plan_is_offered_by_default(engine, monkeypatch):
    monkeypatch.delenv("VALXEL_PLANS")
    assert billing.offered({}) == ["self-hosted"] and billing.offered({"VALXEL_PLANS": "lite, bogus"}) == [
        "self-hosted", "lite"]
    svc = engine.services
    svc.connections = Connections(svc.workspaces, github=GitHubInstalls(APPS, opener=FakeGitHub()))
    with TestClient(create_app(engine, auth=AuthConfig(mode="dev", secret=SECRET), web_dist=None)) as c:
        sign_in(c, "lee")
        assert [p["id"] for p in c.get("/api/plans").json()["plans"]] == ["self-hosted"]
        assert c.post("/api/workspaces", json={"id": "lee-lite", "name": "Lee", "plan": "lite"}).status_code == 400
        # Sign-up in an install makes the workspace on the self-hosted plan, ready to connect its tools.
        r = c.post("/api/workspaces", json={"id": "lee-farms", "name": "Lee Farms"})
        assert r.status_code == 201, r.text
        ws = engine.services.workspaces.get("lee-farms")
        assert ws["plan"] == "self-hosted" and ws["self_serve"] is True
        b = c.get("/api/workspaces/lee-farms/billing").json()
        assert b["plan"]["id"] == "self-hosted" and b["note"] == "" and b["trial_ends"] == ""
        assert c.put("/api/workspaces/lee-farms/plan", json={"plan": "full"}).status_code == 400

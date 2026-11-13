"""The experience round at intake: asked only when an intent changes what people see, each answer
optional, "you decide" recorded as such, and the brief reaching the design agent and its critic."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from test_api import SECRET, sign_in, wait_idle
from valxel_sdlc.api.app import create_app
from valxel_sdlc.evaluators import rubrics
from valxel_sdlc.graph.phase import document_tail
from valxel_sdlc.intake import DELEGATED, Draft, IntakeAgent
from valxel_sdlc.web.auth import AuthConfig


@pytest.fixture
def client(engine):
    app = create_app(engine, auth=AuthConfig(mode="dev", secret=SECRET), web_dist=None)
    with TestClient(app) as c:
        yield c


def say(client, sid, text):
    return client.post(f"/api/intake/{sid}/messages", json={"text": text}).json()


def test_only_changes_people_see_in_products_with_an_app(services):
    agent = IntakeAgent(services)
    assert agent.touches_ui(Draft(product_id="FARM", problem="Managers need an alert on the dashboard"))
    assert not agent.touches_ui(Draft(product_id="FARM", problem="Nightly export of readings to the data lake"))
    assert not agent.touches_ui(Draft(product_id="FARM", problem="It's for Farm app"))  # the product's name
    assert not agent.touches_ui(Draft(product_id="SS", problem="Show audit records on a screen"))  # no app


def test_the_round_asks_offers_choices_and_writes_the_brief(client, engine):
    sign_in(client, "priya", orgs=["acme-farm"])
    sid = client.post("/api/intake", json={}).json()["id"]
    for text in ("Farm managers miss dry fields; they need an alert on the dashboard.",
                 "Managers of farms with irrigation sensors", "Fewer than 2 dry fields a month per farm"):
        s = say(client, sid, text)
    assert s["draft"]["ui"] and s["missing"] == ["experience"] and not s["ready"]
    assert "Which screen or flow" in s["messages"][-1]["text"]

    s = say(client, sid, "The farm dashboard")
    assert "one thing a person must be able to do" in s["messages"][-1]["text"]
    s = say(client, sid, "See which fields are dry right now")
    assert s["messages"][-1]["options"][:3] == ["On a phone, on the move", "At a desk", "Both"]
    s = say(client, sid, "On a phone, on the move")
    assert "Follow the current design" in s["messages"][-1]["options"]
    s = say(client, sid, "Follow the current design, but make dry fields red like the soil map")
    assert s["ready"] and "work from what you said about screen or flow, main task" in s["messages"][-1]["text"]
    assert s["draft"]["experience"]["where"] == "The farm dashboard"

    fid = client.post(f"/api/intake/{sid}/submit", json={}).json()["feature_id"]
    v = wait_idle(engine, fid)
    assert "Experience brief:\n- Screen or flow: The farm dashboard" in v["description"]
    assert "- Look: Follow the current design, but make dry fields red" in v["description"]


def test_you_decide_leaves_it_to_the_design_agent_and_says_so(client):
    sign_in(client, "priya", orgs=["acme-farm"])
    sid = client.post("/api/intake", json={}).json()["id"]
    for text in ("Managers want a screen listing fields by moisture.", "Farm managers", "Checks take half the time"):
        s = say(client, sid, text)
    s = say(client, sid, "The fields list")
    s = say(client, sid, "You decide the rest")
    assert s["ready"] and s["draft"]["experience"] == {
        "where": "The fields list", "main_task": DELEGATED, "context": DELEGATED, "look": DELEGATED}


def test_people_can_turn_the_round_off_or_fill_it_in_themselves(client):
    sign_in(client, "priya", orgs=["acme-farm"])
    sid = client.post("/api/intake", json={}).json()["id"]
    for text in ("Managers want an alert screen for dry fields.", "Farm managers", "Fewer dry fields"):
        s = say(client, sid, text)
    assert s["missing"] == ["experience"]
    s = client.patch(f"/api/intake/{sid}/draft", json={"experience": {"where": "Alerts", "main_task": "See dry fields",
                                                                      "context": "Phone", "look": "Current design"}}).json()
    assert s["ready"]
    s = client.patch(f"/api/intake/{sid}/draft", json={"ui": False}).json()
    assert s["ready"] and not s["draft"]["ui"]
    assert client.patch(f"/api/intake/{sid}/draft", json={"experience": {"colour": "red"}}).status_code == 422


def test_the_design_agent_and_its_critic_work_from_the_brief():
    brief = "Problem\n\nExperience brief:\n- Screen or flow: The farm dashboard"
    assert rubrics.BRIEF in rubrics.for_request("design", brief)
    assert rubrics.BRIEF not in rubrics.for_request("design", "Problem only")
    assert rubrics.BRIEF not in rubrics.for_request("spec", brief)
    tail = document_tail("design", {"feature_id": "F-1", "title": "t", "description": brief})
    assert "Follow it" in tail and "'Traded off:'" in tail
    assert "Follow it" not in document_tail("design", {"feature_id": "F-1", "title": "t", "description": "x"})

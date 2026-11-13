"""The one front door: the intake chat sizes a request as a feature, an initiative or a new product,
the person confirms or switches, and each size asks for what its flow needs and starts it."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from test_api import SECRET, sign_in, wait_idle
from valxel_sdlc.api.app import create_app
from valxel_sdlc.intake import AGENT_PROPOSES, Draft, research_said, size_of, size_said
from valxel_sdlc.web.auth import AuthConfig


@pytest.fixture
def client(engine):
    app = create_app(engine, auth=AuthConfig(mode="dev", secret=SECRET), web_dist=None)
    with TestClient(app) as c:
        yield c


def say(client, sid, text):
    r = client.post(f"/api/intake/{sid}/messages", json={"text": text})
    assert r.status_code == 200, r.text
    return r.json()


def start(client, login="maria", **kw):
    sign_in(client, login, **kw)
    return client.post("/api/intake", json={}).json()


def test_sizing_guesses_from_the_words():
    assert size_of("Add a CSV export to the visits list")[0] == "feature"
    assert size_of("Cut check-in time to 2 minutes by end of Q1")[0] == "initiative"
    assert size_of("Patients check in on their phone; staff see who's ready; reminders carry the link")[0] == "initiative"
    assert size_of("A booking app for small vet practices")[0] == "product"
    assert size_of("A booking app for small vet practices", lane="CLN")[0] != "product"  # names a product you have
    assert size_said("Plan it as an initiative") == "initiative"
    assert size_said("just one feature for now") == "feature"
    assert size_said("make it a new product") == "product"
    assert size_said("Managers of farms") == ""
    assert research_said("I've done my own") == "own" and research_said("Thorough research") == "thorough"
    assert research_said("Quick web research") == "quick"


def test_a_draft_needs_what_its_size_needs():
    assert Draft(problem="x").missing()[0] == "size"
    d = Draft(size="initiative", product_id="CLN", problem="x", success_metric="y")
    assert d.missing() == ["horizon", "research"]
    d.horizon, d.research = "now", "quick"
    assert d.missing() == []
    assert Draft(size="product", problem="x").missing() == ["name", "org", "users", "success_metric", "research"]


def test_a_feature_goes_through_as_before_and_says_how_to_switch(client):
    s = start(client, orgs=["acme-farm"])
    s = say(client, s["id"], "Add a CSV export of soil readings for the Farm app")
    assert s["draft"]["size"] == "feature"
    assert "one feature" in s["messages"][-1]["text"]


def test_an_initiative_is_suggested_asked_for_and_sent_to_g0(client, engine):
    s = start(client, teams=["acme-clinic/product-owners"], orgs=["acme-clinic"])
    sid = s["id"]
    s = say(client, sid, "Clinic patients check in on their phone before they arrive; staff see who's ready; "
                         "reminders carry the check-in link")
    last = s["messages"][-1]
    assert s["draft"]["size_suggested"] == "initiative" and s["draft"]["size"] == ""
    assert last["text"].startswith("This sounds like an initiative")
    assert last["options"][0] == "Plan it as an initiative" and "It's a new product" in last["options"]
    s = say(client, sid, "Plan it as an initiative")
    assert s["draft"]["size"] == "initiative" and s["awaiting"] == "success_metric"
    s = say(client, sid, "Median check-in time from 6 minutes to 2 by March")
    assert s["awaiting"] == "horizon" and "Now" in s["messages"][-1]["options"]
    s = say(client, sid, "Now")
    assert s["draft"]["horizon"] == "now" and s["awaiting"] == "research"
    s = say(client, sid, "I've done my own")
    assert s["draft"]["research"] == "own" and s["ready"]
    s = client.post(f"/api/intake/{sid}/submit", json={}).json()
    assert s["kind"] == "initiative" and s["feature_id"].startswith("CLN-INI-")
    wait_idle(engine, s["feature_id"])
    g0 = next(p for p in engine.pending(s["feature_id"]) if p.get("gate") == "g0")
    assert g0["research"] == "from your own research"
    assert engine._state(s["feature_id"])["measure_hint"].startswith("Median check-in time")


def test_leave_the_rest_to_the_agent(client):
    s = start(client, orgs=["acme-clinic"])
    sid = s["id"]
    say(client, sid, "Cut no-shows at the clinic by a quarter this year")
    s = say(client, sid, "Plan it as an initiative")
    s = say(client, sid, "Leave the rest to the agent")
    d = s["draft"]
    assert d["success_metric"] == AGENT_PROPOSES and d["horizon"] == AGENT_PROPOSES and d["research"] == "quick"
    assert s["ready"]


def test_switching_size_keeps_what_was_said(client):
    s = start(client, orgs=["acme-clinic"])
    sid = s["id"]
    say(client, sid, "Cut no-shows at the clinic by a quarter this year")
    s = say(client, sid, "Just one feature for now")
    assert s["draft"]["size"] == "feature" and "no-shows" in s["draft"]["problem"]
    r = client.patch(f"/api/intake/{sid}/draft", json={"size": "initiative"}).json()
    assert r["draft"]["size"] == "initiative" and "no-shows" in r["draft"]["problem"]


def test_a_new_product_needs_an_inception_starter(client, engine):
    s = start(client, orgs=["acme-farm"])
    sid = s["id"]
    s = say(client, sid, "A booking app for small vet practices")
    assert s["draft"]["size_suggested"] == "product"
    s = say(client, sid, "It's a new product")
    assert "inception starters" in s["messages"][-1]["text"] and not s["ready"]

    s = start(client, login="sunil", teams=["valxel-platform/product-council"], orgs=["acme-farm"])
    sid = s["id"]
    say(client, sid, "A booking app for small vet practices")
    s = say(client, sid, "It's a new product")
    assert s["awaiting"] == "name"
    for text in ("VetBook", "acme-vet", "Front desks at practices with one to five vets",
                 "50 practices booking through it by June", "Quick web research"):
        s = say(client, sid, text)
    d = s["draft"]
    assert (d["name"], d["org"], d["research"]) == ("VetBook", "acme-vet", "quick") and s["ready"]
    s = client.post(f"/api/intake/{sid}/submit", json={}).json()
    assert s["kind"] == "inception" and s["feature_id"].startswith("INCEPTION-")
    wait_idle(engine, s["feature_id"])
    st = engine._state(s["feature_id"])
    assert st["vision"]["org"] == "acme-vet" and st["vision"]["goals"] == ["50 practices booking through it by June"]
    assert st["research"].startswith("# Research: VetBook")


def test_a_bad_org_is_asked_again(client):
    s = start(client, login="sunil", teams=["valxel-platform/product-council"], orgs=["acme-farm"])
    sid = s["id"]
    say(client, sid, "A booking app for small vet practices")
    say(client, sid, "It's a new product")
    say(client, sid, "VetBook")
    s = say(client, sid, "acme vet!")
    assert "doesn't look like a GitHub org" in s["messages"][-1]["text"] and s["awaiting"] == "org"


def test_a_feature_at_g1_becomes_an_initiative_and_back(engine):
    fid = engine.create(product_id="CLN", title="Walk-in intake", created_by="maria",
                        description="Register walk-in patients at the front desk; capture consent; show the queue")
    assert any(p.get("gate") == "g1" for p in engine.pending(fid))
    iid = engine.resize(fid, by="maria")
    assert iid.startswith("CLN-INI-") and engine.view(fid)["status"] == "aborted"
    assert "Became initiative" in str(engine._state(fid).get("gate_decisions") or engine._state(fid).get("events"))
    assert engine._state(iid)["request"].startswith("Register walk-in patients")
    assert any(p.get("gate") == "g0" for p in engine.pending(iid))
    back = engine.resize(iid, by="maria")
    assert back.startswith("CLN-FEAT-") and engine.view(iid)["status"] == "dropped"
    with pytest.raises(ValueError, match="only while it waits"):
        engine.resize(iid, by="maria")

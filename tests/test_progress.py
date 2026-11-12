"""A visible path: estimates per step from the product's history, the expected finish, and telling
people when they're needed."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from test_api import CLN_TEAMS, SECRET, sign_in, wait_idle
from vrixel_sdlc import progress
from vrixel_sdlc.api.app import create_app
from vrixel_sdlc.engine import RAIL
from vrixel_sdlc.notify import Notifier
from vrixel_sdlc.web.auth import AuthConfig

T0 = datetime(2026, 11, 1, 9, tzinfo=timezone.utc)


def feature(minutes_to_spec: int, g1_wait: float) -> dict:
    at = lambda m: (T0 + timedelta(minutes=m)).isoformat()  # noqa: E731
    return {"events": [{"node": "discovery", "kind": "passed", "at": at(10)},
                       {"node": "g1", "kind": "approved", "at": at(10 + g1_wait)},
                       {"node": "spec", "kind": "passed", "at": at(10 + g1_wait + minutes_to_spec)}],
            "gate_decisions": [{"gate": "g1", "decision": "approve", "waited_minutes": g1_wait}]}


def test_estimates_come_from_the_products_history_once_there_is_enough():
    states = [feature(30, 60), feature(40, 120), feature(50, 180)]
    learned = progress.history(states, RAIL)
    assert learned["g1"] == 120 and learned["spec"] == 40
    assert "design" not in learned  # no samples: the default stands
    assert progress.history(states[:2], RAIL) == {}  # too few


def test_the_expected_finish_counts_whats_left():
    stations = [{"key": "discovery", "kind": "phase", "state": "done"}, {"key": "g1", "kind": "gate", "state": "waiting"},
                {"key": "spec", "kind": "phase", "state": "todo"}]
    out = progress.estimate(stations, {"g1": 100, "spec": 40}, now=T0)
    assert out["minutes_left"] == 90  # half the wait in progress, then all of the next step
    assert out["expected_done"] == (T0 + timedelta(minutes=90)).isoformat()
    assert out["stations"][1]["from_history"] and not out["stations"][0]["from_history"]
    done = progress.estimate([{**s, "state": "done"} for s in stations], {}, now=T0)
    assert done["expected_done"] is None
    assert progress.describe(30) == "about 30 minutes" and progress.describe(300) == "about 5 hours"


def test_people_are_told_once_when_theyre_needed_and_watchers_at_staging(tmp_path):
    sent, posted = [], []
    n = Notifier(tmp_path, base_url="https://vrixel.example", send_email=lambda *a: sent.append(a),
                 post=posted.append)
    n.set("maria", ["acme-clinic/product-owners"], email="maria@example.com")
    n.set("raj", ["acme-clinic/architects"], email="raj@example.com")
    n.set("ana", ["acme-clinic/product-owners"], email="", needed=False)
    item = {"feature_id": "CLN-FEAT-1", "interrupt_id": "abc", "gate": "g1", "approvers": ["acme-clinic/product-owners"]}
    assert n.needed(item, "Walk-in intake") == ["maria"]
    assert n.needed(item, "Walk-in intake") == []  # once
    to, subject, text = sent[0]
    assert to == "maria@example.com" and subject == "You're needed: G1 for CLN-FEAT-1 Walk-in intake"
    assert "https://vrixel.example/features/CLN-FEAT-1" in text and "@maria" in posted[0]
    n.watch("raj", "CLN-FEAT-1", True)
    n.set("raj", ["acme-clinic/architects"], staging=True)
    assert n.reached("CLN-FEAT-1", "Walk-in intake", "staging") == ["raj"]
    # Preferences survive a restart.
    again = Notifier(tmp_path, send_email=lambda *a: None, post=lambda t: None)
    assert again.get("raj").watch == ["CLN-FEAT-1"] and again.needed(item) == []


@pytest.fixture
def client(engine):
    app = create_app(engine, auth=AuthConfig(mode="dev", secret=SECRET), web_dist=None)
    with TestClient(app) as c:
        yield c


def test_the_feature_page_shows_the_path_and_whose_gates_are_ahead(client, engine):
    sign_in(client, "maria", teams=CLN_TEAMS, orgs=["acme-clinic"])
    sent = []
    engine.services.notifier._send_email = lambda *a: sent.append(a)
    assert client.put("/api/me/notifications", json={"email": "maria@example.com"}).json()["needed"] is True
    fid = client.post("/api/features", json={"product_id": "CLN", "title": "Walk-in intake",
                                             "description": "Register walk-in patients"}).json()["feature_id"]
    wait_idle(engine, fid)
    v = client.get(f"/api/features/{fid}").json()
    g1 = next(s for s in v["path"]["stations"] if s["key"] == "g1")
    assert g1["state"] == "waiting" and g1["mine"] is True and v["path"]["expected_done"]
    assert sent and sent[0][0] == "maria@example.com"
    assert client.post(f"/api/threads/{fid}/watch", json={"on": True}).json() == {"watching": True}
    assert client.get(f"/api/features/{fid}").json()["watching"] is True
    assert client.put("/api/me/notifications", json={"email": "not an email"}).status_code == 422

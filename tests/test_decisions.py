"""Decisions that take minutes: a short summary first, what changed since the version sent back,
and comments on specific lines that go back to the agents with Revise."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from test_api import CLN_TEAMS, SECRET, sign_in, wait_idle
from vrixel_sdlc import decisions
from vrixel_sdlc.api.app import create_app
from vrixel_sdlc.web.auth import AuthConfig

DOC = """# Walk-in intake

Front desks register walk-in patients on paper, and the queue is invisible to clinicians.

## Problem
- Walk-ins wait without anyone knowing

## Requirements
- R1. Register a walk-in in under a minute
- R2. Capture consent
- R3. Show the queue

## Agent notes
- Assumed: one clinic
"""


def test_the_summary_is_skimmed_from_the_document_without_a_model():
    lines = decisions._skim(DOC)
    assert lines[0] == "Front desks register walk-in patients on paper, and the queue is invisible to clinicians."
    assert "Problem: Walk-ins wait without anyone knowing" in lines
    assert any(ln.startswith("Requirements: 3 items, starting with “R1.") for ln in lines)
    assert not any("Agent notes" in ln for ln in lines)


def test_changes_show_only_what_moved():
    after = DOC.replace("R3. Show the queue", "R3. Show the queue, oldest first")
    lines = decisions.changes(DOC, after)
    assert {"op": "-", "text": "- R3. Show the queue"} in lines
    assert {"op": "+", "text": "- R3. Show the queue, oldest first"} in lines
    assert not any(ln["text"].startswith("# Walk-in") for ln in lines)  # far from the change


def test_line_comments_become_feedback_quoting_their_line():
    fb = decisions.with_comments("Tighten it.", [{"quote": "R2. Capture consent", "text": "Say which form"},
                                                 {"quote": "", "text": "Add an empty state"}, {"text": " "}])
    assert fb == ("Tighten it.\n\nComments on specific lines:\n- On “R2. Capture consent”: Say which form\n"
                  "- Add an empty state")
    assert decisions.with_comments("Only words", None) == "Only words"


@pytest.fixture
def client(engine):
    app = create_app(engine, auth=AuthConfig(mode="dev", secret=SECRET), web_dist=None)
    with TestClient(app) as c:
        yield c


def test_a_gate_shows_its_summary_then_what_changed_after_a_revise(client, engine):
    sign_in(client, "maria", teams=CLN_TEAMS, orgs=["acme-clinic"])
    fid = client.post("/api/features", json={"product_id": "CLN", "title": "Walk-in intake",
                                             "description": "Register walk-in patients at the front desk"}).json()["feature_id"]
    v = wait_idle(engine, fid)
    g1 = next(p for p in v["pending"] if p.get("gate") == "g1")
    b = client.get(f"/api/features/{fid}/decisions/{g1['interrupt_id']}/brief").json()
    assert b["summary"]["by"] == "document" and b["summary"]["lines"] and "changed" not in b
    # Opening it again reads the kept summary.
    assert client.get(f"/api/features/{fid}/decisions/{g1['interrupt_id']}/brief").json() == b

    r = client.post(f"/api/features/{fid}/decisions/{g1['interrupt_id']}",
                    json={"decision": "revise", "feedback": "Narrow it",
                          "comments": [{"quote": "Register walk-in patients", "text": "Only at the main desk"}]})
    assert r.status_code == 202, r.text
    import time

    end = time.time() + 10
    while time.time() < end:
        v = wait_idle(engine, fid)
        if any(p.get("gate") == "g1" and p["interrupt_id"] != g1["interrupt_id"] for p in v["pending"]):
            break
        time.sleep(0.05)
    said = [d for d in engine._state(fid)["gate_decisions"] if d["decision"] == "revise"][-1]["feedback"]
    assert said.endswith("- On “Register walk-in patients”: Only at the main desk")
    g1 = next(p for p in v["pending"] if p.get("gate") == "g1")
    b = client.get(f"/api/features/{fid}/decisions/{g1['interrupt_id']}/brief").json()
    assert b["version"] == 2 and b["changed"]["from_version"] == 1
    assert b["changed"]["asked"].startswith("Narrow it")

"""Decisions that take minutes: a short summary first, what changed since the version sent back,
and comments on specific lines that go back to the agents with Revise."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from test_api import CLN_TEAMS, SECRET, sign_in, wait_idle
from valxel_sdlc import decisions
from valxel_sdlc.api.app import create_app
from valxel_sdlc.web.auth import AuthConfig

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


def test_low_risk_rules():
    base = {"type": "gate", "gate": "g1", "risk_tier": "low", "findings": [], "score": 0.9}
    assert decisions.low_risk(base, "general") == (True, "critic passed, general data, low risk")
    assert decisions.low_risk(base, "phi")[1] == "phi product"
    assert decisions.low_risk({**base, "gate": "g6"}, "general")[1] == "decided one at a time"
    assert decisions.low_risk({**base, "findings": ["x"]}, "general")[0] is False
    assert decisions.low_risk({**base, "revisions": 1}, "general")[1] == "sent back before"
    assert decisions.low_risk({**base, "new_repos": [{"name": "r"}]}, "general")[0] is False


def test_the_inbox_groups_and_batch_approves_only_low_risk(client, engine):
    sign_in(client, "priya", teams=["acme-farm/product-owners"], orgs=["acme-farm", "acme-clinic"])
    farm = [client.post("/api/features", json={"product_id": "FARM", "title": t, "description": t}).json()["feature_id"]
            for t in ("Field list sorting", "Rainfall import from the weather service")]
    for f in farm:
        wait_idle(engine, f)
    items = client.get("/api/inbox?mine=true").json()
    mine = [i for i in items if i["feature_id"] in farm]
    assert mine and all(i["product_name"] and i["low_risk"] for i in mine), mine
    r = client.post("/api/inbox/approve", json={"items": [{"feature_id": i["feature_id"], "interrupt_id": i["interrupt_id"]}
                                                          for i in mine] + [{"feature_id": "FARM-FEAT-99", "interrupt_id": "x"}]})
    out = r.json()
    assert sorted(out["approved"]) == sorted(farm) and out["skipped"][0]["feature_id"] == "FARM-FEAT-99"


def test_a_request_changes_before_the_build_and_the_waiting_gate_goes_back(engine):
    fid = engine.create(product_id="CLN", title="Walk-in intake", created_by="maria",
                        description="Register walk-in patients at the front desk")
    g1 = next(p for p in engine.pending(fid) if p.get("gate") == "g1")
    out = engine.change_request(fid, "Register walk-in patients at the front desk\nAlso in Spanish", by="maria")
    assert out["sent_back"] == "g1" and "Added: Also in Spanish" in out["note"]
    import time

    for _ in range(200):
        if fid not in engine.running and any(p["interrupt_id"] != g1["interrupt_id"] for p in engine.pending(fid)):
            break
        time.sleep(0.05)
    s = engine._state(fid)
    assert s["description"].endswith("Also in Spanish")
    assert any(e["node"] == "request" and e["kind"] == "changed" for e in s["events"])
    assert [d for d in s["gate_decisions"] if d["decision"] == "revise"][-1]["feedback"].startswith("The person who asked")
    with pytest.raises(ValueError, match="Nothing changed"):
        engine.change_request(fid, s["description"], by="maria")


def test_a_diagram_section_is_left_out_of_the_summary():
    doc = "# HLD\n\nExtends the API.\n\n## Diagram\n```mermaid\nflowchart LR\n  a --> b\n```\n\n## Contracts\n- api: minor bump\n"
    lines = decisions._skim(doc)
    assert not any("mermaid" in x or x.startswith("Diagram") for x in lines)
    assert "Contracts: api: minor bump" in lines

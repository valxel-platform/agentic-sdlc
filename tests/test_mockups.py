"""Mockups at G3: a page per screen, checked, kept with the spec and served sandboxed."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from test_api import SECRET, sign_in
from valxel_sdlc import mockups
from valxel_sdlc.api.app import create_app
from valxel_sdlc.web.auth import AuthConfig

UX = """## Screens
- S1: Ready list (web: acme/web)
- S2: Detail (web: acme/web)
## States
- S1: loading, empty (no one yet), error (couldn't load: Try again), success
- S2: loading, no empty: it opens from the list, error (Try again), success
## Copy
- S1 heading: "Who's ready"
- S1 refresh: "Refresh"
- S1 error: "We couldn't load the list. Try again in a moment."
- S2 heading: "Visit details"
"""


def test_the_stub_draws_every_screen_with_its_copy_and_passes_its_checks():
    pages = mockups.split(mockups.stub_pages(UX))
    assert set(pages) == {"S1", "S2"}
    assert "Who&#x27;s ready" in pages["S1"] and 'id="st-error"' in pages["S1"] and "<script" not in pages["S1"]
    assert mockups.check(UX, pages) == []


def test_the_checks_catch_what_a_mockup_must_not_do():
    bad = ('<html><body><script>x()</script><img src="https://cdn/x.png"><input id="q">'
           "<button></button><p>Who is ready</p></body></html>")
    found = mockups.problems("S1", bad, ["Who's ready"])
    assert "S1: no scripts or event handlers (switch states with radio buttons and CSS)" in found
    assert "S1: no external resources or links out: everything inline" in found
    assert any("page language" in f for f in found) and any("alt text" in f for f in found)
    assert any("needs a label" in f for f in found) and any("button needs words" in f for f in found)
    assert any("heading" in f for f in found) and any("\"Who's ready\"" in f for f in found)
    assert mockups.check(UX, {"S1": mockups.stub_page("S1", "x", "", [], "")})[0] == "S2 (Detail) has no mockup"


@pytest.fixture
def client(engine):
    app = create_app(engine, auth=AuthConfig(mode="dev", secret=SECRET), web_dist=None)
    with TestClient(app) as c:
        yield c


def test_g3_shows_the_mockups_and_they_are_served_sandboxed(client, engine):
    sign_in(client, "priya", teams=["acme-farm/product-owners", "acme-farm/design"], orgs=["acme-farm"])
    fid = engine.create(product_id="FARM", title="Field list sorting", description="Sort the field list by moisture")
    for gate in ("g1", "g2"):
        p = next(x for x in engine.pending(fid) if x.get("gate") == gate)
        engine.decide(fid, p["interrupt_id"], {"decision": "approve", "approver": "a"})
    g3 = next(x for x in engine.pending(fid) if x.get("gate") == "g3")
    m = g3["mockups"]
    assert [s["id"] for s in m["screens"]] == ["S1", "S2"] and m["findings"] == []
    r = client.get(f"/api/features/{fid}/mockups/S1")
    assert r.status_code == 200 and "<h1>" in r.text
    assert "sandbox" in r.headers["content-security-policy"] and "default-src 'none'" in r.headers["content-security-policy"]
    assert client.get(f"/api/features/{fid}/mockups/S9").status_code == 404
    sign_in(client, "stranger", orgs=["acme-clinic"])
    assert client.get(f"/api/features/{fid}/mockups/S1").status_code == 403

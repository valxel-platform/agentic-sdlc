"""The G3 prototype (valxel_sdlc.prototype): the UX spec's flow as one page to click through, checked
against the spec, served sandboxed with only the tool's runtime, and recorded with G3's documents."""

from __future__ import annotations

import base64
import hashlib
import re

import pytest
from fastapi.testclient import TestClient
from test_api import SECRET, sign_in

from valxel_sdlc import prototype, records
from valxel_sdlc.api.app import create_app
from valxel_sdlc.web.auth import AuthConfig

UX = """## Screens
- S1: Visit list (web: acme/web) [/visits]
- S2: New visit (web: acme/web) [/visits/new]
## States
- S1: loading, empty (no visits yet), error (couldn't load: Try again), success
- S2: no empty: it's a form, error (couldn't save), success
## Flow
- start -> S1: from the menu
- S1 -> S2: Add visit
- S2 -> S1: Save
- S2 -> cancel: Cancel
## Copy
- S1 heading: "Visits"
- S1 add button: "Add visit"
- S2 heading: "New visit"
- S2 save button: "Save"
- S2 cancel button: "Cancel"
"""

GOOD = """<!doctype html><html lang="en"><head><style>body{font:16px system-ui}</style></head><body>
<section data-screen="S1" aria-label="Visit list"><h1>Visits</h1>
  <div data-state="loading">Loading</div><div data-state="empty">No visits yet</div>
  <div data-state="error">Couldn't load</div>
  <div data-state="success"><ul id="visits"><li>Example visit</li></ul></div>
  <button type="button" data-go="S2" data-change="new">Add visit</button></section>
<section data-screen="S2" aria-label="New visit"><h1>New visit</h1>
  <div data-state="error">Couldn't save</div>
  <div data-state="success"><label for="reason">Reason</label><input id="reason" value="Example reason"></div>
  <template id="visit"><li data-bind="reason"></li></template>
  <button type="button" data-append="visits" data-template="visit" data-go="S1">Save</button>
  <button type="button" data-go="cancel">Cancel</button></section>
</body></html>"""


def test_a_prototype_that_covers_the_spec_passes():
    assert prototype.check(UX, GOOD) == []
    assert list(prototype.screens_in(GOOD)) == ["S1", "S2"]


def test_the_stand_in_designer_draws_one_that_passes():
    doc = prototype.stub(UX)
    assert prototype.check(UX, doc) == []
    assert 'data-go="S2"' in doc and 'data-go="cancel"' in doc


@pytest.mark.parametrize(("change", "says"), [
    (('data-go="S2" data-change="new">Add visit', 'data-change="new">Add visit'), "S1 -> S2 (Add visit): no control on S1"),
    (('<section data-screen="S2"', '<section data-screen="S3"'), "S2 (New visit) has no screen"),
    (("<h1>Visits</h1>", "<h1>All visits</h1>"), 'the copy "Visits"'),
    (('<div data-state="empty">No visits yet</div>', ""), 'S1: show the empty state'),
    (('data-template="visit"', 'data-template="nope"'), 'data-template="nope" points at nothing'),
    (("<h1>Visits</h1>", "<h1>Visits</h1><script>fetch('/api/me')</script>"), "no scripts"),
    (('<button type="button" data-go="cancel">', '<button type="button" onclick="x()" data-go="cancel">'), "no event handlers"),
    (("<style>", "<style>@import url(https://evil.example/x.css);"), "no outside stylesheets"),
    (('<label for="reason">Reason</label>', ""), "S2: every field needs a label"),
    (('<ul id="visits">', '<ul id="visits" class="vx-bar">'), "the vx- class names are the tool's"),
    (('<html lang="en">', "<html>"), "set the page language"),
])
def test_the_checks_catch_what_a_prototype_must_not_do(change, says):
    old, new = change
    assert old in GOOD
    found = prototype.check(UX, GOOD.replace(old, new, 1))
    assert any(says in f for f in found), found


def test_only_the_tools_runtime_may_run():
    page = prototype.assemble(GOOD)
    assert page.count("<script>") == 1 and page.index("<script>") > page.index("</section>")
    digest = base64.b64encode(hashlib.sha256(prototype.RUNTIME_JS.encode()).digest()).decode()
    assert f"'sha256-{digest}'" in prototype.CSP
    assert "default-src 'none'" in prototype.CSP and prototype.CSP.endswith("sandbox allow-scripts")
    assert "connect-src" not in prototype.CSP  # default-src 'none' covers it: no fetch, no beacons


@pytest.fixture
def client(engine):
    with TestClient(create_app(engine, auth=AuthConfig(mode="dev", secret=SECRET), web_dist=None)) as c:
        yield c


def test_g3_has_a_prototype_to_walk_through_and_it_is_served_sandboxed(client, engine):
    sign_in(client, "priya", teams=["acme-farm/product-owners", "acme-farm/design"], orgs=["acme-farm"])
    fid = engine.create(product_id="FARM", title="Field list sorting", description="Sort the field list by moisture")
    for gate in ("g1", "g2"):
        p = next(x for x in engine.pending(fid) if x.get("gate") == gate)
        engine.decide(fid, p["interrupt_id"], {"decision": "approve", "approver": "a"})
    g3 = next(x for x in engine.pending(fid) if x.get("gate") == "g3")
    m = g3["mockups"]
    assert m["prototype"] is True and [s["id"] for s in m["screens"]] == ["S1", "S2"] and m["findings"] == []
    r = client.get(f"/api/features/{fid}/prototype")
    assert r.status_code == 200 and 'data-screen="S1"' in r.text and prototype.RUNTIME_JS in r.text
    csp = r.headers["content-security-policy"]
    assert csp == prototype.CSP and r.headers["referrer-policy"] == "no-referrer"
    sign_in(client, "stranger", orgs=["acme-clinic"])
    assert client.get(f"/api/features/{fid}/prototype").status_code == 403

    files, _ = records.files_for(engine.services, engine._state(fid), "g3")
    page = files[f"features/{fid}/prototype.html"]
    assert prototype.RUNTIME_JS in page  # opens and works from the product's repo


def test_a_feature_without_one_says_so(client, engine):
    sign_in(client, "priya", teams=["acme-farm/product-owners"], orgs=["acme-farm"])
    fid = engine.create(product_id="FARM", title="Harvest export")
    assert client.get(f"/api/features/{fid}/prototype").status_code == 404


def test_the_runtime_has_no_way_out():
    js = prototype.RUNTIME_JS
    for banned in ("fetch(", "XMLHttpRequest", "WebSocket", "sendBeacon", "eval(", "innerHTML", "window.open",
                   "document.cookie", "localStorage", "postMessage"):
        assert banned not in js, banned
    assert not re.search(r"location\s*=|location\.href\s*=", js)

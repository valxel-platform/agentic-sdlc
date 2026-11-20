"""G1's rough sketch (valxel_sdlc.sketch): drawn from the discovery brief, greyed and labelled as
rough, served with no script, recorded with G1's documents, skipped when nothing people see changes."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from test_api import SECRET, sign_in

from valxel_sdlc import records, sketch
from valxel_sdlc.api.app import create_app
from valxel_sdlc.web.auth import AuthConfig


@pytest.fixture
def client(engine):
    with TestClient(create_app(engine, auth=AuthConfig(mode="dev", secret=SECRET), web_dist=None)) as c:
        yield c


def test_g1_has_a_rough_sketch_beside_the_brief(client, engine):
    sign_in(client, "priya", teams=["acme-farm/product-owners"], orgs=["acme-farm"])
    fid = engine.create(product_id="FARM", title="Field list sorting", description="Sort the field list by moisture")
    g1 = next(p for p in engine.pending(fid) if p.get("gate") == "g1")
    assert g1["sketch"]["version"] == 1 and g1["sketch"]["findings"] == []
    r = client.get(f"/api/features/{fid}/sketch")
    assert r.status_code == 200 and "Rough sketch, to talk about scope" in r.text and "grayscale(1)" in r.text
    assert r.headers["content-security-policy"] == sketch.CSP and "script-src" not in sketch.CSP
    assert "<script" not in r.text.lower()
    files, _ = records.files_for(engine.services, engine._state(fid), "g1")
    assert "Rough sketch" in files[f"features/{fid}/sketch.html"]
    sign_in(client, "stranger", orgs=["acme-clinic"])
    assert client.get(f"/api/features/{fid}/sketch").status_code == 403


def test_no_sketch_when_nothing_people_see_changes(engine):
    fid = engine.create(product_id="FARM", title="Nightly export job", description="A batch export", ui=False)
    g1 = next(p for p in engine.pending(fid) if p.get("gate") == "g1")
    assert g1["sketch"] is None and "SKETCH" not in (engine._state(fid).get("artifacts") or {})


@pytest.mark.parametrize(("doc", "says"), [
    ('<html lang="en"><body><h1>A</h1><script>x()</script></body></html>', "no scripts"),
    ('<html><body><h1>A</h1></body></html>', "set the page language"),
    ('<html lang="en"><body><p>A</p></body></html>', "name each screen"),
    ('<html lang="en"><body><h1>A</h1><img src="https://x.example/a.png" alt=""></body></html>', "nothing loaded"),
])
def test_the_checks(doc, says):
    assert any(says in f for f in sketch.check(doc))


def test_the_stand_in_sketch_passes_and_is_greyed_with_the_banner_first():
    doc = sketch.stub("## Goals\n- Farmers sort fields by moisture\n- See the driest first", "Field sorting")
    assert sketch.check(doc) == []
    page = sketch.assemble(doc)
    assert page.index("Rough sketch") < page.index("Field sorting") and "grayscale(1)" in page

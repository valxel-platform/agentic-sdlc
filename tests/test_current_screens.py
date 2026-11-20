"""Today's screens for the G3 prototype (valxel_sdlc.current_screens, graph.prototype_steps): the
product's scan screenshots the spec's routes in staging, the designer draws on top of them, G3 shows
"today" beside the prototype, and a scan that never reports doesn't hold the feature up."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from test_api import SECRET, sign_in

from valxel_sdlc import current_screens, prototype, screens
from valxel_sdlc.api.app import create_app
from valxel_sdlc.gateway.providers import _anthropic_content, _openai_content
from valxel_sdlc.web.auth import AuthConfig

SCAN_REPO = "acme-farm/farm-mobile"  # FARM's web app (role consumer); it has no acceptance repo


def to_g2(engine, title="Field list sorting"):
    fid = engine.create(product_id="FARM", title=title, description="Sort the field list by moisture")
    for gate in ("g1", "g2"):
        p = next(x for x in engine.pending(fid) if x.get("gate") == gate)
        engine.decide(fid, p["interrupt_id"], {"decision": "approve", "approver": "a"})
    return fid


@pytest.fixture
def staging(services, monkeypatch):
    """FARM's web app deployed in staging, with a URL the scan can open, and the scan's workflow."""
    services.github.files[(SCAN_REPO, "main", screens.WORKFLOW)] = "name: screens"
    real = services.openchoreo.deployment

    def deployment(*, component, env, project=None, namespace=None):
        if env == "staging":
            return {"state": "ready", "release": "r1", "url": f"https://{component}.staging.example.internal"}
        return real(component=component, env=env, project=project, namespace=namespace)

    monkeypatch.setattr(services.openchoreo, "deployment", deployment)
    return services


@pytest.fixture
def drawn(monkeypatch):
    """What the designer was given when it drew the prototype."""
    seen: list[dict] = []
    real = prototype.draw

    def draw(services, ctx, ux, findings=None, budget_usd=None, images=(), today=""):
        seen.append({"images": list(images), "today": today})
        return real(services, ctx, ux, findings, budget_usd, images, today)

    monkeypatch.setattr(prototype, "draw", draw)
    return seen


def test_without_staging_the_prototype_is_drawn_from_the_spec_and_g3_and_g4_open_together(engine, drawn):
    fid = to_g2(engine)
    gates = sorted(p["gate"] for p in engine.pending(fid) if p.get("type") == "gate")
    assert gates == ["g3", "g4"]
    assert drawn and drawn[0]["images"] == [] and drawn[0]["today"] == ""
    events = [(e["node"], e["kind"]) for e in engine._state(fid)["events"]]
    assert ("design_prototype", "skipped") in events and ("design_prototype", "prototype") in events
    assert engine.services.github.dispatches == [] or all(
        d["event_type"] != "valxel-screens" for d in engine.services.github.dispatches)


def test_the_designer_draws_on_todays_screens_and_g3_shows_them(staging, engine, drawn):
    gh = engine.services.github
    gh.screens_missing = {"S2"}  # the confirmation screen doesn't exist yet
    fid = to_g2(engine)
    [d] = [d for d in gh.dispatches if d["event_type"] == "valxel-screens"]
    p = d["payload"]
    assert d["repo"] == SCAN_REPO and p["stage"] == "today" and p["check"] == f"screens / {fid} / today"
    assert p["sha"] == gh.head_sha(repo=SCAN_REPO) and p["flag"] == ""
    assert [s["route"] for s in p["screens"]] == ["/field_list_sorting", "/field_list_sorting/done"]
    assert p["env"] == {"farm-mobile": "https://farm-mobile.staging.example.internal"}

    [seen] = drawn
    assert [im["label"] for im in seen["images"]] == ["S1 (Field list sorting form) as it is today, desktop width:"]
    assert seen["images"][0]["media_type"] == "image/png" and seen["images"][0]["data"].startswith(b"\x89PNG")
    assert "Today's screens are attached" in seen["today"] and "S2 (Confirmation)" in seen["today"]

    g3 = next(x for x in engine.pending(fid) if x.get("gate") == "g3")
    by = {s["id"]: s for s in g3["mockups"]["screens"]}
    assert by["S1"]["today"] == {"desktop": True, "phone": True} and by["S1"]["new"] is False
    assert by["S2"]["today"] is None and by["S2"]["new"] is True
    assert any(x.get("gate") == "g4" for x in engine.pending(fid))  # G4 still opens with G3

    with TestClient(create_app(engine, auth=AuthConfig(mode="dev", secret=SECRET), web_dist=None)) as c:
        sign_in(c, "priya", teams=["acme-farm/product-owners"], orgs=["acme-farm"])
        r = c.get(f"/api/features/{fid}/today/S1/desktop")
        assert r.status_code == 200 and r.headers["content-type"] == "image/png" and r.content.startswith(b"\x89PNG")
        assert c.get(f"/api/features/{fid}/today/S2/desktop").status_code == 404
        assert c.get(f"/api/features/{fid}/today/S1/huge").status_code == 404
        sign_in(c, "stranger", orgs=["acme-clinic"])
        assert c.get(f"/api/features/{fid}/today/S1/desktop").status_code == 403


def test_a_repo_without_the_scan_isnt_asked(staging, engine, drawn):
    del engine.services.github.files[(SCAN_REPO, "main", screens.WORKFLOW)]
    fid = to_g2(engine)
    assert not [d for d in engine.services.github.dispatches if d["event_type"] == "valxel-screens"]
    assert sorted(p["gate"] for p in engine.pending(fid) if p.get("type") == "gate") == ["g3", "g4"]


def test_a_g3_revision_reuses_todays_screens(staging, engine):
    fid = to_g2(engine)
    gh = engine.services.github
    g4 = next(x for x in engine.pending(fid) if x.get("gate") == "g4")  # G3 and G4 share a round
    engine.decide(fid, g4["interrupt_id"], {"decision": "approve", "approver": "a"})
    g3 = next(x for x in engine.pending(fid) if x.get("gate") == "g3")
    engine.decide(fid, g3["interrupt_id"], {"decision": "revise", "approver": "a", "feedback": "Bigger save button"})
    assert sum(d["event_type"] == "valxel-screens" for d in gh.dispatches) == 1
    assert any(x.get("gate") == "g3" for x in engine.pending(fid))


def test_a_scan_that_never_reports_doesnt_hold_the_feature_up(staging, engine, drawn):
    engine.services.github.screens_result = "pending"
    fid = to_g2(engine)
    [w] = [p for p in engine.pending(fid) if p.get("wait") == "today_screens"]
    assert w["label"] == "Today's screens" and not any(p.get("gate") == "g3" for p in engine.pending(fid))
    assert engine.waits.check(w) is None  # still within the 20 minutes
    late = {**w, "since": (datetime.now(UTC) - timedelta(minutes=21)).isoformat()}
    res = engine.waits.check(late)
    assert res["status"] == "failure" and "within 20 minutes" in res["reason"]
    engine._run(fid, _resume(w, res))
    gates = sorted(p["gate"] for p in engine.pending(fid) if p.get("type") == "gate")
    assert gates == ["g3", "g4"] and drawn[-1]["images"] == []
    assert any("within 20 minutes" in e.get("detail", "") for e in engine._state(fid)["events"])


def _resume(w, res):
    from langgraph.types import Command

    return Command(resume={w["interrupt_id"]: res})


def test_the_report_is_read_even_when_the_title_runs_into_it():
    got = current_screens.read("2 screens scanned - S1 /a: passed; desktop: F/today/S1-desktop.png; "
                               "phone: F/today/S1-phone.png\n- S2 /b: failed")
    assert got["S1"] == {"route": "/a", "today": True, "desktop": "F/today/S1-desktop.png",
                         "phone": "F/today/S1-phone.png"}
    assert got["S2"] == {"route": "/b", "today": False}


def test_only_safe_paths_are_read_from_the_product_repo():
    gh = SimpleNamespace(get_bytes=lambda **kw: b"png")
    services = SimpleNamespace(github=gh)
    assert current_screens.picture(services, "o/r", "F/today/S1-desktop.png") == b"png"
    for bad in ("../secrets.png", "F/today/S1.txt", "", "a b.png"):
        assert current_screens.picture(services, "o/r", bad) is None


def test_pictures_go_before_the_task_in_both_provider_formats():
    req = SimpleNamespace(layers=SimpleNamespace(tail="Draw it"), images=(
        {"label": "S1 today:", "media_type": "image/png", "data": b"\x89PNG"},
        {"label": "skipped", "media_type": "text/html", "data": b"<script>"}))
    a = _anthropic_content(req)
    assert [p["type"] for p in a] == ["text", "image", "text"] and a[1]["source"]["media_type"] == "image/png"
    assert a[-1]["text"] == "Draw it"
    o = _openai_content(req)
    assert o[1]["image_url"]["url"].startswith("data:image/png;base64,")
    plain = SimpleNamespace(layers=SimpleNamespace(tail="Draw it"), images=())
    assert _anthropic_content(plain) == "Draw it" and _openai_content(plain) == "Draw it"

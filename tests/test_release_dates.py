"""A release's date acts (T-98): on the day, if not everything is ready, its people ship what's
ready (only when every must-have is in; the late features ship on their own) or move the date; a
week before, they're told which features aren't on track."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from test_roadmap import ROOT, gate, make

TODAY = datetime.now(UTC).date().isoformat()


@pytest.fixture
def gov_root(tmp_path):
    import shutil

    root = tmp_path / "governance"
    shutil.copytree(ROOT / "examples" / "governance", root)
    return root


def due_train(eng, *, must=False):
    a = eng.create(product_id="FARM", title="Offline crop log")
    b = eng.create(product_id="FARM", title="Soil sensor import")
    cands = [{"title": "Offline crop log"}, {"title": "Soil sensor import", "must": must}]
    rid = eng.create_release(product_id="FARM", title="Certified build", model="train", features=[a, b],
                             target_date=TODAY, initiative="FARM-INI-9" if must else "", candidates=cands if must else None)
    eng.auto_drive(a)  # ready: waiting for its train after staging; b is still at G1
    return a, b, rid


def test_on_its_date_its_people_ship_whats_ready(tmp_path, gov_root):
    eng, _ = make(tmp_path, gov_root)
    a, b, rid = due_train(eng)
    g = gate(eng, rid, "release_date")
    assert g["allowed"] == ["ship_ready", "move_date"] and g["can_ship"] is True
    assert g["readiness"]["says"].startswith("1 of 2 ready")
    eng.decide(rid, g["interrupt_id"], {"decision": "ship_ready", "approver": "rm"})
    assert eng.view(b)["ship"]["own"] and eng.release_members(rid) == [a]  # b ships on its own
    rel = gate(eng, rid, "release")  # the release goes on with what's ready
    assert rel["features"] == [a]
    eng.decide(rid, rel["interrupt_id"], {"decision": "approve", "approver": "rm"})
    assert eng.view(a)["status"] == "done"
    assert any(e["kind"] == "left_release" for e in eng.view(b)["events"])


def test_or_move_the_date(tmp_path, gov_root):
    eng, _ = make(tmp_path, gov_root)
    _, _, rid = due_train(eng)
    g = gate(eng, rid, "release_date")
    later = (datetime.now(UTC) + timedelta(days=14)).date().isoformat()
    eng.decide(rid, g["interrupt_id"], {"decision": "move_date", "approver": "rm", "target_date": later})
    v = eng.view(rid)
    assert v["target_date"] == later and [p.get("wait") for p in v["pending"]] == ["release_ready"]
    assert any(e["kind"] == "moved" for e in v["events"])


def test_a_missing_must_have_means_the_date_moves(tmp_path, gov_root):
    eng, _ = make(tmp_path, gov_root)
    _, _, rid = due_train(eng, must=True)
    g = gate(eng, rid, "release_date")
    assert g["allowed"] == ["move_date"] and g["can_ship"] is False
    assert g["readiness"]["must"]["missing"]
    with pytest.raises(Exception, match="decision must be one of"):
        eng.decide(rid, g["interrupt_id"], {"decision": "ship_ready", "approver": "rm"})


def test_a_week_before_its_people_hear_whats_not_on_track(tmp_path, gov_root):
    eng, svc = make(tmp_path, gov_root)
    a = eng.create(product_id="FARM", title="Offline crop log")
    soon = (datetime.now(UTC) + timedelta(days=3)).date().isoformat()
    rid = eng.create_release(product_id="FARM", title="Spring season", model="launch", features=[a], target_date=soon)
    assert eng.warn_release_dates() == [rid]
    [msg] = [m for m in svc.notifier.outbox if m["kind"] == "notice"]
    assert msg["ref"] == f"{rid}:date:{soon}" and "Offline crop log" in str(svc.notifier.outbox) or msg
    assert eng.warn_release_dates() == [rid] and len([m for m in svc.notifier.outbox if m["kind"] == "notice"]) == 1
    far = eng.create_release(product_id="FARM", title="Autumn", model="launch",
                             features=[eng.create(product_id="FARM", title="Harvest export")],
                             target_date=(datetime.now(UTC) + timedelta(days=30)).date().isoformat())
    assert far not in eng.warn_release_dates()

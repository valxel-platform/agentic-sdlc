"""No dead ends after approval: merge the record here, start an initiative's features together or
in order, and start a new product's first feature."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from vrixel_sdlc.config import load_settings
from vrixel_sdlc.engine import FeatureEngine
from vrixel_sdlc.services import build_services

ROOT = Path(__file__).resolve().parents[1]
REQUEST = ("Register walk-in patients at the front desk; Capture consent during registration; "
           "Show the waiting queue to clinicians")


@pytest.fixture
def made(tmp_path):
    root = tmp_path / "governance"
    shutil.copytree(ROOT / "examples" / "governance", root)
    s = load_settings(mode="stub", database_url="", artifact_dir=tmp_path / "a", governance_source="local",
                      governance_root=root)
    svc = build_services(s)
    return FeatureEngine(svc, InMemorySaver()), svc


def approved(eng):
    iid = eng.create_initiative(product_id="CLN", title="Walk-in intake", request=REQUEST, created_by="maria",
                                research="own")
    g0 = next(p for p in eng.pending(iid) if p.get("gate") == "g0")
    eng.decide(iid, g0["interrupt_id"], {"decision": "approve", "approver": "maria-po"})
    return iid


def test_the_roadmap_record_merges_from_the_tool(made):
    eng, svc = made
    iid = approved(eng)
    [pr] = eng.record_prs(iid)
    assert pr["key"] == "record" and not pr["merged"]
    out = eng.merge_record(iid, "record", by="maria-po")
    assert out["merged"] and svc.github.prs[f"{pr['repo']}#{pr['number']}"]["merged_by"] == "maria-po"
    assert eng.record_prs(iid)[0]["merged"] and eng.view(iid)["record_pr"]["merged"]
    with pytest.raises(KeyError):
        eng.merge_record(iid, "nothing", by="maria-po")


def test_features_start_together_or_in_order(made):
    eng, _ = made
    iid = approved(eng)
    out = eng.start_candidates(iid, [0, 1, 2], order="sequence", by="maria")
    assert len(out["started"]) == 1 and out["queued"] == ["Capture consent during registration",
                                                          "Show the waiting queue to clinicians"]
    first = out["started"][0]
    for _ in range(50):
        if first not in eng.running:
            break
        import time

        time.sleep(0.05)
    eng.auto_drive(first)  # it reaches everyone: the next one starts by itself
    started = [f for f in eng.feature_summaries("CLN") if f.get("initiative_id") == iid]
    assert {f["title"] for f in started} >= {"Register walk-in patients at the front desk",
                                             "Capture consent during registration"}
    assert eng._queue()[iid]["next"] == [2]
    with pytest.raises(ValueError, match="started already"):
        eng.start_candidates(iid, [0], order="together", by="maria")


def test_a_new_product_starts_its_first_feature_once_registered(made, monkeypatch):
    from vrixel_sdlc.inception import Vision

    eng, svc = made
    tid = eng.create_inception(Vision(name="Aqua Watch", org="acme-water", vision="Small farms lose crops because "
                                      "nobody sees soil moisture in time."), created_by="sunil")
    for gate in ("brief", "product"):
        p = next(x for x in eng.pending(tid) if x.get("gate") == gate)
        eng.decide(tid, p["interrupt_id"], {"decision": "approve", "approver": "sunil"})
    assert eng.view(tid)["status"] == "set_up"
    assert {p["key"] for p in eng.record_prs(tid)} >= {"governance_pr"}
    with pytest.raises(ValueError, match="isn't registered yet"):
        eng.start_first_feature(tid, by="sunil")
    seen = {}
    monkeypatch.setitem(svc.settings.registry, "AW", svc.settings.registry["CLN"])
    monkeypatch.setattr(eng, "create", lambda **kw: seen.update(kw) or "AW-FEAT-1")
    assert eng.start_first_feature(tid, by="sunil") == "AW-FEAT-1"
    assert seen["product_id"] == "AW" and seen["initiative_id"] == "AW-INI-1" and seen["title"]

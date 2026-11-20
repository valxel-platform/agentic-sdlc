"""The product keeps the operations report and the outcome (records.py): the report says what to
watch at release and is recorded after it; each readout whose number is in rewrites outcome.md; the
catalog points at the record."""

from __future__ import annotations

from test_drafts import approve_all

from valxel_sdlc import records
from valxel_sdlc.evaluators.checks import CHECKS
from valxel_sdlc.outcomes import readouts

GOV = "acme-farm/farm-governance"


def released(engine, title="Field list sorting"):
    fid = engine.create(product_id="FARM", title=title, description="Sort the field list by moisture")
    approve_all(engine, fid)
    state = {**engine._state(fid), "feature_id": fid}
    assert state["status"] == "done"
    return fid, state


def test_the_operations_report_is_recorded_after_release(engine):
    fid, state = released(engine)
    files = engine.services.github.files
    report = files[(GOV, "main", f"features/{fid}/operations.md")]
    assert "## What to watch" in report and "readouts at 7, 30 and 90 days" in report
    assert "## Metrics vs brief" not in report  # nothing is measured at release
    approval = files[(GOV, "main", f"features/{fid}/approvals/operate.md")]
    assert approval.startswith(f"# {fid}: operations report after release") and "- Released by: " in approval
    [ev] = [e for e in state["events"] if e["node"] == "operate" and e["kind"] == "documents_recorded"]
    assert ev["merged"] is True

    [cap] = [c for c in engine.services.catalog.lookup("FARM") if c.get("feature") == fid]
    assert cap["record"] == {"repo": GOV, "path": f"features/{fid}/"} and "artifacts" not in cap


def test_the_report_must_say_what_to_watch():
    check = CHECKS["operate"]
    assert check("# Operating report: X\n## What to watch\n- M1: weekly active farmers toward 400\n", {}) == []
    assert check("# Operating report: X\n## Capability\n- CAP-1\n", {}) == [
        "Nothing to watch: name the brief's metrics and targets"]


def readout(fid, window, after, *, before=10.0, tries=1, goal="GOAL-FARM-1"):
    return {"product_id": "FARM", "feature_id": fid, "goal_id": goal, "window_days": window,
            "released_at": "2026-11-01T00:00:00+00:00", "unit": "farmers", "direction": "up",
            "before": {"status": "ok", "value": before}, "after": after,
            "change": (after.get("value") - before) if after.get("status") == "ok" else None,
            "toward": (after.get("value") > before) if after.get("status") == "ok" else None,
            "caveat": readouts.CAVEAT, "tries": tries}


def test_outcome_md_reads_like_a_table():
    rows = [readout("F1", 30, {"status": "suppressed"}), readout("F1", 7, {"status": "ok", "value": 14.0})]
    text = records.outcome_text({"feature_id": "F1", "title": "Sorting", "product_id": "FARM"}, rows)
    assert "## GOAL-FARM-1 (farmers)" in text and "- Released: 2026-11-01" in text
    lines = [line for line in text.splitlines() if line.startswith("| ") and "days" in line]
    assert lines == ["| 7 days | 10 | 14 | 4 | yes |",
                     "| 30 days | 10 | suppressed (too few people to show) | no data | not yet known |"]
    assert readouts.CAVEAT in text


def test_a_readout_whose_number_is_in_updates_the_products_outcome(engine):
    fid, _ = released(engine, "Harvest export")
    store = engine.services.readings
    waiting = readout(fid, 7, {"status": "no_data"})
    assert engine.record_outcomes([waiting]) == 0  # nothing in yet: nothing to record
    got = readout(fid, 7, {"status": "ok", "value": 12.0})
    store.put_readout(got)
    assert engine.record_outcomes([got]) == 1
    text = engine.services.github.files[(GOV, "main", f"features/{fid}/outcome.md")]
    assert "| 7 days | 10 | 12 | 2 | yes |" in text
    out = readouts.feature_outcome(engine, fid)
    assert out["record"]["path"] == f"features/{fid}/outcome.md" and out["record"]["repo"] == GOV
    gave_up = readout(fid, 30, {"status": "no_data"}, tries=readouts.RETRIES)
    assert readouts.final(gave_up)  # it stopped waiting: recorded as no data

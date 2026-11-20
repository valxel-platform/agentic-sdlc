"""The tool's copies are working drafts (valxel_sdlc.drafts): once a finished feature's gate records
are merged in the product's repo and the retention time has passed, they're deleted, and views read
the product's record instead."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from test_drift import Notes

from valxel_sdlc import drafts, prototype, records


def approve_all(engine, fid):
    for _ in range(12):
        gates = [p for p in engine.pending(fid) if p.get("type") == "gate"]
        if not gates:
            return
        for p in gates:
            engine.decide(fid, p["interrupt_id"], {"decision": "approve", "approver": "a"})


def test_the_plan_keeps_only_what_the_product_doesnt_hold_yet():
    state = {"status": "aborted", "artifacts": {k: {} for k in ("OPP", "PRD", "UX", "PROTOTYPE", "HLD", "HANDOFF-T1")},
             "events": [{"node": "g1", "kind": "approved"}, {"node": "g1", "kind": "documents_recorded", "merged": True},
                        {"node": "g2", "kind": "approved"}, {"node": "g2", "kind": "documents_recorded", "merged": False},
                        {"node": "g3", "kind": "approved"}, {"node": "g3", "kind": "documents_not_recorded"}]}
    assert drafts.plan(state) == {"remove": ["HANDOFF-T1", "HLD", "OPP"], "keep": ["PRD", "PROTOTYPE", "UX"]}


def test_a_released_feature_keeps_what_no_record_holds():
    """Released, so no gate is "never approved" in the stopped sense: a document whose gate didn't
    run, or that no record holds, stays with the tool."""
    state = {"status": "done", "artifacts": {k: {} for k in ("OPP", "PRD", "UX", "OPS")},
             "events": [{"node": "g1", "kind": "approved"}, {"node": "g1", "kind": "documents_recorded", "merged": True}]}
    assert drafts.plan(state) == {"remove": ["OPP"], "keep": ["OPS", "PRD", "UX"]}


def test_only_finished_features_past_the_retention_time_are_due():
    now = datetime(2026, 12, 31, tzinfo=UTC)
    old = {"status": "done", "events": [{"at": (now - timedelta(days=31)).isoformat()}]}
    assert drafts.due(old, now=now) and not drafts.due(old, now=now, env={"VALXEL_DRAFT_RETENTION_DAYS": "60"})
    assert not drafts.due({**old, "status": "running"}, now=now)
    assert not drafts.due({"status": "done", "events": [{"at": (now - timedelta(days=2)).isoformat()}]}, now=now)


def test_a_released_feature_reads_its_documents_from_the_product_once_its_drafts_go(engine):
    fid = engine.create(product_id="FARM", title="Field list sorting", description="Sort the field list by moisture")
    approve_all(engine, fid)
    state = {**engine._state(fid), "feature_id": fid}
    assert state["status"] == "done", state.get("status")
    store = engine.services.artifacts
    before = engine.artifact(fid, "PRD")["content"]
    proto = prototype.page(engine.services, state)
    assert proto and store.drop(fid, "nothing") == 0

    assert drafts.remove(engine.services, engine.cluster, state) is None  # not due yet: just finished
    later = datetime.now(UTC) + timedelta(days=31)
    assert not any(e["node"] == "g5" and e["kind"] == "approved" for e in state["events"])  # a low-risk change
    files, _ = records.files_for(engine.services, state, "g6")
    assert {f"features/{fid}/plan.md", f"features/{fid}/build.md"} <= set(files)  # G6 carries what G5 would have
    done = drafts.remove(engine.services, engine.cluster, state, now=later)
    assert {"OPP", "PRD", "UX", "PROTOTYPE", "HLD", "PLAN", "BUILD", "REL", "SKETCH"} <= set(done["removed"])
    assert done["keep"] == ["OPS"] and done["files"] >= 9  # no record holds the operations report: it stays
    assert not list(store._dir(fid).glob("PRD.v*.md"))
    assert drafts.remove(engine.services, engine.cluster, state, now=later) is None  # nothing left to do

    assert engine.artifact(fid, "PRD")["content"] == before  # read from features/<fid>/prd.md
    page = prototype.page(engine.services, state)
    assert page.count(prototype.RUNTIME_JS) == 1  # the recorded copy already has the runtime: not added twice


def test_an_unmerged_record_keeps_its_gates_drafts(engine):
    fid = engine.create(product_id="FARM", title="Harvest export", description="Export the harvest log")
    gh = engine.services.github
    real = gh.merge

    def refuse(**kw):
        raise RuntimeError("Required review missing")

    p = next(x for x in engine.pending(fid) if x.get("gate") == "g1")
    engine.decide(fid, p["interrupt_id"], {"decision": "approve", "approver": "a"})
    gh.merge = refuse
    p = next(x for x in engine.pending(fid) if x.get("gate") == "g2")
    engine.decide(fid, p["interrupt_id"], {"decision": "approve", "approver": "a"})
    gh.merge = real
    p = next(x for x in engine.pending(fid) if x.get("type") == "gate")  # G3/G4: stop the feature there
    engine.decide(fid, p["interrupt_id"], {"decision": "abort", "approver": "a", "feedback": "Not now"})
    state = {**engine._state(fid), "feature_id": fid}
    out = drafts.remove(engine.services, Notes(), state, now=datetime.now(UTC) + timedelta(days=31))
    assert "PRD" in out["keep"] and "OPP" in out["removed"]
    assert list(engine.services.artifacts._dir(fid).glob("PRD.v*.md"))  # the only approved copy stays


def test_the_daily_job_runs_over_every_feature(engine):
    assert engine.remove_drafts() == 0  # nothing finished long enough ago

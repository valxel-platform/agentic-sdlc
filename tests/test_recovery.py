"""Crash recovery: a thread a replica dropped mid-step is resumed from its checkpoint by the watcher's
leader; one that stopped on an error waits for a person to resume it (engine_parts/recovery.py)."""

import time

import pytest

from valxel_sdlc.cluster import LeaseLost, stamp


def _drop_mid_step(engine):
    """The next run of any thread loses its lock after its first step, as when the replica dies."""
    real = engine.cluster.lock

    def lock(thread):
        lease = real(thread)
        if lease is None:
            return None
        calls = {"n": 0}
        alive = lease.alive

        def dying():
            calls["n"] += 1
            return calls["n"] < 2 and alive()

        lease.alive = dying
        engine.cluster.lock = real  # once
        return lease

    engine.cluster.lock = lock


def _settle(engine, fid, timeout=20.0):
    end = time.time() + timeout
    while engine.is_running(fid) and time.time() < end:
        time.sleep(0.05)
    assert not engine.is_running(fid)


def _dropped(engine):
    fid = engine.start(product_id="FARM", title="Harvest log")["feature_id"]
    [p] = engine.pending(fid)
    _drop_mid_step(engine)
    with pytest.raises(LeaseLost):
        engine.decide(fid, p["interrupt_id"], {"decision": "approve", "approver": "po"})
    return fid


def test_a_thread_dropped_mid_step_is_found_and_resumed_from_its_checkpoint(engine):
    fid = _dropped(engine)
    assert engine.pending(fid) == [] and engine.stranded_reason(fid) == ""
    assert engine.errors.get(fid) is None  # a dropped thread isn't an error: nobody saw it fail
    assert engine.recover_stranded() == [fid]
    _settle(engine, fid)
    assert [x["gate"] for x in engine.pending(fid)] == ["g2"]  # carried on to the next gate
    assert engine.stranded_reason(fid) == "waiting" and engine.recover_stranded() == []
    assert engine.recoveries.get(fid) is None  # ran through: the count starts again


def test_work_queued_on_a_replica_is_left_alone_until_that_replica_is_presumed_dead(engine, monkeypatch):
    fid = _dropped(engine)
    engine.queued[fid] = stamp()  # as if another replica had it in its queue
    assert engine.stranded_reason(fid) == "queued" and engine.recover_stranded() == []
    with pytest.raises(RuntimeError, match="about to run"):
        engine.resume(fid)
    monkeypatch.setenv("VALXEL_RUN_QUEUE_STALE_S", "0")
    assert engine.stranded_reason(fid) == ""


def test_a_thread_that_keeps_killing_its_replica_is_stopped_not_restarted_for_ever(engine, monkeypatch):
    fid = _dropped(engine)
    submitted = []
    monkeypatch.setattr(engine, "_submit", lambda t, payload: submitted.append(t))  # each restart dies too
    for _ in range(4):
        engine.recover_stranded()
    assert submitted == [fid] * 3
    assert "restarted 3 times" in engine.errors.get(fid) and engine.stranded_reason(fid) == "error"
    assert engine.recover_stranded() == []  # stays stopped for a person


def test_a_person_resumes_a_thread_stopped_on_an_error(engine):
    fid = _dropped(engine)
    engine.errors[fid] = "TimeoutError: the model didn't answer"
    assert engine.stranded_reason(fid) == "error" and engine.recover_stranded() == []
    assert engine.resume(fid, by="po") == {"feature_id": fid, "accepted": True}
    _settle(engine, fid)
    assert engine.errors.get(fid) is None and [x["gate"] for x in engine.pending(fid)] == ["g2"]


def test_nothing_to_resume_is_refused(engine):
    fid = engine.start(product_id="FARM", title="Harvest log")["feature_id"]
    with pytest.raises(RuntimeError, match="waiting for a decision"):
        engine.resume(fid)
    lease = engine.cluster.lock(fid)
    with pytest.raises(RuntimeError, match="is running"):
        engine.resume(fid)
    lease.release()


def test_resume_over_the_api_needs_the_products_org(engine):
    from fastapi.testclient import TestClient

    from valxel_sdlc.api.app import create_app
    from valxel_sdlc.web.auth import AuthConfig

    fid = _dropped(engine)
    app = create_app(engine, auth=AuthConfig(mode="dev", secret="x" * 40), web_dist=None)
    with TestClient(app) as client:
        def sign_in(login, orgs):
            assert client.post("/api/auth/dev-login", json={"login": login, "teams": [], "orgs": orgs}).status_code == 200

        sign_in("shop-person", ["acme-shop"])
        assert client.post(f"/api/features/{fid}/resume", json={}).status_code == 403
        sign_in("farm-person", ["acme-farm"])
        assert client.post(f"/api/features/{fid}/resume", json={}).status_code == 202
        _settle(engine, fid)
        assert client.post(f"/api/features/{fid}/resume", json={}).status_code == 409  # now waiting at G2
        assert client.post("/api/features/FARM-FEAT-999/resume", json={}).status_code == 404

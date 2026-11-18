"""A graph step can run again: a worker that dies mid-step leaves its last checkpoint, and the next
run of the thread repeats the step from the top (run_queue.py, engine_parts/recovery.py). What a
step does to other systems must be safe to do twice (tracker T-11; the README's table)."""

from types import SimpleNamespace

from valxel_sdlc.cluster import MemoryCluster
from valxel_sdlc.ops import changes
from valxel_sdlc.ops.servicenow import InMemoryReceipts, ServiceNowReceipts, WithReceipts


class GitHub:
    def __init__(self, fail=0):
        self.dispatched, self.fail = [], fail

    def dispatch(self, *, repo, event_type, payload):
        if self.fail:
            self.fail -= 1
            raise RuntimeError("GitHub is down")
        self.dispatched.append((repo, event_type, payload["sha"], payload["stage"]))


def test_a_store_release_is_asked_for_once_however_often_its_step_runs():
    from valxel_sdlc.graph.routing import release_store_app

    c = MemoryCluster()
    gh = GitHub(fail=1)
    services = SimpleNamespace(github=gh, ask_once=c.claim, unask=c.unclaim)
    state = {"feature_id": "SHOP-FEAT-3"}
    try:
        release_store_app(services, state, "acme-shop/shop-app", "abc123", "internal", actor="agent:g6")
    except RuntimeError:
        pass  # it didn't go: the claim is given back
    for _ in range(3):  # the step again, after a crash, and on another replica
        out = release_store_app(services, state, "acme-shop/shop-app", "abc123", "internal", actor="agent:g6")
    assert gh.dispatched == [("acme-shop/shop-app", "valxel-release", "abc123", "internal")]
    assert out["env"] == "internal"
    release_store_app(services, state, "acme-shop/shop-app", "abc123", "production", actor="rm")  # a new stage asks
    assert len(gh.dispatched) == 2
    retried = {**state, "events": [{"kind": "retry"}]}  # people asked for it again: a later checkpoint
    release_store_app(services, retried, "acme-shop/shop-app", "abc123", "production", actor="rm")
    assert len(gh.dispatched) == 3


def test_a_regression_turned_off_again_records_one_change():
    from valxel_sdlc.ops.rollout import on_regression

    log = changes.InMemoryChangeLog()
    flags = SimpleNamespace(set=lambda *a, **k: None)
    services = SimpleNamespace(flags=flags, changes=log)
    reading = {"verdict": "worse", "on_rate": 0.04, "off_rate": 0.01, "on_errors": 40, "on_requests": 1000,
               "off_errors": 10, "off_requests": 1000, "z": 4.2}
    res = {"regression": {"shop.checkout": reading}, "at": "2026-11-18T07:00:00+00:00"}
    for _ in range(2):  # the step again, minutes later
        on_regression(services, product_id="SHOP", thread_id="SHOP-REL-1", title="Checkout", res=res,
                      flags={"SHOP-FEAT-3": "shop.checkout"}, policy={"autonomous": {"flag_off": {}}})
    assert len(log.list()) == 1


def test_receipts_that_never_went_are_sent_once_later():
    sent = []

    class Spy(ServiceNowReceipts):
        def send(self, rec):
            sent.append(rec["id"])
            self.store.put(rec["id"], {"status": "queued"})

    inner = changes.InMemoryChangeLog()
    spec = {"provider": "servicenow", "instance": "https://x", "types": {}, "close": {}}
    log = WithReceipts(inner, Spy(lambda pid: spec if pid == "CLN" else None, InMemoryReceipts()))
    lost = inner.record(changes.new(key="lost", product_id="CLN", kind="release", type="standard", action="deploy",
                                    summary="its process stopped before the receipt went", targets=[]))
    inner.record(changes.new(key="farm", product_id="FARM", kind="release", type="standard", action="deploy",
                             summary="FARM sends no receipts", targets=[]))
    log.record(changes.new(key="sent", product_id="CLN", kind="release", type="standard", action="deploy",
                           summary="sent as it was recorded", targets=[]))
    assert sent == ["CHG-3"]
    assert log.catch_up() == 1 and sent == ["CHG-3", lost["id"]]
    assert log.catch_up() == 0  # nothing twice

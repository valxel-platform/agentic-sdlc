"""An initiative's features in order (T-105 to T-107): G0 says which candidates build on which; a
candidate starts only once what it needs has passed G4; G4 works out the dependencies the design
didn't list from what other features introduced; and a feature isn't released before what it
depends on is live."""

from __future__ import annotations

import time

from test_release_planning import gov_root  # noqa: F401
from test_roadmap import approved_initiative, make

from valxel_sdlc.evaluators.checks import _undo
from valxel_sdlc.roadmap.initiative import build_order, order_findings, parse_draft
from valxel_sdlc.roadmap.record import candidates
from valxel_sdlc.undo import derived, introduces

CANDS = """## Candidate features
- Walk-in queue (must): patients join a queue at the desk
- Queue screen: shows the queue in the waiting room [needs: Walk-in queue]
- SMS when called: texts the patient [needs: Walk-in queue; Queue screen]
"""


def test_g0_says_what_each_candidate_needs():
    cs = candidates(CANDS)
    assert [(c["title"], c["must"], c["needs"]) for c in cs] == [
        ("Walk-in queue", True, []), ("Queue screen", False, ["Walk-in queue"]),
        ("SMS when called", False, ["Walk-in queue", "Queue screen"])]
    assert parse_draft("# Initiative\n" + CANDS)["candidates"] == cs
    assert build_order(cs) == [["Walk-in queue"], ["Queue screen"], ["SMS when called"]]
    assert order_findings(cs) == []
    bad = candidates(CANDS.replace("[needs: Walk-in queue]", "[needs: Kiosk]"))
    assert any("needs 'Kiosk', which isn't one of the candidates" in f for f in order_findings(bad))
    loop = candidates(CANDS.replace("- Walk-in queue (must): patients join a queue at the desk",
                                    "- Walk-in queue: joins [needs: SMS when called]"))
    assert order_findings(loop) == ["The candidates' needs go round in a circle: no feature could be started first"]


FIRST = """# HLD: Walk-in queue
## Affected repos
- acme-clinic/cln-intake-api (provider)
## Contracts
- acme-clinic/cln-intake-api: 1.2.0 -> 1.3.0, adds /queue
## Data
- queue_entry: new; id, visit_id, position
- Migration: V8__queue.sql; one step: a new table
- Backfill: none
- Rollback: safe; the old version doesn't read it
## Depends on features
- None: the first of the initiative
## Flag off
- No queue, as today
"""
SECOND = FIRST.replace("Walk-in queue", "Queue screen").replace("1.2.0 -> 1.3.0", "1.3.0 -> 1.4.0").replace(
    "queue_entry: new", "queue_entry: none").replace("- None: the first of the initiative", "- None: stands alone")


def test_g4_finds_the_dependency_the_design_didnt_list():
    others = [{"feature_id": "CLN-FEAT-4", "title": "Walk-in queue", "introduces": introduces(FIRST)}]
    assert introduces(FIRST) == {"tables": ["queue_entry"], "repos": [], "versions": {"cln-intake-api": "1.3.0"},
                                 "events": []}
    assert derived(SECOND, others) == {"CLN-FEAT-4": ["table queue_entry", "cln-intake-api 1.3.0, the API version it introduces"]}
    api = [{"name": "acme-clinic/cln-intake-api", "role": "provider"}]
    [f] = _undo(SECOND, api, {"feature_id": "CLN-FEAT-5", "other_designs": others})
    assert "uses table queue_entry; cln-intake-api 1.3.0" in f and "list it ('- CLN-FEAT-4:" in f
    fixed = SECOND.replace("- None: stands alone", "- CLN-FEAT-4: the queue table and its API")
    assert _undo(fixed, api, {"feature_id": "CLN-FEAT-5", "other_designs": others}) == []
    planned = _undo(fixed, api, {"feature_id": "CLN-FEAT-5", "planned_needs": {"CLN-FEAT-3": "Intake form"}})
    assert any("G0 planned this feature to build on CLN-FEAT-3" in f for f in planned)


REQUEST = "Walk-in queue; Queue screen for the waiting room"


def test_a_candidate_starts_once_what_it_needs_passed_g4(tmp_path, gov_root):  # noqa: F811
    eng, _ = make(tmp_path, gov_root)
    iid = approved_initiative(eng, title="Walk-ins", request=REQUEST)
    s = eng._state(iid)
    d = dict(s["draft"])
    d["candidates"] = [{"title": "Walk-in queue", "description": "", "must": False, "needs": []},
                       {"title": "Queue screen", "description": "", "must": False, "needs": ["Walk-in queue"]}]
    eng._g(iid).update_state(eng._config(iid), {"draft": d})
    cands = eng.view(iid)["candidates"]
    assert [c["waiting_on"] for c in cands] == [[], ["Walk-in queue"]]
    out = eng.start_candidates(iid, [0, 1], order="together", by="maria")
    [first] = out["started"]
    assert out["waiting"] == [{"title": "Queue screen", "on": ["Walk-in queue"]}]
    assert not eng.view(iid)["candidates"][1]["started"]
    assert eng._start_unblocked(first) == []  # its design isn't settled yet
    while eng.is_running(first):
        time.sleep(0.02)
    eng.auto_drive(first)  # through G4 and on: the queue screen starts by itself
    eng._pool.shutdown(wait=True)
    v = eng.view(iid)["candidates"]
    assert v[1]["started"] and v[1]["waiting_on"] == []
    assert "needs:" + iid not in eng._queue()
    assert [f["title"] for f in eng.feature_summaries("CLN")].count("Queue screen") == 1


def test_a_feature_isnt_released_before_what_it_depends_on(tmp_path, gov_root, monkeypatch):  # noqa: F811
    from valxel_sdlc import undo

    eng, _ = make(tmp_path, gov_root)
    base = eng.create(product_id="FARM", title="Offline crop log")
    real = undo.facts
    monkeypatch.setattr(undo, "facts", lambda hld, released=(): {**real(hld, released), "depends_on": [base]})  # its G4 says so
    later = eng.create(product_id="FARM", title="Soil sensor import")
    eng.auto_drive(later)
    while eng.is_running(later):
        time.sleep(0.02)
    [w] = [p for p in eng.pending(later) if p.get("wait") == "dependencies"]
    assert base in w["label"] and "not in production yet" in w["label"]
    assert eng._release_waits(w) is None  # still not live
    monkeypatch.setattr(undo, "facts", real)
    eng.auto_drive(base)
    assert eng.dependencies_unmet(later) == []
    assert eng._release_waits(w)["status"] == "success"

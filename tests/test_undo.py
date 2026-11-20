"""Undoing one feature in production (T-101 to T-104): the architecture says what the feature
depends on, what users get with its flag off and whether its data survives a rollback; an
incident's triage then turns off the feature and its dependents first, fixes forward when a
rollback isn't safe, and never takes out a whole release while one feature can be found."""

from __future__ import annotations

from test_incidents import (  # noqa: F401
    alert,
    gate,
    gov_root,
    make,
    no_autonomy,
    released_feature,
)

from valxel_sdlc.evaluators.checks import _undo
from valxel_sdlc.ops.signals import from_alertmanager
from valxel_sdlc.undo import facts, plan

HLD = """# HLD: Walk-in queue
## Data
- visit: changed; adds a queue_position column
- Migration: V7__queue.sql; one step: a nullable column
- Backfill: none
- Rollback: not safe; the previous version rejects visits with a queue position
## Depends on features
- CLN-FEAT-3: reads the intake form it captures
## Flag off
- Visits are booked as today: no queue
"""
API = [{"name": "acme-clinic/cln-intake-api", "role": "provider"}]


def test_the_architecture_says_how_to_undo_the_feature():
    f = facts(HLD)
    assert f["depends_on"] == ["CLN-FEAT-3"] and f["flag_off"] == "Visits are booked as today: no queue"
    assert f["rollback"] == {"safe": False, "why": "the previous version rejects visits with a queue position"}
    assert facts("# HLD\n")["rollback"]["safe"] is True  # no data changes: nothing to lose
    assert _undo(HLD, API, {"feature_id": "CLN-FEAT-9"}) == []


def test_g4_asks_for_what_undoing_needs():
    bare = HLD.replace("not safe; ", "").split("## Depends on features")[0]
    found = _undo(bare, API, {"feature_id": "CLN-FEAT-9"})
    assert any("no Depends on features section" in f for f in found)
    assert any("no Flag off section" in f for f in found)
    assert any("starts with 'safe' or 'not safe'" in f for f in found)
    assert any("lists the feature itself" in f for f in _undo(HLD, API, {"feature_id": "CLN-FEAT-3"}))
    assert any("isn't '- <FEATURE-ID>" in f for f in _undo(HLD.replace("- CLN-FEAT-3:", "- the intake form:"), API, {}))


LIVE = {
    "CLN-FEAT-3": {"title": "Intake form", "flag": "intake", "flag_on": True, "depends_on": [], "repos": ["api"],
                   "rollback": {"safe": True}, "flag_off": "the paper form, as today",
                   "release": {"id": "CLN-REL-1", "size": 10}},
    "CLN-FEAT-5": {"title": "Queue", "flag": "queue", "flag_on": True, "depends_on": ["CLN-FEAT-3"], "repos": ["api"],
                   "rollback": {"safe": False, "why": "the old version rejects queued visits"}},
    "CLN-FEAT-7": {"title": "Queue screen", "flag": "screen", "flag_on": True, "depends_on": ["CLN-FEAT-5"],
                   "repos": ["web"], "rollback": {"safe": True}},
    "CLN-FEAT-8": {"title": "Reminders", "flag": "", "depends_on": ["CLN-FEAT-3"], "repos": ["jobs"]},
}


def test_a_feature_goes_off_after_the_features_that_depend_on_it():
    out = plan([{"type": "hotfix", "repo": "api", "change": "fix it", "text": "api: fix it"},
                {"type": "flag_off", "feature_id": "CLN-FEAT-3", "text": "CLN-FEAT-3"}], LIVE)
    assert [(a["type"], a.get("feature_id") or a.get("repo")) for a in out] == [
        ("flag_off", "CLN-FEAT-7"), ("flag_off", "CLN-FEAT-5"), ("flag_off", "CLN-FEAT-3"), ("hotfix", "api")]
    assert [a["id"] for a in out] == ["A1", "A2", "A3", "A4"]
    own = out[2]["text"]
    assert "CLN-FEAT-8 (Reminders) depends on it and has no flag" in own
    assert "shipped in CLN-REL-1 with 9 other features: they stay on" in own
    assert "With the flag off: the paper form, as today" in own


def test_no_rollback_the_data_cant_survive():
    [a] = plan([{"type": "rollback", "component": "api", "text": "api"}], LIVE)
    assert a["type"] == "hotfix" and a["repo"] == "api" and "old version rejects queued visits" in a["change"]
    # one hotfix per component, even when triage proposed one too
    assert [x["type"] for x in plan([{"type": "rollback", "component": "api"},
                                     {"type": "hotfix", "repo": "api", "change": "x"}], LIVE)] == ["hotfix"]
    [b] = plan([{"type": "rollback", "component": "web", "text": "web"}], LIVE)
    assert b["type"] == "rollback" and "also takes out: CLN-FEAT-7" in b["text"]


def test_triage_turns_off_the_dependents_too(tmp_path, gov_root):  # noqa: F811
    no_autonomy(gov_root)
    eng, svc = make(tmp_path, gov_root)
    dependent = released_feature(eng, title="Consent reminders")
    base = released_feature(eng)
    eng._g(dependent).update_state(eng._config(dependent), {"undo": {"depends_on": [base], "flag_off": "",
                                                                     "rollback": {"safe": True}}})
    assert eng._state(base)["undo"]["rollback"]["safe"] is True  # recorded from the stub's G4 design
    [iid] = eng.ingest(from_alertmanager(alert()))
    g = gate(eng, iid, "incident")
    assert [(a["type"], a.get("feature_id")) for a in g["actions"]][:2] == [("flag_off", dependent), ("flag_off", base)]
    eng.decide(iid, g["interrupt_id"], {"decision": "approve", "approver": "oncall-1"})
    for fid in (dependent, base):
        assert svc.flags.get("CLN", eng.view(fid)["release"]["flag"]) is False
    assert eng.turned_off("CLN") == {dependent: iid, base: iid}  # the release page names the incident


def test_a_shipped_release_names_the_incident_that_turned_a_feature_off():
    from valxel_sdlc.roadmap.views import _members

    class Eng:
        def turned_off(self, pid):
            return {"CLN-FEAT-2": "CLN-INC-4"}

        def release_members(self, rid):
            return ["CLN-FEAT-1", "CLN-FEAT-2"]

    by_id = {"CLN-FEAT-1": {"feature_id": "CLN-FEAT-1", "flag_on": True},
             "CLN-FEAT-2": {"feature_id": "CLN-FEAT-2", "flag_on": False}}
    shipped = _members(Eng(), {"product_id": "CLN", "status": "shipped"}, "CLN-REL-1", by_id)
    assert [f.get("turned_off_by") for f in shipped] == [None, "CLN-INC-4"]
    # before the release ships, a flag off is the launch keeping it dark: nothing to name
    assert not any(f.get("turned_off_by") for f in _members(Eng(), {"product_id": "CLN", "status": "ready"},
                                                             "CLN-REL-1", by_id))

"""A feature is a dependency until it's on for everyone (T-110): G4/G5 ask to list it, release
waits for 100% rather than any 'on', and an incident works out dependents from what each design
uses, so a feature at 100% that's turned off still takes its dependents off."""

from __future__ import annotations

from test_release_planning import gov_root  # noqa: F401
from test_roadmap import make

QUEUE = {"tables": ["queue_entry"], "repos": [], "versions": {}, "events": []}
USES_QUEUE = {"tables": ["queue_entry"], "repos": [], "starts": {}, "events": []}


def setup(eng):
    a = eng.create(product_id="FARM", title="Offline crop log")
    b = eng.create(product_id="FARM", title="Soil sensor import")
    for fid, undo in ((a, {"depends_on": [], "introduces": QUEUE, "uses": {}}),
                      (b, {"depends_on": [], "introduces": {}, "uses": USES_QUEUE})):
        eng._g(fid).update_state(eng._config(fid), {
            "undo": undo, "gate_decisions": [{"gate": "g4", "decision": "approve", "approver": "x", "at": ""}]})
    return a, b


def roll(eng, fid, percent, flag="farm-crop-log"):
    rel = {"production": True, "flag": flag, "flag_on": percent > 0, "flag_percent": percent}
    eng._g(fid).update_state(eng._config(fid), {"release": rel})


def test_g4_asks_to_list_a_feature_until_it_is_on_for_everyone(tmp_path, gov_root):  # noqa: F811
    eng, _ = make(tmp_path, gov_root)
    a, b = setup(eng)
    assert a in [d["feature_id"] for d in eng.design_facts("FARM", exclude=b)]  # not in production
    roll(eng, a, 0)
    assert a in [d["feature_id"] for d in eng.design_facts("FARM", exclude=b)]  # dark
    roll(eng, a, 10)
    assert a in [d["feature_id"] for d in eng.design_facts("FARM", exclude=b)]  # partly on
    roll(eng, a, 100)
    assert a not in [d["feature_id"] for d in eng.design_facts("FARM", exclude=b)]  # part of the product
    assert a in [d["feature_id"] for d in eng.design_facts("FARM", exclude=b, include_full=True)]


def test_release_waits_for_100_percent_even_when_the_design_didnt_list_it(tmp_path, gov_root):  # noqa: F811
    eng, _ = make(tmp_path, gov_root)
    a, b = setup(eng)
    roll(eng, a, 10)
    [d] = eng.dependencies_unmet(b)
    assert d["feature_id"] == a and d["where"] == "on for 10% of users, not everyone"
    roll(eng, a, 0)
    assert eng.dependencies_unmet(b)[0]["where"] == "in production with its flag off"
    roll(eng, a, 100)
    assert eng.dependencies_unmet(b) == []


def test_turning_off_a_feature_at_100_percent_takes_what_uses_it_off_too(tmp_path, gov_root):  # noqa: F811
    eng, _ = make(tmp_path, gov_root)
    a, b = setup(eng)
    roll(eng, a, 100)
    roll(eng, b, 100, flag="farm-soil-import")
    out = eng.undo_plan("FARM", [{"type": "flag_off", "feature_id": a, "text": a}])
    assert [(x["type"], x["feature_id"]) for x in out] == [("flag_off", b), ("flag_off", a)]
    assert f"depends on {a}" in out[0]["text"]

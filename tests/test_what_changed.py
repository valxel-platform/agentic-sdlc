"""What changed around an alert (valxel_sdlc.what_changed): the setup, the model that answered, or a
product's governance, lessons and prompt sections."""

from types import SimpleNamespace

from valxel_sdlc import what_changed as changes
from valxel_sdlc.gateway.ledger import UsageRecord

SINCE, UNTIL = "2026-11-12T00:00:00+00:00", "2026-11-19T00:00:00+00:00"
BEFORE, DURING = "2026-11-05T10:00:00+00:00", "2026-11-14T10:00:00+00:00"


def rec(ts, *, role="spec", config="cfg-A", served="claude-sonnet-5-5", product="FARM", layers=None):
    return UsageRecord(ts=ts, product_id=product, feature_id="F1", phase="spec", role=role, runtime="generic",
                       provider="anthropic", model="anthropic/claude-sonnet-5-5", workspace="w", input_tokens=1,
                       cache_creation_input_tokens=0, cache_read_input_tokens=0, output_tokens=1, cache_ttl="1h",
                       prompt_version="spec@1", config_version=config, served_model=served,
                       layer_tokens=layers or {"L3": 2000, "L4": 3000, "L6": 4000})


class Notes(dict):
    def put(self, k, v):
        self[k] = v


def test_nothing_moved_points_at_the_provider():
    got = changes.what_changed(None, Notes(), [rec(BEFORE), rec(DURING)], since=SINCE, until=UNTIL)
    assert got["setup"] == [] and got["models"] == [] and got["products"] == {}
    assert "suspect the provider's serving" in changes.summary(got)


def test_a_new_setup_and_a_new_model_behind_the_same_name():
    rs = [rec(BEFORE), rec(DURING, config="cfg-B", served="claude-sonnet-5-5-20261110"), rec(DURING, role="critic")]
    got = changes.what_changed(None, Notes(), rs, since=SINCE, until=UNTIL)
    assert got["setup"] == [{"config": "cfg-B", "first_seen": DURING}]
    assert got["models"] == [{"role": "spec", "answered": "claude-sonnet-5-5-20261110",
                              "before": ["claude-sonnet-5-5"], "first_seen": DURING}]
    s = changes.summary(got)
    assert s.startswith("What changed: setup cfg-B went live 2026-11-14; spec answered by claude-sonnet-5-5-20261110")


def test_a_product_that_added_lessons_and_grew_its_prompt():
    notes = Notes()
    snaps = {"FARM": SimpleNamespace(pins={"product": {"sha": "a" * 40}}, lessons=())}
    services = SimpleNamespace(settings=SimpleNamespace(registry={"FARM": None}), snapshot=lambda pid: snaps[pid])
    changes.observe(services, notes, at=BEFORE)
    changes.observe(services, notes, at="2026-11-10T00:00:00+00:00")  # unchanged: not recorded again
    snaps["FARM"] = SimpleNamespace(pins={"product": {"sha": "b" * 40}},
                                    lessons=(SimpleNamespace(id="L-007"), SimpleNamespace(id="L-008")))
    changes.observe(services, notes, at=DURING)
    assert len(notes[changes.note_key("FARM")]) == 2
    rs = [rec(BEFORE), rec(DURING, layers={"L3": 2000, "L4": 3000, "L6": 9000})]
    got = changes.what_changed(services, notes, rs, since=SINCE, until=UNTIL)
    farm = got["products"]["FARM"]
    assert farm["governance"]["lessons_added"] == ["L-007", "L-008"] and farm["governance"]["commits"][0]["sha"] == "b" * 12
    assert farm["prompt_growth"] == {"task and lessons": {"before": 4000, "now": 9000, "change": 1.25}}
    assert "FARM added lessons L-007, L-008" in changes.summary(got)


def test_small_growth_is_not_growth():
    rs = [rec(BEFORE, layers={"L4": 1000}), rec(DURING, layers={"L4": 1400})]  # +40% but only 400 tokens
    assert changes.what_changed(None, Notes(), rs, since=SINCE, until=UNTIL)["products"] == {}


def test_the_health_report_and_its_alerts_say_what_changed(engine):
    from valxel_sdlc import health

    said = []
    engine.start(product_id="FARM", title="Soil sensor import")
    result = health.run(engine, alert=said.append)
    assert {"setup", "models", "products"} <= set(result["what_changed"])
    assert engine.cluster.get(changes.note_key("FARM"))  # governance observed


def test_the_stand_in_records_the_model_it_answers_as(engine):
    engine.start(product_id="FARM", title="Soil sensor import")
    recs = engine.services.ledger.records()
    assert recs and all(r.served_model == r.model.split("/", 1)[-1] for r in recs if r.runtime == "generic")


def test_claude_code_names_the_model_that_did_most_of_the_work():
    from valxel_sdlc.runtime.claude_code import served_of

    assert served_of({"claude-haiku-5-5": {"outputTokens": 40}, "claude-sonnet-5-5": {"outputTokens": 900}}) == \
        "claude-sonnet-5-5"
    assert served_of({}) == ""

import dataclasses

import pytest

from vrixel_sdlc.agents.parse import parse_acs
from vrixel_sdlc.config import RoleConfig
from vrixel_sdlc.constitution import load_constitution
from vrixel_sdlc.evaluators import run_checks
from vrixel_sdlc.gateway import CallTags, InMemoryLedger, ModelGateway, PolicyViolation, PromptLayers, StubProvider
from vrixel_sdlc.gateway.pricing import estimate_cost


def layers(**kw):
    base = dict(tools="t" * 400, skills="s" * 4000, constitution="c" * 1200, product_context="p" * 800,
                feature_context="f" * 2000, tail="do the task")
    return PromptLayers(**{**base, **kw})


def tags(**kw):
    base = dict(product_id="AG", feature_id="AG-FEAT-1", phase="build", role="coder",
                data_class="general", workspace="wrkspc-shared")
    return CallTags(**{**base, **kw})


def test_cache_hits_on_an_identical_prefix_and_misses_across_workspaces(settings):
    ledger = InMemoryLedger()
    gw = ModelGateway(settings, ledger, {"stub": StubProvider()})
    first = gw.complete(tags(), layers()).response
    second = gw.complete(tags(), layers(tail="next step")).response
    other_ws = gw.complete(tags(workspace="wrkspc-emr"), layers()).response
    assert first.cache_read_input_tokens == 0 and first.cache_creation_input_tokens > 0
    assert second.cache_read_input_tokens == first.cache_creation_input_tokens
    assert second.cache_creation_input_tokens == 0
    assert other_ws.cache_read_input_tokens == 0  # caches are isolated per workspace
    assert len(ledger.records()) == 3


def test_changing_a_middle_layer_keeps_only_the_prefix_before_it(settings):
    gw = ModelGateway(settings, InMemoryLedger(), {"stub": StubProvider()})
    gw.complete(tags(), layers())
    r = gw.complete(tags(), layers(product_context="different product pack")).response
    tokens = layers().token_counts()
    assert r.cache_read_input_tokens == tokens["L1"] + tokens["L2"]  # hit at BP1 only


def test_cache_entries_expire_after_their_ttl(settings):
    clock = [0.0]
    gw = ModelGateway(settings, InMemoryLedger(), {"stub": StubProvider(clock=lambda: clock[0])})
    gw.complete(tags(), layers())
    clock[0] = 3601  # past the 1-hour TTL
    assert gw.complete(tags(), layers()).response.cache_read_input_tokens == 0


def test_volatile_values_in_stable_layers_are_detected():
    assert layers(constitution="Current time 2026-10-27T13:30:05").volatile_layers() == ["L3"]


def test_phi_cannot_reach_a_provider_outside_the_allowlist(settings):
    settings.roles["coder"] = dataclasses.replace(settings.roles["coder"], model="otherai/model-x")
    gw = ModelGateway(settings, InMemoryLedger(), {"stub": StubProvider()})
    with pytest.raises(PolicyViolation):
        gw.complete(tags(data_class="phi"), layers())
    gw.complete(tags(data_class="general"), layers())  # general data may go anywhere


def test_list_price_math():
    usd, basis = estimate_cost("anthropic/claude-sonnet-5-5", input_tokens=1_000_000, cache_creation_input_tokens=0,
                               cache_read_input_tokens=1_000_000, output_tokens=100_000)
    assert basis == "list" and usd == pytest.approx(2.0 + 0.10 + 1.0)
    _, unknown = estimate_cost("otherai/x", input_tokens=1, cache_creation_input_tokens=0,
                               cache_read_input_tokens=0, output_tokens=1)
    assert unknown == "unknown"


def test_constitutions_combine_platform_and_product(settings):
    con = load_constitution(str(settings.constitution_dir), "emr")
    assert con.version == "platform@1.0.0+emr@1.0.0"
    assert "VX-3" in con.article_ids and "EMR-8" in con.article_ids
    assert all(a.startswith("EMR-") for a in con.product_articles())


def test_spec_check_flags_untestable_acceptance_criteria():
    prd = ("## Goals\n- G1\n\n## Non-goals\n- x\n\n## Acceptance criteria\n- AC-1: it works\n"
           "- AC-2: Given a user, when they save, then it persists\n\n## Constitution articles\n- VX-5\n")
    assert list(parse_acs(prd)) == ["AC-1", "AC-2"]
    findings = run_checks("spec", prd, {"articles": ["VX-5"]})
    assert findings == ["AC-1 is not testable: write it as Given / when / then"]

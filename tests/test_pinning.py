"""Every model is pinned (valxel_sdlc.pinning): a provider can't swap what a name means."""

import pytest

from valxel_sdlc.config import ProviderConfig, RoleConfig
from valxel_sdlc.gateway.pricing import estimate_cost, has_price
from valxel_sdlc.pinning import undated, why_unpinned

PROVIDERS = {"selfhosted": ProviderConfig(name="selfhosted", base_url="http://vllm:8000/v1"),
             "idle": ProviderConfig(name="idle", base_url_env="VALXEL_IDLE_URL_UNSET")}


@pytest.mark.parametrize("model,ok", [
    ("anthropic/claude-sonnet-5-5", True),            # 4.6+ IDs are pinned as written
    ("anthropic/claude-opus-4-6", True),
    ("anthropic/claude-sonnet-4-5-20250929", True),   # an older generation, dated
    ("anthropic/claude-sonnet-4-5", False),           # an older alias
    ("anthropic/claude-3-7-sonnet-latest", False),
    ("openai/gpt-5.4-2026-03-05", True),
    ("openai/gpt-5.4", False),
    ("openai/gpt-5.4-latest", False),
    ("selfhosted/qwen3-coder", False),                 # in use, but no `pinned:` on its provider
    ("idle/anything", True),                           # no URL here: not in use
    ("nowhere/model", False),
])
def test_what_counts_as_pinned(model, ok):
    assert (why_unpinned(model, PROVIDERS) == "") is ok


def test_a_self_hosted_provider_pins_by_what_it_serves():
    pinned = {"selfhosted": ProviderConfig(name="selfhosted", base_url="http://vllm:8000/v1",
                                           pinned="Qwen/Qwen3-Coder@3b1f7c2")}
    assert why_unpinned("selfhosted/qwen3-coder", pinned) == ""


def test_a_pinned_snapshot_keeps_its_models_price_and_window(services):
    assert undated("gpt-5.4-2026-03-05") == "gpt-5.4" and undated("claude-sonnet-4-5-20250929") == "claude-sonnet-4-5"
    prices = {"gpt-5.4": (2.5, 15.0, 0.25)}
    assert has_price("openai/gpt-5.4-2026-03-05", prices)
    assert estimate_cost("openai/gpt-5.4-2026-03-05", input_tokens=1_000_000, cache_creation_input_tokens=0,
                         cache_read_input_tokens=0, output_tokens=0, prices=prices) == (2.5, "list")
    services.settings.context_windows["openai/gpt-5.4"] = 1_050_000
    assert services.settings.context_window("openai/gpt-5.4-2026-03-05") == 1_050_000


def test_the_shipped_config_is_pinned(services, monkeypatch):
    from valxel_sdlc.pinning import unpinned

    monkeypatch.delenv("VALXEL_SELFHOSTED_URL", raising=False)
    assert unpinned(services.settings) == []  # the self-hosted server isn't in use until its URL is set
    monkeypatch.setenv("VALXEL_SELFHOSTED_URL", "http://vllm:8000/v1")
    assert [line.split(":")[0] for line in unpinned(services.settings)] == ["role coder_open", "role reviewer_open"]


def test_strict_mode_refuses_an_unpinned_model_that_can_run(services, monkeypatch):
    from valxel_sdlc import strict

    s = services.settings
    s.roles["spec"] = RoleConfig(runtime="generic", model="anthropic/claude-sonnet-4-5")
    got = strict.unpinned_models(s, {"VALXEL_MODE": "live"})
    assert any(line.startswith("role spec: anthropic/claude-sonnet-4-5: name the dated snapshot") for line in got)
    assert strict.unpinned_models(s, {"VALXEL_MODE": "stub"}) == []

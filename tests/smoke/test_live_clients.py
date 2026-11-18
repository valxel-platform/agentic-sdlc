"""The live clients against recorded exchanges with GitHub and the LLM gateway (T-18)."""

import os
from types import SimpleNamespace

import httpx

from valxel_sdlc.cassettes import Cassette, httpx_transport, urllib_opener
from valxel_sdlc.gateway.prompt import PromptLayers
from valxel_sdlc.gateway.providers import AnthropicProvider, ModelRequest
from valxel_sdlc.integrations.github import GitHubRest

from . import fakes
from .conftest import RECORD, SEED

REPO = os.environ.get("VALXEL_SMOKE_GITHUB_REPO", "valxel-platform/sdlc-smoke")
GATEWAY = os.environ.get("VALXEL_LLM_GATEWAY_URL", "http://llm-gateway.valxel-llm-gateway.svc.cluster.local:8080")


class _Token:
    def token(self, org: str) -> str:
        return os.environ.get("VALXEL_SMOKE_GITHUB_TOKEN", "replayed") if RECORD else "replayed"


def _gateway_key() -> str:
    return os.environ.get("VALXEL_LLM_GATEWAY_API_KEY", "replayed") if RECORD else "replayed"


def _http(cassette: Cassette, module=httpx):
    real = (fakes.gateway(module) if SEED else None)
    return module.Client(transport=httpx_transport(cassette, real=real, module=module))


def _anthropic(cassette: Cassette):
    """The Anthropic SDK's client on the cassette, with the httpx it takes (httpx2 in newer SDKs)."""
    import anthropic

    for name in ("httpx2", "httpx"):
        try:
            module = __import__(name)
            return anthropic.Anthropic(base_url=GATEWAY, api_key=_gateway_key(), http_client=_http(cassette, module),
                                       max_retries=0)
        except (ImportError, TypeError):
            continue
    raise RuntimeError("the Anthropic SDK took neither httpx2 nor httpx")


def test_github_a_change_goes_from_a_branch_to_a_merged_pull_request(cassette):
    gh = GitHubRest(_Token(), opener=urllib_opener(cassette, real=fakes.github() if SEED else None))
    put = gh.put_file(repo=REPO, branch="valxel/smoke", path="smoke/hello.md",
                      content="Hello from the tool's live smoke test.\n", message="smoke: hello")
    pr = gh.open_pr(repo=REPO, branch="valxel/smoke", title="Smoke: hello", body="The tool's live smoke test.",
                    files=["smoke/hello.md"], labels=["valxel"])
    assert pr["number"] and pr["state"] == "open" and pr["labels"] == ["valxel"] and put["sha"] == pr["head_sha"]
    ci = gh.checks(repo=REPO, sha=pr["head_sha"])
    assert ci["state"] == "success"
    merged = gh.merge(repo=REPO, number=pr["number"], actor="smoke", sha=pr["head_sha"])
    assert merged["state"] == "merged" and merged["merge_sha"]


def test_gateway_a_session_token_is_minted_capped_and_revoked(cassette):
    from valxel_sdlc import gateway_sessions

    tags = SimpleNamespace(product_id="valxel", feature_id="SMOKE-1", phase="build", role="coder", workspace="",
                           data_class="general")
    env = {"VALXEL_LLM_GATEWAY_URL": GATEWAY, "VALXEL_LLM_GATEWAY_API_KEY": _gateway_key()}
    with _http(cassette) as http, gateway_sessions.mint(tags, ["claude-sonnet-5-5"], 0.5, 600, http=http, env=env) as (url, tok):
        assert url == GATEWAY.rstrip("/") and tok


def test_model_a_call_through_the_gateway_carries_its_labels_and_reports_usage(cassette):
    provider = AnthropicProvider(client=_anthropic(cassette), routed=True)
    layers = PromptLayers(tools="", skills="Write PRDs.", constitution="Ids, never field values.",
                          product_context="FARM: a farm-management product.", feature_context="",
                          tail="Write the PRD for a harvest export: a CSV of each field's yield by season.")
    res = provider.complete(ModelRequest(model="anthropic/claude-sonnet-5-5", layers=layers, workspace="", max_tokens=256,
                                         labels={"product": "FARM", "feature": "SMOKE-1", "phase": "spec", "agent": "spec"},
                                         data_class="general"))
    assert res.text.startswith("# PRD") and res.input_tokens > 0 and res.output_tokens > 0 and res.request_id

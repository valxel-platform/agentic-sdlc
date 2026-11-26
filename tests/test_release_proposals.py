"""The tool proposes its own releases: a signed image from main moves its pin in the deployment
repository's releases file, on one release branch and PR (valxel_sdlc.release_proposals)."""

import hashlib
import hmac
import json

import pytest
from fastapi.testclient import TestClient

from valxel_sdlc.api.app import create_app
from valxel_sdlc.cluster import MemoryCluster
from valxel_sdlc.integrations.github import StubGitHub
from valxel_sdlc.release_proposals import CONTEXT, Proposer, carry, pin, pins
from valxel_sdlc.web.auth import AuthConfig

REPO, PATH = "valxel-platform/platform-foundation", "gitops/clusters/tools/releases.yaml"
BRANCH = "release/valxel-images"
A, B, C, D = ("sha256:" + c * 64 for c in "abcd")
RELEASES = f"""# The images this cluster runs.
apiVersion: v1
kind: ConfigMap
data:
  # agentic-sdlc
  sdlc_tool_image: ghcr.io/valxel-platform/agentic-sdlc:main@{A}
  llm_gateway_image: ghcr.io/valxel-shared-services/ss-llm-gateway:main@{B}   # the gateway
  pinned_release: ghcr.io/valxel-platform/agentic-sdlc:v1.2.0@{A}
"""


def status(repo="valxel-platform/agentic-sdlc", digest=C, branches=("main",), state="success", context=CONTEXT):
    return {"context": context, "state": state, "description": digest, "sha": "1234567890abcdef",
            "target_url": "https://github.com/x/actions/runs/1", "repository": {"full_name": repo},
            "branches": [{"name": b} for b in branches]}


@pytest.fixture
def gh():
    g = StubGitHub()
    g.files[(REPO, "main", PATH)] = RELEASES
    return g


@pytest.fixture
def proposer(gh):
    return Proposer(gh, MemoryCluster(), REPO, wait_s=1)


def release_prs(gh):
    return [p for p in gh.prs.values() if p["repo"] == REPO and p["branch"] == BRANCH]


def test_reads_and_moves_pins():
    assert pins(RELEASES)["llm_gateway_image"] == {"repo": "valxel-shared-services/ss-llm-gateway", "tag": "main",
                                                   "digest": B}
    out, changed = pin(RELEASES, "Valxel-Platform/agentic-sdlc", "main", C)
    assert changed == ["sdlc_tool_image"]
    assert f"agentic-sdlc:main@{C}" in out and f"agentic-sdlc:v1.2.0@{A}" in out  # a release tag stays
    assert out.count("\n") == RELEASES.count("\n") and "# the gateway" in out  # comments and lines kept


def test_carries_what_the_open_pr_already_proposes():
    waiting, _ = pin(RELEASES, "valxel-shared-services/ss-llm-gateway", "main", D)
    out, _ = pin(carry(RELEASES, waiting), "valxel-platform/agentic-sdlc", "main", C)
    assert pins(out)["llm_gateway_image"]["digest"] == D and pins(out)["sdlc_tool_image"]["digest"] == C


def test_a_signed_image_from_main_opens_the_release_pr(gh, proposer):
    got = proposer.on_status(status())
    assert got["changed"] == ["sdlc_tool_image"]
    [pr] = release_prs(gh)
    assert pr["title"] == "Release agentic-sdlc" and f"`{A[7:19]}` | `{C[7:19]}`" in pr["body"]
    assert pins(gh.files[(REPO, BRANCH, PATH)])["sdlc_tool_image"]["digest"] == C
    assert gh.files[(REPO, "main", PATH)] == RELEASES  # only proposed: main is a person's to merge


def test_a_second_image_joins_the_open_pr(gh, proposer):
    proposer.on_status(status())
    proposer.on_status(status("valxel-shared-services/ss-llm-gateway", D))
    [pr] = release_prs(gh)
    proposed = pins(gh.files[(REPO, BRANCH, PATH)])
    assert proposed["sdlc_tool_image"]["digest"] == C and proposed["llm_gateway_image"]["digest"] == D
    assert pr["title"] == "Release agentic-sdlc, ss-llm-gateway"
    # A newer build of the first replaces its pin; the other update stays.
    proposer.on_status(status(digest=D))
    proposed = pins(gh.files[(REPO, BRANCH, PATH)])
    assert proposed["sdlc_tool_image"]["digest"] == D and proposed["llm_gateway_image"]["digest"] == D


def test_after_a_merge_the_next_release_starts_from_main(gh, proposer):
    proposer.on_status(status())
    [pr] = release_prs(gh)
    gh.merge(repo=REPO, number=pr["number"], actor="sunil")
    proposer.on_status(status("valxel-shared-services/ss-llm-gateway", D))
    new = [p for p in release_prs(gh) if p["state"] == "open"]
    assert len(new) == 1 and new[0]["number"] != pr["number"]
    assert new[0]["title"] == "Release ss-llm-gateway"  # the merged one's update isn't proposed again


def test_a_closed_pr_isnt_carried(gh, proposer):
    proposer.on_status(status("valxel-shared-services/ss-llm-gateway", D))
    [pr] = release_prs(gh)
    pr["state"] = "closed"  # someone turned that release down
    proposer.on_status(status())
    assert pins(gh.files[(REPO, BRANCH, PATH)])["llm_gateway_image"]["digest"] == B


@pytest.mark.parametrize("event, why", [
    (status(state="pending"), "not a published image"),
    (status(digest="sha256:short"), "the description isn't a digest"),
    (status(branches=("feature/x",)), "not built from main"),
])
def test_ignores_what_isnt_a_signed_image_from_main(gh, proposer, event, why):
    assert proposer.on_status(event) == {"ignored": why}
    assert not release_prs(gh)


def test_ignores_other_statuses_and_unpinned_repositories(gh, proposer):
    assert proposer.on_status(status(context="ci/build")) is None
    assert "isn't pinned" in proposer.on_status(status("valxel-platform/someone-else"))["ignored"]
    assert "already pinned" in proposer.on_status(status(digest=A))["ignored"]
    assert not release_prs(gh)


def test_waits_its_turn_and_says_so_if_it_cant_get_one(gh):
    cluster = MemoryCluster()
    held = cluster.lock("release-proposal")
    with pytest.raises(TimeoutError):
        Proposer(gh, cluster, REPO, wait_s=0.1).on_status(status())
    held.release()
    assert Proposer(gh, cluster, REPO, wait_s=0.1).on_status(status())["changed"] == ["sdlc_tool_image"]


def test_off_unless_the_deployment_repository_is_set():
    assert Proposer.from_env(StubGitHub(), MemoryCluster(), {}) is None
    p = Proposer.from_env(StubGitHub(), MemoryCluster(), {"VALXEL_RELEASE_REPO": REPO})
    assert (p.repo, p.path, p.branch) == (REPO, PATH, BRANCH)


def test_the_webhook_proposes_it(monkeypatch, engine, services):
    monkeypatch.setenv("VALXEL_GITHUB_WEBHOOK_SECRET", "s3cret")
    client = TestClient(create_app(engine=engine, auth=AuthConfig(mode="dev", secret="x" * 32), web_dist=None))
    services.github.files[(REPO, "main", PATH)] = RELEASES
    body = json.dumps(status()).encode()
    headers = {"x-hub-signature-256": "sha256=" + hmac.new(b"s3cret", body, hashlib.sha256).hexdigest(),
               "x-github-event": "status", "content-type": "application/json"}
    assert client.post("/api/webhooks/github", content=body, headers=headers).json()["release"] == "off"
    monkeypatch.setenv("VALXEL_RELEASE_REPO", REPO)
    got = client.post("/api/webhooks/github", content=body, headers=headers)
    assert got.status_code == 202 and got.json()["release"]["changed"] == ["sdlc_tool_image"]
    assert release_prs(services.github)

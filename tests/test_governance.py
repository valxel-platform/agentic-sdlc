"""Product governance is read from each product's own org, validated, and pinned by commit."""

import json
import shutil
from io import BytesIO
from pathlib import Path
from urllib.error import HTTPError

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from conftest import approve
from vrixel_sdlc.config import load_settings
from vrixel_sdlc.engine import FeatureEngine
from vrixel_sdlc.governance import (
    EnvTokenProvider,
    GitHubAppTokenProvider,
    GitHubSource,
    GovernanceError,
)
from vrixel_sdlc.services import build_services

EXAMPLES = Path(__file__).resolve().parents[1] / "examples" / "governance"


@pytest.fixture
def gov_root(tmp_path):
    root = tmp_path / "governance"
    shutil.copytree(EXAMPLES, root)
    return root


@pytest.fixture
def local_services(tmp_path, gov_root):
    s = load_settings(mode="stub", database_url="", artifact_dir=tmp_path / "artifacts",
                      governance_source="local", governance_root=gov_root)
    return build_services(s)


def edit(path: Path, old: str, new: str) -> None:
    text = path.read_text()
    assert old in text, f"{old!r} not in {path}"
    path.write_text(text.replace(old, new))


def test_snapshot_reads_platform_and_product_from_their_orgs(services):
    snap = services.snapshot("EMR")
    assert snap.product.org == "vrixel-emr"
    assert snap.pins["platform"]["org"] == "vrixel-tools"
    assert snap.pins["product"]["org"] == "vrixel-emr"
    assert {"VX-3", "EMR-8"} <= set(snap.constitution.article_ids)
    prefixes = {a.split("-")[0] for a in snap.constitution.product_articles()}
    assert prefixes == {"EMR", "SS", "APX"}  # its own articles plus those of the lanes it depends on
    assert set(snap.pins["dependencies"]) == {"SS", "APX"}
    assert snap.version.startswith("platform@1.3.0#") and "+emr@1.0.0#" in snap.version
    assert "FHIR R4" in snap.product_context  # context.md feeds the L4 prompt layer
    assert all(r.name.startswith("vrixel-emr/") for r in snap.product.repos)


@pytest.mark.parametrize("old,new,message", [
    ("critic_threshold: 0.8", "critic_threshold: 0.5", "critic_threshold 0.5 must be between 0.75"),
    ("max_loops: 3", "max_loops: 9", "max_loops 9 must be between 1 and 5"),
    ("budget_usd: 150", "budget_usd: 5000", "budget_usd 5000.0 must be above 0 and at most 500"),
    ("name: vrixel-emr/emr-web", "name: vrixel-pos/emr-web", "outside the product's organization vrixel-emr"),
    ("product_id: EMR", "product_id: EMR\ndata_class: general", "data classes are set by the platform"),
    ("  g5:\n  - vrixel-emr/tech-leads\n", "  g5: []\n", "gate g5 has no approvers"),
    ("  - vrixel-emr/architects", "  - vrixel-pos/architects", "outside the product's organization vrixel-emr"),
    ("  - vrixel-emr/architects", "  - not a handle!", "must be a GitHub user"),
    ("product_id: EMR", "product_id: POS", "the registry expects 'EMR'"),
])
def test_manifest_cannot_loosen_platform_limits(local_services, gov_root, old, new, message):
    edit(gov_root / "vrixel-emr/governance/product.yaml", old, new)
    with pytest.raises(GovernanceError, match=message):
        local_services.snapshot("EMR")


def test_product_constitution_cannot_redefine_platform_articles(local_services, gov_root):
    con = gov_root / "vrixel-emr/governance/constitution.md"
    con.write_text(con.read_text() + "\n- **VX-3** Agents may promote to production. (Policy)\n")
    with pytest.raises(GovernanceError, match="may not define platform articles: VX-3"):
        local_services.snapshot("EMR")


def test_missing_governance_repo_blocks_intake(tmp_path, gov_root):
    shutil.rmtree(gov_root / "vrixel-pos")
    s = load_settings(mode="stub", database_url="", artifact_dir=tmp_path / "a",
                      governance_source="local", governance_root=gov_root)
    engine = FeatureEngine(build_services(s), InMemorySaver())
    with pytest.raises(GovernanceError, match="vrixel-pos/governance not found"):
        engine.start(product_id="POS", title="Tip jar")
    assert engine.index.all() == []  # nothing registered for a product it could not govern


def test_a_feature_keeps_its_pinned_constitution_after_an_amendment(local_services, gov_root):
    engine = FeatureEngine(local_services, InMemorySaver())
    first = engine.start(product_id="EMR", title="Consent check")
    pinned_version = first["constitution_version"]

    con = gov_root / "vrixel-emr/governance/constitution.md"
    edit(con, "version: 1.0.0", "version: 1.1.0")
    con.write_text(con.read_text() + "\n- **EMR-9** New consent rule. (Test)\n")

    v = engine.auto_drive(first["feature_id"])
    assert v["status"] == "done"
    assert v["constitution_version"] == pinned_version  # the amendment did not change a running feature
    snap = local_services.snapshot("EMR", v["governance"])
    assert "EMR-9" not in snap.constitution.article_ids

    second = engine.start(product_id="EMR", title="Allergy list")
    assert "emr@1.1.0" in second["constitution_version"]  # new features pick up the amendment
    assert second["governance"]["product"]["sha"] != first["governance"]["product"]["sha"]


def test_raised_budget_is_capped_by_the_platform(local_services):
    engine = FeatureEngine(local_services, InMemorySaver())
    v = engine.start(product_id="AG", title="Weather alerts", budget_usd=0.000001)
    v = approve(engine, v["feature_id"], "g1")
    [esc] = v["pending"]
    v = engine.decide(v["feature_id"], esc["interrupt_id"], {"decision": "retry", "approver": "pm", "budget_usd": 10_000})
    assert v["budget_usd"] == 500


class FakeResponse(BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeGitHub:
    """Minimal fake of the GitHub REST endpoints the source and token provider call."""

    def __init__(self, repos: dict[str, dict[str, str]], sha: str = "a" * 40):
        self.repos, self.sha, self.calls = repos, sha, []

    def open(self, req, timeout=None):
        url, auth = req.full_url, req.headers.get("Authorization")
        self.calls.append((req.get_method(), url, auth, req.headers.get("Accept")))
        path = url.split("https://api.github.com", 1)[1]
        if path.startswith("/orgs/") and path.endswith("/installation"):
            return FakeResponse(json.dumps({"id": 77}).encode())
        if path.startswith("/app/installations/77/access_tokens"):
            return FakeResponse(json.dumps({"token": "ghs_inst", "expires_at": "2099-01-01T00:00:00Z"}).encode())
        parts = path.split("/")
        repo = f"{parts[2]}/{parts[3]}"
        if repo not in self.repos:
            raise HTTPError(url, 404, "Not Found", {}, None)
        if parts[4] == "commits":
            return FakeResponse(self.sha.encode())
        file_path, _, query = "/".join(parts[5:]).partition("?ref=")
        if query != self.sha or file_path not in self.repos[repo]:
            raise HTTPError(url, 404, "Not Found", {}, None)
        return FakeResponse(self.repos[repo][file_path].encode())


def _repo_files(org_repo: str) -> dict[str, str]:
    base = EXAMPLES / org_repo
    return {str(p.relative_to(base)): p.read_text() for p in base.rglob("*") if p.is_file()}


def test_github_source_uses_a_token_per_org_and_pins_by_sha(tmp_path):
    repos = ["vrixel-tools/sdlc-governance", "vrixel-emr/governance", "vrixel-shared-services/governance",
             "vrixel-apx/governance"]
    fake = FakeGitHub({r: _repo_files(r) for r in repos})
    tokens = EnvTokenProvider({"VRIXEL_GITHUB_TOKEN__VRIXEL_TOOLS": "tok-tools",
                               "VRIXEL_GITHUB_TOKEN__VRIXEL_EMR": "tok-emr",
                               "VRIXEL_GITHUB_TOKEN__VRIXEL_SHARED_SERVICES": "tok-ss",
                               "VRIXEL_GITHUB_TOKEN__VRIXEL_APX": "tok-apx"})
    s = load_settings(mode="stub", database_url="", artifact_dir=tmp_path / "a", governance_source="github")
    services = build_services(s, governance_source=GitHubSource(tokens, opener=fake))

    snap = services.snapshot("EMR")
    assert snap.pins["product"]["sha"] == "a" * 40
    assert "#aaaaaaa" in snap.version
    auth_by_org = {url.split("/repos/")[1].split("/")[0]: auth for _, url, auth, _ in fake.calls}
    assert auth_by_org == {"vrixel-tools": "Bearer tok-tools", "vrixel-emr": "Bearer tok-emr",
                           "vrixel-shared-services": "Bearer tok-ss", "vrixel-apx": "Bearer tok-apx"}
    assert any("?ref=" + "a" * 40 in url for _, url, _, _ in fake.calls)  # reads are pinned to the commit

    n = len(fake.calls)
    services.snapshot("EMR", snap.pins)
    assert len(fake.calls) == n  # pinned snapshots are cached; no further API calls

    with pytest.raises(GovernanceError, match="No GitHub token for org vrixel-pos"):
        services.snapshot("POS")


def test_github_app_mints_a_read_only_installation_token_per_org():
    jwt = pytest.importorskip("jwt")
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption()).decode()
    fake = FakeGitHub({})
    provider = GitHubAppTokenProvider("12345", pem, opener=fake)
    assert provider.token("vrixel-emr") == "ghs_inst"
    assert provider.token("vrixel-emr") == "ghs_inst"
    methods = [(m, u.split(".com")[1]) for m, u, _, _ in fake.calls]
    assert methods == [("GET", "/orgs/vrixel-emr/installation"), ("POST", "/app/installations/77/access_tokens")]
    app_jwt = fake.calls[0][2].split(" ", 1)[1]
    claims = jwt.decode(app_jwt, key.public_key(), algorithms=["RS256"])
    assert claims["iss"] == "12345"

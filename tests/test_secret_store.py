"""S3: secrets from a secret store (AWS Secrets Manager, mounted files or the environment), and a
GitHub App signer that can keep the key in KMS."""

from __future__ import annotations

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from valxel_sdlc import secret_ring, signing, strict
from valxel_sdlc.config import ProviderConfig, ServerConfig
from valxel_sdlc.governance.sources import GitHubAppTokenProvider


class NotFound(Exception):
    def __init__(self, code="ResourceNotFoundException"):
        self.response = {"Error": {"Code": code}}


class FakeSecretsManager:
    def __init__(self, data):
        self.data = data  # (secret id, stage) -> value
        self.calls = 0

    def get_secret_value(self, SecretId, VersionStage):
        self.calls += 1
        if (SecretId, VersionStage) not in self.data:
            raise NotFound("ResourceNotFoundException" if VersionStage == "AWSCURRENT" else "InvalidRequestException")
        return {"SecretString": self.data[(SecretId, VersionStage)]}


# ------------------------------------------------------------------ AWS Secrets Manager

def test_secrets_are_scoped_by_product_org_or_platform():
    assert secret_ring.secret_id("VALXEL_FLIPT_TOKEN_PAY") == "valxel/products/pay/VALXEL_FLIPT_TOKEN_PAY"
    assert secret_ring.secret_id("VALXEL_SERVICENOW_AUTH_CLN", "acme") == "acme/products/cln/VALXEL_SERVICENOW_AUTH_CLN"
    assert secret_ring.secret_id("VALXEL_GITHUB_TOKEN__ACME_FARM") == "valxel/orgs/acme_farm/VALXEL_GITHUB_TOKEN__ACME_FARM"
    assert secret_ring.secret_id("VALXEL_SIGNALS_TOKEN") == "valxel/platform/VALXEL_SIGNALS_TOKEN"


def test_aws_store_reads_current_and_previous_and_caches():
    sm = FakeSecretsManager({("valxel/platform/VALXEL_SIGNALS_TOKEN", "AWSCURRENT"): "new",
                             ("valxel/platform/VALXEL_SIGNALS_TOKEN", "AWSPREVIOUS"): "old",
                             ("valxel/products/pay/VALXEL_FLIPT_TOKEN_PAY", "AWSCURRENT"): "flipt"})
    now = [0.0]
    store = secret_ring.AwsSecretsManagerStore(client=sm, ttl=60, clock=lambda: now[0])
    assert store.versions("VALXEL_SIGNALS_TOKEN") == ["new", "old"]
    assert store.versions("VALXEL_FLIPT_TOKEN_PAY")[0] == "flipt"  # no previous version: just current
    assert store.versions("VALXEL_SMTP_PASSWORD") == ["", ""]  # not in the store
    calls = sm.calls
    store.versions("VALXEL_SIGNALS_TOKEN")
    assert sm.calls == calls  # cached
    sm.data[("valxel/platform/VALXEL_SIGNALS_TOKEN", "AWSCURRENT")] = "newer"
    now[0] = 61
    assert store.versions("VALXEL_SIGNALS_TOKEN")[0] == "newer"  # picked up after the cache expires


def test_aws_store_raises_on_other_errors():
    class Broken:
        def get_secret_value(self, **kw):
            raise NotFound("AccessDeniedException")
    with pytest.raises(NotFound):
        secret_ring.AwsSecretsManagerStore(client=Broken()).versions("VALXEL_SIGNALS_TOKEN")


def test_the_store_is_chosen_by_setting(monkeypatch, tmp_path):
    assert secret_ring.store_kind({}) == "env"
    assert secret_ring.store_kind({"VALXEL_SECRETS_DIR": str(tmp_path)}) == "files"
    assert secret_ring.store_kind({"VALXEL_SECRET_STORE": "AWS"}) == "aws"
    with pytest.raises(ValueError, match="one of env, files, aws"):
        secret_ring.store_kind({"VALXEL_SECRET_STORE": "vault"})
    sm = FakeSecretsManager({("acme/platform/VALXEL_SIGNALS_TOKEN", "AWSCURRENT"): "from-aws"})
    monkeypatch.setitem(secret_ring._aws, ("acme", ""), secret_ring.AwsSecretsManagerStore(prefix="acme", client=sm))
    env = {"VALXEL_SECRET_STORE": "aws", "VALXEL_SECRET_PREFIX": "acme", "VALXEL_SIGNALS_TOKEN": "from-env"}
    assert secret_ring.values("VALXEL_SIGNALS_TOKEN", env) == ["from-aws"]  # the environment is ignored


# ------------------------------------------------------------------ code that reads secrets

def test_secret_names_resolve_through_the_store(tmp_path):
    (tmp_path / "VALXEL_FLIPT_TOKEN_PAY").write_text("flipt-token")
    base = {"VALXEL_SECRETS_DIR": str(tmp_path), "VALXEL_FLIPT_TOKEN_PAY": "stale", "VALXEL_PUBLIC_URL": "https://v"}
    env = secret_ring.environ(base)
    assert env["VALXEL_FLIPT_TOKEN_PAY"] == "flipt-token"
    assert env.get("VALXEL_PUBLIC_URL") == "https://v"  # not a secret: from the environment
    assert env.get("VALXEL_SMTP_PASSWORD") is None
    assert secret_ring.is_secret("VALXEL_LUCID_MCP_AUTH") and secret_ring.is_secret("OPENROUTER_API_KEY")
    assert not secret_ring.is_secret("VALXEL_AUTH") and not secret_ring.is_secret("VALXEL_GITHUB_APP_PRIVATE_KEY_FILE")


def test_config_named_credentials_come_from_the_store(monkeypatch, tmp_path):
    monkeypatch.setenv("VALXEL_SECRETS_DIR", str(tmp_path))
    (tmp_path / "LUCID_TOKEN").write_text("Bearer lucid")
    (tmp_path / "VALXEL_OPENCHOREO_CLIENT_SECRET").write_text("oc-secret")
    (tmp_path / "OPENROUTER_KEY").write_text("or-key")
    monkeypatch.setenv("VALXEL_OPENCHOREO_TOKEN_URL", "https://idp/token")
    server = ServerConfig(name="lucid", url="https://x", auth_env="LUCID_TOKEN",
                          settings={"client_secret_env": "VALXEL_OPENCHOREO_CLIENT_SECRET",
                                    "token_url_env": "VALXEL_OPENCHOREO_TOKEN_URL"})
    assert server.auth_value() == "Bearer lucid"
    assert server.setting("client_secret") == "oc-secret" and server.setting("token_url") == "https://idp/token"
    provider = ProviderConfig(name="openrouter", base_url="https://or", api_key_env="OPENROUTER_KEY")
    assert provider.api_key() == "or-key"


def test_product_secrets_and_model_keys_come_from_the_store(monkeypatch, tmp_path):
    from valxel_sdlc.outcomes.sources import secret
    from valxel_sdlc.sandbox import session_env

    monkeypatch.setenv("VALXEL_SECRETS_DIR", str(tmp_path))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    (tmp_path / "VALXEL_PROMETHEUS_TOKEN_PAY").write_text("prom")
    (tmp_path / "ANTHROPIC_API_KEY").write_text("sk-ant-x")
    assert secret("prometheus", "pay") == "prom"
    env = session_env("claude_code", str(tmp_path / "home"))
    assert env["ANTHROPIC_API_KEY"] == "sk-ant-x"  # the session still gets its model key
    assert env.get("VALXEL_SECRETS_DIR", "") == ""  # but not where the tool's secrets are


# ------------------------------------------------------------------ strict mode

def test_strict_refuses_secrets_left_in_the_environment_once_a_store_is_set(tmp_path):
    env = {"VALXEL_SECRET_STORE": "files", "VALXEL_SECRETS_DIR": str(tmp_path), "VALXEL_SIGNALS_TOKEN": "t",
           "VALXEL_SESSION_SECRET_PREVIOUS": "p", "VALXEL_PUBLIC_URL": "https://v"}
    [problem] = strict.secrets_in_environment(env)
    assert "VALXEL_SESSION_SECRET_PREVIOUS, VALXEL_SIGNALS_TOKEN" in problem and "PUBLIC_URL" not in problem
    assert strict.secrets_in_environment({"VALXEL_SIGNALS_TOKEN": "t"}) == []  # env store: allowed (a known gap)
    assert "not 'vault'" in strict.secrets_in_environment({"VALXEL_SECRET_STORE": "vault"})[0]


# ------------------------------------------------------------------ the GitHub App signer

@pytest.fixture(scope="module")
def app_key():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
                            serialization.NoEncryption()).decode()
    return pem, key.public_key()


class FakeKms:
    """Signs like KMS would, with a key the caller never sees."""

    def __init__(self, pem):
        self._signer = signing.RsaKeySigner(pem)
        self.requests = []

    def sign(self, KeyId, Message, MessageType, SigningAlgorithm):
        self.requests.append((KeyId, MessageType, SigningAlgorithm))
        return {"Signature": self._signer.sign(Message)}


def test_a_kms_signed_github_app_jwt_verifies(app_key):
    pem, public = app_key
    kms = FakeKms(pem)
    tokens = GitHubAppTokenProvider("123", signing.KmsSigner("alias/valxel-github-app", client=kms), clock=lambda: 1_700_000_000)
    claims = jwt.decode(tokens._jwt(), public, algorithms=["RS256"], options={"verify_exp": False})
    assert claims["iss"] == "123" and claims["exp"] - claims["iat"] == 600
    assert kms.requests == [("alias/valxel-github-app", "RAW", "RSASSA_PKCS1_V1_5_SHA_256")]
    assert not hasattr(tokens.key, "_key")  # the provider holds a signer, not the key


def test_a_pem_still_works(app_key):
    pem, public = app_key
    tokens = GitHubAppTokenProvider("123", pem, clock=lambda: 1_700_000_000)
    assert jwt.decode(tokens._jwt(), public, algorithms=["RS256"], options={"verify_exp": False})["iss"] == "123"


def test_the_signer_is_chosen_by_setting(app_key, tmp_path, monkeypatch):
    pem, _ = app_key
    monkeypatch.setattr(signing, "KmsSigner", lambda key_id, region="": ("kms", key_id))
    assert signing.github_app_signer({"VALXEL_GITHUB_APP_KMS_KEY_ID": "arn:aws:kms:k"}) == ("kms", "arn:aws:kms:k")
    (tmp_path / "VALXEL_GITHUB_APP_PRIVATE_KEY").write_text(pem)
    assert isinstance(signing.github_app_signer({"VALXEL_SECRETS_DIR": str(tmp_path)}), signing.RsaKeySigner)
    key_file = tmp_path / "app.pem"
    key_file.write_text(pem)
    assert isinstance(signing.github_app_signer({"VALXEL_GITHUB_APP_PRIVATE_KEY_FILE": str(key_file)}), signing.RsaKeySigner)
    with pytest.raises(RuntimeError, match="VALXEL_GITHUB_APP_KMS_KEY_ID"):
        signing.github_app_signer({})
    assert signing.github_app_configured({"VALXEL_GITHUB_APP_ID": "1", "VALXEL_GITHUB_APP_KMS_KEY_ID": "k"})
    assert not signing.github_app_configured({"VALXEL_GITHUB_APP_ID": "1"})

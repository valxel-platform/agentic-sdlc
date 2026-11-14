"""S1 and S2: no published or weak secrets in production, and rotation without downtime."""

from __future__ import annotations

import hashlib
import hmac
import os
import time

import pytest

from valxel_sdlc import secret_ring, strict
from valxel_sdlc.web.auth import AuthConfig, SessionSigner

OLD = "a3f9c27b8d41e6f05a7c9e2b4d6f8a1c"  # 32 random-looking characters
NEW = "9e8d7c6b5a4f3e2d1c0b9a8f7e6d5c4b3a2f1e0d"
PUBLISHED = "local-dev-only-session-secret-change-me-0000"


# ------------------------------------------------------------------ S1: weak and published secrets

def test_github_sign_in_refuses_the_published_default(monkeypatch):
    monkeypatch.setenv("VALXEL_AUTH", "github")
    monkeypatch.setenv("VALXEL_GITHUB_OAUTH_CLIENT_ID", "id")
    monkeypatch.setenv("VALXEL_GITHUB_OAUTH_CLIENT_SECRET", "s")
    monkeypatch.setenv("VALXEL_SESSION_SECRET", PUBLISHED)
    with pytest.raises(RuntimeError, match="published in this repository"):
        AuthConfig.from_env()
    monkeypatch.setenv("VALXEL_SESSION_SECRET", "a" * 40)
    with pytest.raises(RuntimeError, match="too few distinct characters"):
        AuthConfig.from_env()
    monkeypatch.setenv("VALXEL_SESSION_SECRET", NEW)
    assert AuthConfig.from_env().secret == NEW


def test_a_cookie_signed_with_the_old_default_is_refused():
    forged = SessionSigner(PUBLISHED).dumps({"login": "mallory", "teams": ["acme/release-managers"],
                                             "exp": int(time.time()) + 3600})
    assert SessionSigner(NEW).loads(forged) is None


def test_weak_rules():
    assert secret_ring.weak(NEW) == ""
    assert "shorter than 32" in secret_ring.weak("short")
    assert "published" in secret_ring.weak("my-changeme-secret-0123456789abcdef")
    assert secret_ring.weak_password("valxel") and secret_ring.weak_password("Tr0ub4dor&3xyzabc") == ""


def test_strict_mode_refuses_weak_secrets_and_the_dev_database_password():
    env = {"VALXEL_SESSION_SECRET": PUBLISHED, "VALXEL_SESSION_SECRET_PREVIOUS": "x" * 32,
           "VALXEL_GITHUB_WEBHOOK_SECRET": "abc", "VALXEL_SIGNALS_TOKEN": NEW}
    out = strict.weak_secrets(env, database_url="postgresql://valxel:valxel@postgres:5432/valxel")
    assert any(p.startswith("VALXEL_SESSION_SECRET is an example value") for p in out)
    assert any("previous value is too few distinct" in p for p in out)
    assert any(p.startswith("VALXEL_GITHUB_WEBHOOK_SECRET is shorter than 20") for p in out)
    assert "DATABASE_URL's password is a default or example password" in out
    assert not any("SIGNALS" in p for p in out)
    good = {"VALXEL_SESSION_SECRET": NEW}
    assert strict.weak_secrets(good, database_url="postgresql://valxel:k8Tz2pQv9LmN4xRw@db/valxel") == []
    assert strict.weak_secrets(good, database_url="postgresql://db") == []  # no password: IAM auth


def test_compose_has_no_session_secret_default_and_keeps_postgres_local():
    with open(os.path.join(os.path.dirname(__file__), "..", "docker-compose.yml")) as f:
        text = f.read()
    assert PUBLISHED not in text
    assert "VALXEL_SESSION_SECRET: ${VALXEL_SESSION_SECRET:-}" in text
    assert '"127.0.0.1:5432:5432"' in text


# ------------------------------------------------------------------ S2: key rings

def test_values_come_from_the_secrets_folder_and_change_without_a_restart(tmp_path):
    env = {"VALXEL_SECRETS_DIR": str(tmp_path), "VALXEL_SIGNALS_TOKEN": "from-env"}
    (tmp_path / "VALXEL_SIGNALS_TOKEN").write_text(OLD + "\n")
    assert secret_ring.values("VALXEL_SIGNALS_TOKEN", env) == [OLD]
    time.sleep(0.01)
    (tmp_path / "VALXEL_SIGNALS_TOKEN").write_text(NEW)
    (tmp_path / "VALXEL_SIGNALS_TOKEN.previous").write_text(OLD)
    assert secret_ring.values("VALXEL_SIGNALS_TOKEN", env) == [NEW, OLD]
    assert secret_ring.values("VALXEL_SIGNALS_TOKEN", {"VALXEL_SIGNALS_TOKEN": "e", "VALXEL_SIGNALS_TOKEN_PREVIOUS": "p"}) == ["e", "p"]


def test_rotating_the_session_secret_signs_no_one_out(tmp_path, monkeypatch):
    monkeypatch.setenv("VALXEL_SECRETS_DIR", str(tmp_path))
    monkeypatch.setenv("VALXEL_AUTH", "dev")
    (tmp_path / "VALXEL_SESSION_SECRET").write_text(OLD)
    cfg = AuthConfig.from_env()
    signer = SessionSigner(cfg.ring())
    before = signer.dumps({"login": "sunil", "exp": int(time.time()) + 3600})
    time.sleep(0.01)
    (tmp_path / "VALXEL_SESSION_SECRET").write_text(NEW)
    (tmp_path / "VALXEL_SESSION_SECRET.previous").write_text(OLD)
    assert signer.loads(before)["login"] == "sunil"  # an old session survives the rotation
    after = signer.dumps({"login": "sunil", "exp": int(time.time()) + 3600})
    assert SessionSigner(NEW).loads(after) is not None  # new sessions use the new key
    (tmp_path / "VALXEL_SESSION_SECRET.previous").unlink()
    assert signer.loads(before) is None and signer.loads(after)["login"] == "sunil"


def test_webhooks_accept_either_value_during_a_rotation(monkeypatch, tmp_path):
    import shutil
    from pathlib import Path

    from fastapi.testclient import TestClient
    from langgraph.checkpoint.memory import InMemorySaver

    from valxel_sdlc.api.app import create_app
    from valxel_sdlc.config import load_settings
    from valxel_sdlc.engine import FeatureEngine
    from valxel_sdlc.services import build_services

    root = tmp_path / "governance"
    shutil.copytree(Path(__file__).resolve().parents[1] / "examples" / "governance", root)
    engine = FeatureEngine(build_services(load_settings(mode="stub", database_url="", artifact_dir=tmp_path / "a",
                                                        governance_source="local", governance_root=root)),
                           InMemorySaver())

    monkeypatch.setenv("VALXEL_GITHUB_WEBHOOK_SECRET", NEW)
    monkeypatch.setenv("VALXEL_GITHUB_WEBHOOK_SECRET_PREVIOUS", OLD)
    monkeypatch.setenv("VALXEL_SIGNALS_TOKEN", NEW)
    monkeypatch.setenv("VALXEL_SIGNALS_TOKEN_PREVIOUS", OLD)
    app = create_app(engine, auth=AuthConfig(mode="dev", secret=NEW), web_dist=None)
    c = TestClient(app)
    body = b'{"zen": "ok"}'

    def gh(secret):
        sig = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        return c.post("/api/webhooks/github", content=body, headers={"x-hub-signature-256": sig,
                                                                      "x-github-event": "ping"}).status_code

    assert gh(NEW) != 401 and gh(OLD) != 401 and gh("x" * 32) == 401
    monkeypatch.delenv("VALXEL_GITHUB_WEBHOOK_SECRET_PREVIOUS")
    assert gh(OLD) == 401  # the old secret stops working once it's removed

    def alert(token):
        return c.post("/api/signals/alertmanager", json={"alerts": []},
                      headers={"authorization": f"Bearer {token}"}).status_code
    assert alert(NEW) != 401 and alert(OLD) != 401 and alert("nope") == 401


"""Secrets are read through the store (valxel_sdlc.secret_ring), never straight from the environment."""

import re
from pathlib import Path

from valxel_sdlc import secret_ring
from valxel_sdlc.secret_ring import is_secret

SRC = Path(__file__).resolve().parent.parent / "src" / "valxel_sdlc"
DIRECT = re.compile(r"""(?:os\.environ(?:\.get)?\s*[\[\(]|os\.getenv\()\s*["']([A-Z0-9_]+)["']""")


def test_no_code_reads_a_secret_from_the_environment_directly():
    found = [f"{p.relative_to(SRC)}:{i}: {m.group(1)}"
             for p in sorted(SRC.rglob("*.py")) for i, line in enumerate(p.read_text().splitlines(), 1)
             for m in DIRECT.finditer(line) if is_secret(m.group(1))]
    assert not found, "read these through valxel_sdlc.secret_ring (get, current or environ()):\n" + "\n".join(found)


def test_settings_kept_with_the_secrets_are_adopted_and_secrets_are_not(tmp_path):
    (tmp_path / "VALXEL_GITHUB_APP_ID").write_text("12345\n")
    (tmp_path / "ANTHROPIC_API_KEY").write_text("sk-not-for-the-environment")
    (tmp_path / "VALXEL_PUBLIC_URL").write_text("https://ignored")
    (tmp_path / "lower-case").write_text("x")
    env = {"VALXEL_SECRET_STORE": "files", "VALXEL_SECRETS_DIR": str(tmp_path), "VALXEL_PUBLIC_URL": "https://set"}
    assert secret_ring.adopt_settings(env) == ["VALXEL_GITHUB_APP_ID"]
    assert env["VALXEL_GITHUB_APP_ID"] == "12345" and env["VALXEL_PUBLIC_URL"] == "https://set"
    assert "ANTHROPIC_API_KEY" not in env
    assert secret_ring.current("ANTHROPIC_API_KEY", env) == "sk-not-for-the-environment"
    assert secret_ring.adopt_settings({"VALXEL_SECRET_STORE": "env"}) == []


def test_the_cli_reads_the_database_through_the_store(tmp_path, monkeypatch):
    from valxel_sdlc import cli

    (tmp_path / "DATABASE_URL").write_text("postgresql://u:p@db/x")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("VALXEL_SECRET_STORE", "files")
    monkeypatch.setenv("VALXEL_SECRETS_DIR", str(tmp_path))
    assert cli._database_url() == "postgresql://u:p@db/x"

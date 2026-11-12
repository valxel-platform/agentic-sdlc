"""Agent sessions never run with the tool's credentials: an allowlisted environment, no hooks,
the tool's files unreadable, shell commands sandboxed to the package registries, and a coder's
checkout cloned with a token for its one repository."""

from __future__ import annotations

import io
import json
import os
from pathlib import Path

import pytest

from vrixel_sdlc import sandbox
from vrixel_sdlc.governance.sources import GitHubAppTokenProvider


def test_the_session_sees_only_the_allowlisted_environment():
    base = {"PATH": "/usr/bin", "LANG": "C.UTF-8", "ANTHROPIC_API_KEY": "sk-model", "DATABASE_URL": "postgres://x",
            "VRIXEL_GITHUB_APP_PRIVATE_KEY_FILE": "/run/secrets/app.pem", "VRIXEL_SESSION_SECRET": "s3cret",
            "OPENAI_API_KEY": "sk-openai", "VRIXEL_AGENT_ENV_PASS": "AWS_REGION", "AWS_REGION": "us-east-2"}
    env = sandbox.session_env("claude_code", "/tmp/h", {"CLAUDE_CODE_PROMPT_CACHE_TTL": "1h"}, base=base)
    # Every name the tool has is present, so the SDK's merge can't bring it back; secrets are blank.
    assert set(base) <= set(env)
    assert env["ANTHROPIC_API_KEY"] == "sk-model" and env["PATH"] == "/usr/bin" and env["AWS_REGION"] == "us-east-2"
    for secret in ("DATABASE_URL", "VRIXEL_GITHUB_APP_PRIVATE_KEY_FILE", "VRIXEL_SESSION_SECRET", "OPENAI_API_KEY"):
        assert env[secret] == ""
    assert env["HOME"] == "/tmp/h" and env["npm_config_ignore_scripts"] == "true"
    # Codex gets only the names it may have, and its own key.
    clean = sandbox.clean_env("codex", "/tmp/h", base=base)
    assert "DATABASE_URL" not in clean and clean["OPENAI_API_KEY"] == "sk-openai" and "ANTHROPIC_API_KEY" not in clean


def test_no_hooks_the_tools_files_unreadable_and_the_shell_sandboxed(monkeypatch, tmp_path):
    monkeypatch.setenv("VRIXEL_GITHUB_APP_PRIVATE_KEY_FILE", "/etc/vrixel-keys/app.pem")
    s = sandbox.claude_settings("/tmp/w")
    assert s["disableAllHooks"] is True
    deny = s["permissions"]["deny"]
    assert {"Read(//app/**)", "Read(//data/**)", "Read(//etc/vrixel-keys/**)", "Read(//proc/self/environ)"} <= set(deny)
    egress = tmp_path / "egress.yaml"
    egress.write_text("allowed: [registry.npmjs.org, github.com]\n")
    monkeypatch.setenv("VRIXEL_EGRESS_CONFIG", str(egress))
    box = sandbox.claude_sandbox()
    assert box["enabled"] and box["allowUnsandboxedCommands"] is False
    assert box["network"]["allowedDomains"] == ["registry.npmjs.org", "github.com"]
    # The shipped allowlist is registries and GitHub only.
    monkeypatch.setenv("VRIXEL_EGRESS_CONFIG", str(Path(__file__).resolve().parents[1] / "config/egress.yaml"))
    assert all(d.endswith(("npmjs.org", "pythonhosted.org", "pypi.org", "maven.org", "apache.org", "gradle.org",
                           "github.com", "githubusercontent.com")) for d in sandbox.egress())


def test_claude_code_sessions_carry_it():
    from vrixel_sdlc.runtime.base import AgentTask
    from vrixel_sdlc.runtime.claude_code import session_options

    class Role:
        max_turns, max_call_usd = 10, 1.0

    class Layers:
        def layers(self):
            return {k: "" for k in ("L2", "L3", "L4", "L5")}

    task = AgentTask(role="coder", phase="build", tags=None, layers=Layers(), stub_text="", sandbox={"cwd": "/tmp/w", "home": "/tmp/h"},
                     cwd="/tmp/w")
    opts = session_options(task, Role(), "anthropic/claude-x", cwd=None, plugin_dirs=[], home="/tmp/h")
    assert json.loads(opts["settings"])["disableAllHooks"] is True
    assert opts["sandbox"]["enabled"] and opts["env"]["HOME"] == "/tmp/h" and opts["cwd"] == "/tmp/w"
    for name in os.environ:
        if name.startswith("VRIXEL_"):
            assert opts["env"][name] == ""
    ro = AgentTask(role="architect", phase="architecture", tags=None, layers=Layers(), stub_text="", cwd="/tmp/c", read_only=True)
    assert "sandbox" not in session_options(ro, Role(), "x", cwd=None, plugin_dirs=[], home="/tmp/h")


class _Opener:
    def __init__(self):
        self.calls = []

    def open(self, req, timeout=None):
        body = json.loads(req.data.decode()) if req.data else None
        self.calls.append((req.get_method(), req.full_url, body))
        if req.full_url.endswith("/installation"):
            out = {"id": 7, "permissions": {"contents": "write", "statuses": "write"}}
        else:
            out = {"token": "ghs_scoped", "expires_at": "2026-11-12T23:00:00Z",
                   "permissions": {"contents": "write", "metadata": "read"}}
        return io.BytesIO(json.dumps(out).encode())


def test_a_coder_token_names_one_repository_and_contents_only(monkeypatch):
    opener = _Opener()
    p = GitHubAppTokenProvider("1", "key", opener=opener)
    monkeypatch.setattr(p, "_jwt", lambda: "jwt")
    assert p.scoped("acme-clinic", ["cln-api"], {"contents": "write"}) == "ghs_scoped"
    method, url, body = opener.calls[-1]
    assert url.endswith("/app/installations/7/access_tokens")
    assert body == {"repositories": ["cln-api"], "permissions": {"contents": "write"}}
    # Anything granted beyond the ask is refused.
    class Greedy(_Opener):
        def open(self, req, timeout=None):
            if req.full_url.endswith("/access_tokens"):
                return io.BytesIO(json.dumps({"token": "t", "expires_at": "2026-11-12T23:00:00Z",
                                              "permissions": {"contents": "write", "statuses": "write"}}).encode())
            return super().open(req, timeout)

    g = GitHubAppTokenProvider("1", "key", opener=Greedy())
    monkeypatch.setattr(g, "_jwt", lambda: "jwt")
    with pytest.raises(Exception, match="more than asked for: statuses"):
        g.scoped("acme-clinic", ["cln-api"], {"contents": "write"})


def test_a_session_folder_is_outside_the_tools_files_and_goes_away(monkeypatch, tmp_path):
    monkeypatch.setenv("VRIXEL_SANDBOX_DIR", str(tmp_path / "boxes"))

    class S:
        class settings:
            mode = "stub"

    with sandbox.session(S, repo="acme/cln-api", branch="feature/X") as box:
        assert Path(box.cwd).is_dir() and Path(box.home).is_dir() and str(tmp_path / "boxes") in box.root
        assert box.spec == {"cwd": box.cwd, "home": box.home}
        root = box.root
    assert not Path(root).exists()
    # Live mode needs a client that can mint a one-repository token: no silent fallback to the tool's.
    class Live:
        class settings:
            mode = "live"

        class github:
            tokens = object()

    with pytest.raises(RuntimeError, match="token for one repository"):
        with sandbox.session(Live, repo="acme/cln-api", branch="feature/X"):
            pass


def test_git_asks_for_the_token_from_the_sessions_own_file(tmp_path):
    import subprocess

    script = sandbox._askpass(tmp_path, "ghs_scoped")
    out = subprocess.run([str(script), "Password for 'https://x-access-token@github.com':"], capture_output=True, text=True)
    assert out.stdout.strip() == "ghs_scoped"
    assert subprocess.run([str(script), "Username for 'https://github.com':"], capture_output=True, text=True).stdout.strip() == "x-access-token"
    assert oct((tmp_path / ".token").stat().st_mode)[-3:] == "600"

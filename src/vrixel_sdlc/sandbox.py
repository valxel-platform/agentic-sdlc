"""Where agent sessions run, and with what: never with the tool's own credentials.

The tool's process holds the GitHub App's private key, model keys, the database URL and webhook
secrets. A coder session runs shell commands and installs packages, and a package's install
script runs with whatever the session can reach. So every session gets:

    environment   only an allowlist (PATH, locale, its model credentials, telemetry); every other
                  variable the tool has is blanked, so `env` in the session shows none of them
    files         Read is denied on the tool's own files and secrets (the App key, /app, /data);
                  the session works in its own folder outside them
    hooks         none: `disableAllHooks`, so a repository's .claude/settings.json runs nothing
    shell         Claude Code's sandbox for every Bash command (no way around it), with network
                  only to the package registries and GitHub (config/egress.yaml); Codex runs in its
                  workspace-write sandbox
    installs      npm, yarn and pnpm skip packages' install scripts

A coder works in a checkout of its one repository, cloned for the session with a GitHub token
the App mints for that repository only, with contents:write and nothing else: it can push its
branch, and can't post the vrixel/gates status, change settings or touch another repository.
The token lives an hour at most and the folder is deleted when the session ends.

    with session(services, repo="acme/cln-api", branch="feature/CLN-FEAT-7") as box:
        runtime.run(AgentTask(..., cwd=box.cwd, sandbox=box.spec))
"""

from __future__ import annotations

import contextlib
import logging
import os
import shutil
import stat
import subprocess
import tempfile
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import yaml

log = logging.getLogger(__name__)

# What a session may see from the tool's environment. Model credentials per runtime are added
# below; VRIXEL_AGENT_ENV_PASS adds names (comma-separated) a deployment needs, e.g. AWS_REGION
# for Bedrock.
SAFE = ("PATH", "LANG", "LC_ALL", "LC_CTYPE", "TZ", "TERM", "SSL_CERT_FILE", "SSL_CERT_DIR",
        "REQUESTS_CA_BUNDLE", "NODE_EXTRA_CA_CERTS")
MODEL_KEYS = {
    "claude_code": ("ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN",
                    "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "ANTHROPIC_MODEL"),
    "codex": ("OPENAI_API_KEY", "CODEX_API_KEY", "OPENAI_BASE_URL"),
}
# Package managers: no install scripts from dependencies.
NO_SCRIPTS = {"npm_config_ignore_scripts": "true", "YARN_ENABLE_SCRIPTS": "false", "PNPM_IGNORE_SCRIPTS": "true",
              "GIT_TERMINAL_PROMPT": "0"}
# Never readable from a session (absolute paths; Claude Code's '//' form is added when rendered).
DENY_READ = ("/app", "/data", "/run/secrets", "/var/run/secrets", "/etc/vrixel", "/proc/self/environ")
DEFAULT_EGRESS = ("registry.npmjs.org", "pypi.org", "files.pythonhosted.org", "repo.maven.apache.org",
                  "repo1.maven.org", "plugins.gradle.org", "services.gradle.org", "github.com",
                  "codeload.github.com", "objects.githubusercontent.com")


def egress(path: str | None = None) -> list[str]:
    """The domains a session's shell may reach (config/egress.yaml `allowed`)."""
    p = Path(path or os.environ.get("VRIXEL_EGRESS_CONFIG") or Path(os.environ.get("VRIXEL_ROOT", ".")) / "config/egress.yaml")
    try:
        doc = yaml.safe_load(p.read_text()) or {}
        allowed = [str(d) for d in doc.get("allowed") or []]
        return allowed or list(DEFAULT_EGRESS)
    except OSError:
        return list(DEFAULT_EGRESS)


def secret_paths() -> list[str]:
    """The tool's secret files, wherever the deployment put them."""
    out = list(DENY_READ)
    for key in ("VRIXEL_GITHUB_APP_PRIVATE_KEY_FILE", "GOOGLE_APPLICATION_CREDENTIALS", "VRIXEL_SECRETS_DIR"):
        v = os.environ.get(key, "")
        if v:
            out.append(str(Path(v).parent) if key.endswith("_FILE") or key.endswith("CREDENTIALS") else v)
    return sorted(set(out))


def session_env(runtime: str, home: str, extra: dict | None = None, base: dict | None = None) -> dict[str, str]:
    """The environment a session runs with, as the full set of names the tool's process has:
    allowed names keep their values, every other name is blanked (the SDK merges the parent's
    environment, so blanking is how a name is taken away)."""
    base = dict(os.environ if base is None else base)
    passed = set(SAFE) | set(MODEL_KEYS.get(runtime, ())) | {
        n.strip() for n in (base.get("VRIXEL_AGENT_ENV_PASS") or "").split(",") if n.strip()}
    env = {k: (v if k in passed else "") for k, v in base.items()}
    env.update({"HOME": home, "TMPDIR": str(Path(home) / "tmp"), **NO_SCRIPTS, **(extra or {})})
    return env


def clean_env(runtime: str, home: str, extra: dict | None = None, base: dict | None = None) -> dict[str, str]:
    """For runtimes that start their own process (Codex): only the allowed names."""
    return {k: v for k, v in session_env(runtime, home, extra, base).items() if v != ""}


def claude_settings(workspace: str) -> dict:
    """Claude Code settings for every session: no hooks, the tool's files unreadable."""
    deny = [f"Read(/{p}/**)" if not p.endswith("environ") else f"Read(/{p})" for p in secret_paths()]
    return {"disableAllHooks": True, "permissions": {"deny": deny}}


def claude_sandbox() -> dict:
    """Claude Code's sandbox for Bash: every command inside it, network only to the allowlist."""
    return {"enabled": True, "autoAllowBashIfSandboxed": True, "allowUnsandboxedCommands": False,
            "network": {"allowedDomains": egress()},
            # Unprivileged containers can't create the user namespaces the full sandbox needs.
            "enableWeakerNestedSandbox": os.environ.get("VRIXEL_AGENT_SANDBOX_WEAKER", "") == "1"}


@dataclass
class Box:
    cwd: str
    home: str
    root: str
    repo: str = ""
    branch: str = ""
    token_scope: dict = field(default_factory=dict)

    @property
    def spec(self) -> dict:
        return {"cwd": self.cwd, "home": self.home}


def _root() -> Path:
    # Outside /data and /app, which sessions may not read.
    return Path(os.environ.get("VRIXEL_SANDBOX_DIR") or Path(tempfile.gettempdir()) / "vrixel-sandboxes")


def _askpass(folder: Path, token: str) -> Path:
    """git asks this script for the password: the token, from a file only the session's folder holds."""
    (folder / ".token").write_text(token)
    (folder / ".token").chmod(0o600)
    script = folder / "askpass.sh"
    script.write_text('#!/bin/sh\ncase "$1" in Username*) echo x-access-token ;; *) cat "$(dirname "$0")/.token" ;; esac\n')
    script.chmod(stat.S_IRWXU)
    return script


@contextlib.contextmanager
def session(services, *, repo: str = "", branch: str = "", clone: bool | None = None):
    """A folder for one agent session, and for a coder a checkout of its repo on its branch."""
    root = _root() / uuid.uuid4().hex[:12]
    home = root / "home"
    (home / "tmp").mkdir(parents=True, exist_ok=True)
    cwd = root / "work"
    cwd.mkdir()
    box = Box(cwd=str(cwd), home=str(home), root=str(root), repo=repo, branch=branch)
    try:
        do_clone = clone if clone is not None else bool(repo) and getattr(services.settings, "mode", "") == "live"
        if do_clone:
            box.cwd, box.token_scope = _checkout(services, repo, branch, root)
        yield box
    finally:
        def fix(func, path, _exc):
            os.chmod(path, stat.S_IRWXU)
            func(path)

        shutil.rmtree(root, onerror=fix)


def _checkout(services, repo: str, branch: str, root: Path) -> tuple[str, dict]:
    tokens = getattr(services.github, "tokens", None)
    scoped = getattr(tokens, "scoped", None)
    if not callable(scoped):
        raise RuntimeError("This GitHub client can't mint a token for one repository; coder sessions need the "
                           "GitHub App (VRIXEL_GITHUB_APP_ID and its key)")
    org, name = repo.split("/", 1)
    token = scoped(org, [name], {"contents": "write"})
    dest = root / "work" / name
    askpass = _askpass(root, token)
    env = {"PATH": os.environ.get("PATH", ""), "GIT_ASKPASS": str(askpass), "GIT_TERMINAL_PROMPT": "0",
           "HOME": str(root / "home")}
    url = f"https://github.com/{repo}.git"
    subprocess.run(["git", "clone", "--quiet", "--depth", "50", url, str(dest)], check=True, env=env,
                   capture_output=True, timeout=300)
    have = subprocess.run(["git", "-C", str(dest), "ls-remote", "--heads", "origin", branch], env=env,
                          capture_output=True, text=True, timeout=60).stdout.strip()
    if have:
        subprocess.run(["git", "-C", str(dest), "fetch", "--quiet", "origin", branch], check=True, env=env,
                       capture_output=True, timeout=120)
        subprocess.run(["git", "-C", str(dest), "checkout", "--quiet", "-B", branch, "FETCH_HEAD"], check=True,
                       env=env, capture_output=True, timeout=60)
    else:
        subprocess.run(["git", "-C", str(dest), "checkout", "--quiet", "-b", branch], check=True, env=env,
                       capture_output=True, timeout=60)
    for k, v in (("credential.helper", ""), ("core.askPass", str(askpass)),
                 ("user.name", "vrixel-coder"), ("user.email", "vrixel-coder@users.noreply.github.com")):
        subprocess.run(["git", "-C", str(dest), "config", k, v], check=True, capture_output=True, timeout=30)
    return str(dest), {"repo": repo, "permissions": {"contents": "write"}}

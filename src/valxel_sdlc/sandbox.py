"""Where agent sessions run, and with what: never with the tool's own credentials.

The tool's process holds the GitHub App's private key, model keys, the database URL and webhook
secrets. A coder session runs shell commands and installs packages, and a package's install
script runs with whatever the session can reach. So every session gets:

    environment   only an allowlist (PATH, locale, its model credentials, telemetry); every other
                  variable the tool has is blanked, so `env` in the session shows none of them
    files         Read is denied on the tool's own files and secrets (the App key, /app, /data) and
                  on all of /proc (other processes' environments, memory maps, command lines); the
                  session works in its own folder outside them
    the tool      makes itself non-dumpable before the first session starts (protect_process): the
                  kernel then refuses /proc/<pid>/environ and /proc/<pid>/mem of the tool's process
                  to every other process of the same user, by whatever route (Read, a shell, Codex)
    hooks         none: `disableAllHooks`, so a repository's .claude/settings.json runs nothing
    shell         Claude Code's sandbox for every Bash command (no way around it), with network
                  only to the package registries and GitHub (config/egress.yaml); Codex runs in its
                  workspace-write sandbox
    installs      npm, yarn and pnpm skip packages' install scripts

A coder works in a checkout of its one repository, cloned for the session with a GitHub token
the App mints for that repository only, with contents:write and nothing else: it can push its
branch, and can't post the valxel/gates status, change settings or touch another repository.
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
# below; VALXEL_AGENT_ENV_PASS adds names (comma-separated) a deployment needs, e.g. AWS_REGION
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
DENY_READ = ("/app", "/data", "/run/secrets", "/var/run/secrets", "/etc/valxel", "/proc")
def egress(path: str | None = None) -> list[str]:
    """The domains a session may reach (config/egress.yaml `allowed`): the one list behind the shell
    sandbox here, the VM tier's egress proxy (egress_proxy.py) and the cluster's network policy
    (platform-foundation generates it from a copy of this file, which `valxel repos check`
    compares). No built-in fallback: a second list is how they drifted (engineering review,
    finding 4). Without the file a session may reach nothing, and the error says why."""
    p = Path(path or os.environ.get("VALXEL_EGRESS_CONFIG") or Path(os.environ.get("VALXEL_ROOT", ".")) / "config/egress.yaml")
    try:
        doc = yaml.safe_load(p.read_text()) or {}
    except OSError:
        log.error("no egress list at %s (VALXEL_EGRESS_CONFIG or VALXEL_ROOT): sessions may reach nothing", p)
        return []
    return [str(d).strip().lower() for d in doc.get("allowed") or [] if str(d).strip()]


def secret_paths() -> list[str]:
    """The tool's secret files, wherever the deployment put them."""
    out = list(DENY_READ)
    for key in ("VALXEL_GITHUB_APP_PRIVATE_KEY_FILE", "GOOGLE_APPLICATION_CREDENTIALS", "VALXEL_SECRETS_DIR"):
        v = os.environ.get(key, "")
        if v:
            out.append(str(Path(v).parent) if key.endswith("_FILE") or key.endswith("CREDENTIALS") else v)
    return sorted(set(out))


def session_env(runtime: str, home: str, extra: dict | None = None, base: dict | None = None) -> dict[str, str]:
    """The environment a session runs with, as the full set of names the tool's process has:
    allowed names keep their values, every other name is blanked (the SDK merges the parent's
    environment, so blanking is how a name is taken away)."""
    from_process = base is None
    base = dict(os.environ if base is None else base)
    passed = set(SAFE) | set(MODEL_KEYS.get(runtime, ())) | {
        n.strip() for n in (base.get("VALXEL_AGENT_ENV_PASS") or "").split(",") if n.strip()}
    env = {k: (v if k in passed else "") for k, v in base.items()}
    if from_process:  # model credentials kept in the secret store rather than the environment
        from .secret_ring import current, is_secret

        for k in MODEL_KEYS.get(runtime, ()):
            if is_secret(k) and not env.get(k):
                v = current(k)
                if v:
                    env[k] = v
    env.update({"HOME": home, "TMPDIR": str(Path(home) / "tmp"), **NO_SCRIPTS, **(extra or {})})
    return env


def clean_env(runtime: str, home: str, extra: dict | None = None, base: dict | None = None) -> dict[str, str]:
    """For runtimes that start their own process (Codex): only the allowed names."""
    return {k: v for k, v in session_env(runtime, home, extra, base).items() if v != ""}


def claude_settings(workspace: str) -> dict:
    """Claude Code settings for every session: no hooks, the tool's files unreadable."""
    deny = [f"Read(/{p}/**)" for p in secret_paths()]
    return {"disableAllHooks": True, "permissions": {"deny": deny}}


PR_SET_DUMPABLE, PR_GET_DUMPABLE = 4, 3
_protected = False


def protect_process() -> bool:
    """Make this process non-dumpable (Linux prctl): /proc/<pid>/environ, mem and maps of the tool
    become root-owned, so a session running as the same user can't read the tool's environment
    (DATABASE_URL, webhook secrets, a key left in an environment variable) or memory. Sessions are
    started with exec, which makes them dumpable again, so their own behaviour doesn't change.
    False where the kernel has no such switch (not Linux)."""
    global _protected
    if _protected:
        return True
    try:
        import ctypes

        libc = ctypes.CDLL(None, use_errno=True)
        if libc.prctl(PR_SET_DUMPABLE, 0, 0, 0, 0) != 0:
            return False
        _protected = libc.prctl(PR_GET_DUMPABLE, 0, 0, 0, 0) == 0
    except (OSError, AttributeError):
        return False
    return _protected


def _proxy_env() -> dict[str, str]:
    """The sandbox's proxy settings, which git needs to reach anything from a sandbox with no route out."""
    return {k: v for k in ("HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY", "https_proxy", "http_proxy", "no_proxy")
            if (v := os.environ.get(k))}


def allowed_domains() -> list[str]:
    """The shell sandbox's network allowlist: config/egress.yaml, and the git broker when it's on
    (the session's own git push goes there)."""
    from .git_broker import host, url

    b = url()
    return egress() + ([host(b)] if b and host(b) not in egress() else [])


def claude_sandbox() -> dict:
    """Claude Code's sandbox for Bash: every command inside it, network only to the allowlist."""
    return {"enabled": True, "autoAllowBashIfSandboxed": True, "allowUnsandboxedCommands": False,
            "network": {"allowedDomains": allowed_domains()},
            # Unprivileged containers can't create the user namespaces the full sandbox needs.
            "enableWeakerNestedSandbox": os.environ.get("VALXEL_AGENT_SANDBOX_WEAKER", "") == "1"}


@dataclass
class Box:
    cwd: str
    home: str
    root: str
    repo: str = ""
    branch: str = ""
    token_scope: dict = field(default_factory=dict)
    # Sessions in their own sandbox (valxel_sdlc.session_runner): the checkout the runner makes
    # there, with a token for this one repository. Nothing is cloned here.
    checkout: dict = field(default_factory=dict)

    @property
    def spec(self) -> dict:
        return {"cwd": self.cwd, "home": self.home, **({"checkout": self.checkout} if self.checkout else {})}


def _root() -> Path:
    # Outside /data and /app, which sessions may not read.
    return Path(os.environ.get("VALXEL_SANDBOX_DIR") or Path(tempfile.gettempdir()) / "valxel-sandboxes")


def sweep(*, older_than_s: float = 6 * 3600, now: float | None = None) -> int:
    """Delete session folders and read-only checkouts a crashed worker left behind (T-121): a
    session ends its own (`session`, `workspace.release`), so anything still here hours later is
    customer code, and maybe a repo token, that nothing will use again. Sessions are capped at an
    hour (VALXEL_SESSION_TIMEOUT_S); six hours leaves room for any that run long."""
    import time

    now = time.time() if now is None else now
    root, gone = _root(), 0
    if not root.is_dir():
        return 0
    folders = [p for p in root.iterdir() if p.is_dir() and p.name != "checkouts"]
    if (root / "checkouts").is_dir():
        folders += [p for p in (root / "checkouts").iterdir() if p.is_dir()]
    for folder in folders:
        try:
            if now - folder.stat().st_mtime < older_than_s:
                continue
        except FileNotFoundError:
            continue

        def fix(func, path, _exc):
            os.chmod(path, stat.S_IWRITE | stat.S_IREAD | stat.S_IEXEC)
            func(path)

        shutil.rmtree(folder, onerror=fix)
        gone += 1
    return gone


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
    if not protect_process():
        log.warning("couldn't make the tool non-dumpable: a session could read its environment through /proc")
    root = _root() / uuid.uuid4().hex[:12]
    home = root / "home"
    (home / "tmp").mkdir(parents=True, exist_ok=True)
    cwd = root / "work"
    cwd.mkdir()
    box = Box(cwd=str(cwd), home=str(home), root=str(root), repo=repo, branch=branch)
    try:
        do_clone = clone if clone is not None else bool(repo) and getattr(services.settings, "mode", "") == "live"
        if do_clone and remote():  # the session's own sandbox clones it: only the token goes there
            box.checkout = {"repo": repo, "branch": branch, "token": repo_token(services, repo)}
            box.token_scope = {"repo": repo, "permissions": {"contents": "write"}}
        elif do_clone:
            box.cwd, box.token_scope = _checkout(services, repo, branch, root)
        yield box
    finally:
        def fix(func, path, _exc):
            os.chmod(path, stat.S_IRWXU)
            func(path)

        shutil.rmtree(root, onerror=fix)


def remote() -> bool:
    """Whether agent sessions run in sandboxes of their own: kubernetes (the cluster's) or docker (a
    container per session on one machine)."""
    return os.environ.get("VALXEL_SESSION_EXECUTOR", "local").lower() in ("kubernetes", "docker")


def repo_token(services, repo: str) -> str:
    """A token for this one repository, contents:write and nothing else, living an hour at most."""
    tokens = getattr(services.github, "tokens", None)
    scoped = getattr(tokens, "scoped", None)
    if not callable(scoped):
        raise RuntimeError("This GitHub client can't mint a token for one repository; coder sessions need the "
                           "GitHub App (VALXEL_GITHUB_APP_ID and its key)")
    org, name = repo.split("/", 1)
    return scoped(org, [name], {"contents": "write"})


def _checkout(services, repo: str, branch: str, root: Path) -> tuple[str, dict]:
    return clone(repo, branch, repo_token(services, repo), root), {"repo": repo, "permissions": {"contents": "write"}}


def clone(repo: str, branch: str, token: str, root: Path, via: str = "") -> str:
    """A checkout of `repo` on `branch` (new if it doesn't exist yet) under root/work, pushing with
    `token` through an askpass script only root holds. Here, or in a session's sandbox. With `via`
    (the git broker, valxel_sdlc.git_broker) the remote is the broker and `token` is the session's
    pass, not a GitHub token."""
    from .git_broker import remote_url

    name = repo.split("/", 1)[1]
    dest = root / "work" / name
    askpass = _askpass(root, token)
    env = {"PATH": os.environ.get("PATH", ""), "GIT_ASKPASS": str(askpass), "GIT_TERMINAL_PROMPT": "0",
           "HOME": str(root / "home"), **_proxy_env()}
    url = remote_url(repo, via)
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
                 ("user.name", "valxel-coder"), ("user.email", "valxel-coder@users.noreply.github.com")):
        subprocess.run(["git", "-C", str(dest), "config", k, v], check=True, capture_output=True, timeout=30)
    return str(dest)

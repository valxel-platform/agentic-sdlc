"""Read-only checkouts of a product's repos, for agents that must read the code before deciding
(the architect, and the design-record draft for an existing product).

    checkout(services, repos, key="CLN-FEAT-7-architecture")
      -> {"root": "/…/checkouts/CLN-FEAT-7-architecture", "repos": {"acme/api": {"sha": "…"},
                                                                     "acme/big": {"skipped": "too large"}}}

Shallow clones at each repo's default branch, through the GitHub App's installation token (never
written into the clone: the remote is reset after cloning), files made read-only, the folder deleted
by `release` once the session is done. Only in live mode with GitHub's REST client; elsewhere it
returns None and the agent works from the governance record alone, which the gate says.
"""

from __future__ import annotations

import logging
import os
import shutil
import stat
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)
MAX_MB = int(os.environ.get("VRIXEL_CHECKOUT_MAX_MB", "200"))


def available(services) -> bool:
    return services.settings.mode == "live" and hasattr(services.github, "tokens") and shutil.which("git") is not None


def _readonly(path: Path) -> None:
    for root, dirs, files in os.walk(path):
        for name in files:
            p = Path(root, name)
            try:
                p.chmod(p.stat().st_mode & ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))
            except OSError:
                pass


def checkout(services, repos: list[str], *, key: str) -> dict | None:
    if not available(services) or not repos:
        return None
    from .sandbox import _root

    # Outside /data and /app, which agent sessions may not read (vrixel_sdlc.sandbox).
    root = _root() / "checkouts" / key
    release({"root": str(root)})
    root.mkdir(parents=True, exist_ok=True)
    gh = services.github
    out: dict = {"root": str(root), "repos": {}}
    for repo in dict.fromkeys(repos):
        try:
            meta = gh._call("GET", repo, "")
            if int(meta.get("size") or 0) > MAX_MB * 1024:  # GitHub reports kilobytes
                out["repos"][repo] = {"skipped": f"larger than {MAX_MB} MB"}
                continue
            dest = root / repo.split("/", 1)[1]
            token = gh.tokens.token(repo.split("/", 1)[0])
            url = f"https://x-access-token:{token}@github.com/{repo}.git"
            subprocess.run(["git", "clone", "--quiet", "--depth", "1", url, str(dest)], check=True,
                           capture_output=True, timeout=300)
            subprocess.run(["git", "-C", str(dest), "remote", "set-url", "origin", f"https://github.com/{repo}.git"],
                           check=True, capture_output=True, timeout=30)
            sha = subprocess.run(["git", "-C", str(dest), "rev-parse", "HEAD"], check=True, capture_output=True,
                                 text=True, timeout=30).stdout.strip()
            _readonly(dest)
            out["repos"][repo] = {"sha": sha}
        except Exception as exc:  # one unreadable repo doesn't stop the others
            msg = str(exc).replace(token, "***") if "token" in locals() and token else str(exc)
            log.warning("checking out %s failed: %s", repo, msg)
            out["repos"][repo] = {"skipped": f"couldn't be read ({type(exc).__name__})"}
    return out


def release(co: dict | None) -> None:
    """Delete a checkout (its files were made read-only, so make them writable first)."""
    if not co or not co.get("root"):
        return
    root = Path(co["root"])
    if not root.exists():
        return

    def fix(func, path, _exc):
        os.chmod(path, stat.S_IWUSR | stat.S_IRUSR | stat.S_IXUSR)
        func(path)

    shutil.rmtree(root, onerror=fix)


def describe(co: dict | None) -> str:
    """For the agent's prompt: where the code is, and what couldn't be read."""
    if not co:
        return ""
    read = [f"{r} @ {v['sha'][:7]}" for r, v in co["repos"].items() if v.get("sha")]
    skipped = [f"{r} ({v['skipped']})" for r, v in co["repos"].items() if v.get("skipped")]
    return ("Your working directory is a read-only checkout of the product's code: "
            + (", ".join(read) or "none could be read")
            + (f". Not read: {', '.join(skipped)}" if skipped else "")
            + ". Read the code (Read, Grep, Glob) before deciding what changes in each repo: name the files and the "
              "existing contracts you build on. Under Agent notes, list each repo you read as 'Read: <repo> @ <sha>'"
              ", and any affected repo you didn't as 'Not read: <repo>: <why>'.")

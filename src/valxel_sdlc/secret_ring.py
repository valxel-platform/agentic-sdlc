"""The secrets the tool checks or signs with, as key rings: current first, then previous.

A rotation without downtime needs two values for a while: checks accept either, signing uses the
current one. Each named secret is read from

    VALXEL_SECRETS_DIR/<NAME> and <NAME>.previous   files, as a secret manager mounts them; re-read
                                                    when they change, so a rotation needs no restart
    <NAME> and <NAME>_PREVIOUS                       environment variables, otherwise

Rotating, e.g. the GitHub webhook secret: put the new value in <NAME> and the old one in
<NAME>.previous, change the secret at GitHub, then remove <NAME>.previous once nothing sends the
old one. Sessions signed with the previous value stay valid until they expire.

`weak` says why a value isn't fit to protect anything: too short, too few distinct characters, or
an example published in this repository.
"""

from __future__ import annotations

import hmac
import os
import threading
from pathlib import Path
from typing import Callable

# Values published in this repository or its docs: never acceptable outside a laptop.
PUBLISHED = frozenset({
    "local-dev-only-session-secret-change-me-0000",
})
WEAK_PASSWORDS = frozenset({"", "valxel", "postgres", "password", "admin", "changeme", "change-me", "secret"})

_cache: dict[Path, tuple[int, str]] = {}
_lock = threading.Lock()


def _read(path: Path) -> str:
    """A secret file's value, re-read only when the file changes."""
    try:
        mtime = path.stat().st_mtime_ns
    except FileNotFoundError:
        return ""
    with _lock:
        hit = _cache.get(path)
        if hit and hit[0] == mtime:
            return hit[1]
    value = path.read_text().strip()
    with _lock:
        _cache[path] = (mtime, value)
    return value


def values(name: str, env=None) -> list[str]:
    """The secret's values, current first: [current] or [current, previous]; [] if unset."""
    env = env if env is not None else os.environ
    folder = env.get("VALXEL_SECRETS_DIR", "")
    if folder and (Path(folder) / name).exists():
        found = [_read(Path(folder) / name), _read(Path(folder) / f"{name}.previous")]
    else:
        found = [env.get(name, ""), env.get(f"{name}_PREVIOUS", "")]
    out: list[str] = []
    for v in found:
        v = (v or "").strip()
        if v and v not in out:
            out.append(v)
    return out


def current(name: str, env=None) -> str:
    vals = values(name, env)
    return vals[0] if vals else ""


def any_matches(name: str, check: Callable[[str], bool], env=None) -> bool:
    """Whether any of the secret's values passes `check` (each compared in constant time by it)."""
    return any(check(v) for v in values(name, env))


def bearer_ok(name: str, header: str, env=None) -> bool:
    """An Authorization header carrying any of the secret's values as a bearer token."""
    return any_matches(name, lambda v: hmac.compare_digest(header or "", f"Bearer {v}"), env)


def weak(secret: str, *, min_length: int = 32) -> str:
    """Why `secret` can't protect anything, or "" when it can."""
    s = secret or ""
    if s in PUBLISHED or "change-me" in s.lower() or "changeme" in s.lower():
        return "an example value published in this repository"
    if len(s) < min_length:
        return f"shorter than {min_length} characters"
    if len(set(s)) < 10:
        return "too few distinct characters to be random"
    return ""


def weak_password(password: str) -> str:
    """Why a database password is unfit outside a laptop, or ""."""
    if password.lower() in WEAK_PASSWORDS:
        return "a default or example password"
    if len(password) < 16:
        return "shorter than 16 characters"
    return ""

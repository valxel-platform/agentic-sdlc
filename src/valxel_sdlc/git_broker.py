"""Git for agent sessions without GitHub tokens in the sandbox (the engineering review's egress
recommendation; ADR 0008).

A coder session used to get a token for its one repository, and an architect a read token for the
repos it reads. Narrow as they were, both sat in the sandbox, where code the session runs (a
dependency's script, a prompt-injected command) could read them, and the sandbox could reach all of
github.com: a branch pushed to someone else's repository was a way out.

With the broker on (VALXEL_GIT_BROKER_URL and VALXEL_GIT_BROKER_KEY), the session gets a *pass*
instead of tokens:

    tool  ── seal({session, write repo, read repos, their tokens, expiry}) ──▶ pass (AES-GCM)
    sandbox  git remote = <broker>/git/<owner>/<repo>.git, password = pass
    egress proxy  opens the pass with the same key, checks the repository and the operation,
                  puts the real token on the request and sends it to github.com

- The tokens are encrypted in the pass: useless without the key, which only the tool and the
  proxy hold. The pass is useless outside the cluster: only the proxy accepts it.
- Push (git-receive-pack) only to the session's own repository; fetch only from the repositories
  the session was given. Nothing else on github.com is reachable through the broker, and the
  proxy no longer tunnels to github.com directly (egress_proxy.allow_list).
- Stateless: any proxy replica opens any pass; nothing to register or clean up. A pass expires
  with the session (its timeout plus a margin), and the tokens in it within the hour anyway.
- Every brokered request is logged by the proxy with the session, repository, operation and bytes.

Without the broker, sessions clone from github.com with the token as before: laptops without the
key, and tests.
"""

from __future__ import annotations

import base64
import json
import os
import re
import secrets
import time
from dataclasses import dataclass, field

PREFIX = "vxg1."  # a pass, version 1
_REPO = re.compile(r"[A-Za-z0-9-]{1,39}/[A-Za-z0-9_.-]{1,100}")
_URL = re.compile(r"https?://[A-Za-z0-9.-]+(:[0-9]{1,5})?")
MARGIN_S = 900  # a pass outlives its session's timeout by this much


@dataclass
class Grant:
    """What one session may do through the broker."""

    session: str
    tokens: dict[str, str] = field(default_factory=dict)  # repo (lower-case) -> its GitHub token
    write: str = ""  # the one repository it may push to, or none
    exp: int = 0

    def may_read(self, repo: str) -> bool:
        return repo.lower() in self.tokens

    def may_write(self, repo: str) -> bool:
        return bool(self.write) and repo.lower() == self.write

    def token(self, repo: str) -> str:
        return self.tokens.get(repo.lower(), "")


def url(env=None) -> str:
    """The broker as sessions reach it (the egress proxy), or '' when it's off."""
    e = env if env is not None else os.environ
    u = (e.get("VALXEL_GIT_BROKER_URL") or "").strip().rstrip("/")
    if u and not _URL.fullmatch(u):
        raise RuntimeError(f"VALXEL_GIT_BROKER_URL must be http(s)://host[:port], not {u!r}")
    return u


def key(env=None) -> bytes:
    """The 32-byte key passes are sealed with: the secret VALXEL_GIT_BROKER_KEY (64 hex characters)
    from the tool's secret store (valxel_sdlc.secret_ring), or the file VALXEL_GIT_BROKER_KEY_FILE
    names (the egress proxy's mount). b'' when unset."""
    e = env if env is not None else os.environ
    f = e.get("VALXEL_GIT_BROKER_KEY_FILE", "")
    if f:
        with open(f, encoding="utf-8") as fh:
            raw = fh.read().strip()
    else:
        from .secret_ring import current

        raw = (current("VALXEL_GIT_BROKER_KEY", e) or "").strip()
    if not raw:
        return b""
    try:
        k = bytes.fromhex(raw)
    except ValueError:
        k = b""
    if len(k) != 32:
        raise RuntimeError("VALXEL_GIT_BROKER_KEY must be 64 hex characters (openssl rand -hex 32)")
    return k


def enabled(env=None) -> bool:
    return bool(url(env) and key(env))


def _aead(k: bytes):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    return AESGCM(k)


def seal(grant: Grant, k: bytes) -> str:
    body = json.dumps({"s": grant.session, "t": grant.tokens, "w": grant.write, "e": grant.exp},
                      separators=(",", ":")).encode()
    nonce = secrets.token_bytes(12)
    ct = _aead(k).encrypt(nonce, body, PREFIX.encode())
    return PREFIX + base64.urlsafe_b64encode(nonce + ct).decode().rstrip("=")


def open_pass(token: str, k: bytes, now: float | None = None) -> Grant | None:
    """The grant a pass carries, or None: not a pass, sealed with another key, altered, or expired."""
    if not token.startswith(PREFIX) or not k:
        return None
    raw = token[len(PREFIX):]
    try:
        blob = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
        body = json.loads(_aead(k).decrypt(blob[:12], blob[12:], PREFIX.encode()))
    except Exception:  # noqa: BLE001 - any failure means no grant
        return None
    g = Grant(session=str(body.get("s", "")), tokens={str(a): str(b) for a, b in (body.get("t") or {}).items()},
              write=str(body.get("w", "")), exp=int(body.get("e", 0)))
    return g if g.exp > (time.time() if now is None else now) else None


def broker_spec(spec: dict, env=None, now: float | None = None) -> dict:
    """The session spec with its tokens swapped for one pass and its clones pointed at the broker.
    The spec unchanged when the broker is off or the session clones nothing."""
    u, k = url(env), key(env)
    co, reads = spec.get("checkout") or None, list(spec.get("read_checkouts") or [])
    if not (u and k) or not (co or reads):
        return spec
    tokens: dict[str, str] = {}
    for item in ([co] if co else []) + reads:
        repo = str(item.get("repo", ""))
        if not _REPO.fullmatch(repo):
            raise ValueError(f"not a repository name: {repo!r}")
        tokens[repo.lower()] = str(item.get("token", ""))
    exp = int((time.time() if now is None else now) + int(spec.get("timeout_s", 3600)) + MARGIN_S)
    p = seal(Grant(session=str(spec.get("id", "")), tokens=tokens,
                   write=str(co["repo"]).lower() if co else "", exp=exp), k)
    out = dict(spec)
    if co:
        out["checkout"] = {**co, "token": p, "via": u}
    if reads:
        out["read_checkouts"] = [{**rc, "token": p, "via": u} for rc in reads]
    return out


def remote_url(repo: str, via: str = "") -> str:
    """Where a session's clone of `repo` points: the broker when `via` names it, else GitHub."""
    if not via:
        return f"https://github.com/{repo}.git"
    if not _URL.fullmatch(via):
        raise ValueError(f"not a broker address: {via!r}")
    return f"{via}/git/{repo}.git"


def host(u: str) -> str:
    """The host part of a broker URL, for allowlists."""
    return u.split("://", 1)[-1].split(":", 1)[0].split("/", 1)[0]


# ------------------------------------------------------------------ the proxy's side

_GIT_PATH = re.compile(r"/git/([A-Za-z0-9-]{1,39}/[A-Za-z0-9_.-]{1,100}?)\.git/"
                       r"(info/refs|git-upload-pack|git-receive-pack)(\?.*)?")


def parse_target(target: str) -> tuple[str, str, str] | None:
    """(repo, operation, upstream path) for a broker request target, or None. operation is 'read'
    or 'write' (git-receive-pack, or the refs advertisement for one). Absolute-form targets
    (http://host:port/git/...), as a client sends to a proxy, count by their path."""
    if target.startswith(("http://", "https://")):
        rest = target.split("://", 1)[1]
        target = "/" + rest.split("/", 1)[1] if "/" in rest else "/"
    m = _GIT_PATH.fullmatch(target)
    if not m:
        return None
    repo, endpoint, query = m.group(1), m.group(2), (m.group(3) or "")
    if endpoint == "info/refs":
        service = dict(p.split("=", 1) for p in query[1:].split("&") if "=" in p).get("service", "")
        if service not in ("git-upload-pack", "git-receive-pack"):
            return None
        op = "write" if service == "git-receive-pack" else "read"
    else:
        op = "write" if endpoint == "git-receive-pack" else "read"
    return repo, op, f"/{repo}.git/{endpoint}{query}"

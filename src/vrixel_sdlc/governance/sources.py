"""Where governance files come from.

Every source answers two questions: which commit is `ref` right now (resolve), and what did a
file contain at that commit (read). Reads are keyed by commit SHA, so a feature pinned to a SHA
keeps reading the same constitution even after the product org amends it.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from pathlib import Path
from typing import Callable, Protocol
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, build_opener

from .errors import GovernanceError


class GovernanceSource(Protocol):
    def resolve(self, org: str, repo: str, ref: str) -> str: ...
    def read(self, org: str, repo: str, path: str, sha: str, *, optional: bool = False) -> str | None: ...


class LocalSource:
    """Reads <root>/<org>/<repo>/<path>. For development and tests: the 'commit SHA' is a hash
    of the repo directory's contents, and content is snapshotted in memory at resolve time so
    pinned reads stay stable in-process. Use GitHubSource anywhere threads must survive restarts."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self._snapshots: dict[tuple[str, str, str], dict[str, str]] = {}
        self._lock = threading.Lock()

    def _dir(self, org: str, repo: str) -> Path:
        for part in (org, repo):
            if not part or "/" in part or part.startswith("."):
                raise GovernanceError(f"Invalid repo coordinates {org}/{repo}")
        d = self.root / org / repo
        if not d.is_dir():
            raise GovernanceError(f"Governance repo {org}/{repo} not found under {self.root}")
        return d

    def _tree(self, org: str, repo: str) -> dict[str, str]:
        d = self._dir(org, repo)
        return {str(p.relative_to(d)): p.read_text(encoding="utf-8") for p in sorted(d.rglob("*")) if p.is_file()}

    def resolve(self, org: str, repo: str, ref: str) -> str:
        tree = self._tree(org, repo)
        digest = hashlib.sha1(json.dumps(tree, sort_keys=True).encode()).hexdigest()
        with self._lock:
            self._snapshots.setdefault((org, repo, digest), tree)
        return digest

    def read(self, org: str, repo: str, path: str, sha: str, *, optional: bool = False) -> str | None:
        with self._lock:
            tree = self._snapshots.get((org, repo, sha))
        if tree is None:
            self.resolve(org, repo, "")
            with self._lock:
                tree = self._snapshots.get((org, repo, sha))
        if tree is None:
            raise GovernanceError(
                f"{org}/{repo}@{sha[:7]} is no longer available locally; the files changed since the feature "
                "was pinned. Use the github source for durable pins."
            )
        if path not in tree:
            if optional:
                return None
            raise GovernanceError(f"{org}/{repo}@{sha[:7]} has no {path}")
        return tree[path]


class TokenProvider(Protocol):
    def token(self, org: str) -> str: ...


class GitHubSource:
    """Reads governance repos through the GitHub REST API with a token scoped to each org."""

    def __init__(self, tokens: TokenProvider, *, api: str = "https://api.github.com", opener=None,
                 timeout: float = 10.0) -> None:
        self.tokens = tokens
        self.api = api.rstrip("/")
        self.opener = opener or build_opener()
        self.timeout = timeout
        self._cache: dict[tuple[str, str, str, str], str | None] = {}
        self._lock = threading.Lock()

    def _get(self, org: str, path: str, accept: str) -> str | None:
        req = Request(f"{self.api}{path}", headers={
            "Authorization": f"Bearer {self.tokens.token(org)}",
            "Accept": accept,
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "vrixel-agentic-sdlc",
        })
        try:
            with self.opener.open(req, timeout=self.timeout) as resp:
                return resp.read().decode("utf-8")
        except HTTPError as exc:
            if exc.code == 404:
                return None
            raise GovernanceError(f"GitHub {exc.code} reading {path} for org {org}") from exc

    def resolve(self, org: str, repo: str, ref: str) -> str:
        sha = self._get(org, f"/repos/{quote(org)}/{quote(repo)}/commits/{quote(ref, safe='')}",
                        "application/vnd.github.sha")
        if not sha:
            raise GovernanceError(f"Governance repo {org}/{repo} or ref {ref!r} not found (or no access)")
        return sha.strip()

    def read(self, org: str, repo: str, path: str, sha: str, *, optional: bool = False) -> str | None:
        key = (org, repo, path, sha)
        with self._lock:
            hit, text = key in self._cache, self._cache.get(key)
        if not hit:
            text = self._get(org, f"/repos/{quote(org)}/{quote(repo)}/contents/{quote(path)}?ref={quote(sha)}",
                             "application/vnd.github.raw+json")
            with self._lock:  # content at a commit SHA never changes, so caching is safe
                self._cache[key] = text
        if text is None and not optional:
            raise GovernanceError(f"{org}/{repo}@{sha[:7]} has no {path}")
        return text


class EnvTokenProvider:
    """VRIXEL_GITHUB_TOKEN__<ORG> (org upper-cased, '-' as '_'), else VRIXEL_GITHUB_TOKEN."""

    def __init__(self, env: dict[str, str] | None = None) -> None:
        import os

        self.env = env if env is not None else os.environ

    def token(self, org: str) -> str:
        key = "VRIXEL_GITHUB_TOKEN__" + org.upper().replace("-", "_")
        tok = self.env.get(key) or self.env.get("VRIXEL_GITHUB_TOKEN")
        if not tok:
            raise GovernanceError(f"No GitHub token for org {org}: set {key} or VRIXEL_GITHUB_TOKEN")
        return tok


class GitHubAppTokenProvider:
    """One GitHub App installed in the tools org and every product org. Mints a short-lived
    installation token per org, narrowed to `permissions` (contents:read for governance reads;
    the delivery adapter asks for more). Requires `pip install .[github]`."""

    def __init__(self, app_id: str, private_key_pem: str, *, api: str = "https://api.github.com", opener=None,
                 clock: Callable[[], float] = time.time, permissions: dict[str, str] | None = None) -> None:
        self.app_id = str(app_id)
        self.key = private_key_pem
        self.api = api.rstrip("/")
        self.opener = opener or build_opener()
        self.clock = clock
        self.permissions = dict(permissions or {"contents": "read"})
        self._tokens: dict[str, tuple[str, float]] = {}
        self._lock = threading.Lock()

    def _jwt(self) -> str:
        import jwt  # lazy: optional dependency

        now = int(self.clock())
        return jwt.encode({"iat": now - 60, "exp": now + 540, "iss": self.app_id}, self.key, algorithm="RS256")

    def _call(self, method: str, path: str, bearer: str, body: dict | None = None) -> dict:
        data = json.dumps(body).encode() if body is not None else None
        req = Request(f"{self.api}{path}", data=data, method=method, headers={
            "Authorization": f"Bearer {bearer}", "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "vrixel-agentic-sdlc",
        })
        try:
            with self.opener.open(req, timeout=10) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except HTTPError as exc:
            raise GovernanceError(f"GitHub App {method} {path} failed with {exc.code}") from exc

    def token(self, org: str) -> str:
        with self._lock:
            cached = self._tokens.get(org)
            if cached and cached[1] - 300 > self.clock():
                return cached[0]
        app_jwt = self._jwt()
        inst = self._call("GET", f"/orgs/{quote(org)}/installation", app_jwt)
        tok = self._call("POST", f"/app/installations/{inst['id']}/access_tokens", app_jwt,
                         {"permissions": self.permissions})
        from datetime import datetime

        expires = datetime.fromisoformat(tok["expires_at"].replace("Z", "+00:00")).timestamp()
        with self._lock:
            self._tokens[org] = (tok["token"], expires)
        return tok["token"]

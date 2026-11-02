"""Sign-in and authorization.

GitHub sign-in (OAuth web flow, scope read:org) records who a person is and which orgs and teams
they belong to. The session is an HMAC-signed cookie holding only that identity; the GitHub token
is used once at sign-in and never stored.

Approvers in a lane's product.yaml are GitHub handles: "org/team-slug" for a team (preferred) or
"login" for a person. A gate item may only be decided by someone matching its approvers.
Dev sign-in (VRIXEL_AUTH=dev) lets you pick any identity; never enable it in production.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from dataclasses import asdict, dataclass, field
from typing import Callable
from urllib.parse import urlencode
from urllib.request import Request, build_opener

SESSION_COOKIE = "vrixel_session"
STATE_COOKIE = "vrixel_oauth_state"
SESSION_TTL = 12 * 3600

# Who may act on an escalation: the approvers of the gate that phase leads to.
ESCALATION_GATE = {"discovery": "g1", "spec": "g2", "design": "g3", "architecture": "g4", "plan": "g4",
                   "build": "g5", "release": "g6", "operate": "g6", "delivery": "g6",
                   "security": "security", "provisioning": "g4"}  # approvers.security, falling back to G5's approvers


@dataclass
class User:
    login: str
    name: str = ""
    avatar_url: str = ""
    orgs: list[str] = field(default_factory=list)
    teams: list[str] = field(default_factory=list)  # "org/team-slug"

    def matches(self, approver: str) -> bool:
        a = approver.strip().lstrip("@").lower()
        if "/" in a:
            return a in {t.lower() for t in self.teams}
        return a == self.login.lower()

    def member_of(self, org: str) -> bool:
        return org.lower() in {o.lower() for o in self.orgs}


def can_decide(user: User, item: dict) -> bool:
    return any(user.matches(a) for a in item.get("approvers") or [])


class SessionSigner:
    def __init__(self, secret: str) -> None:
        if len(secret) < 32:
            raise ValueError("VRIXEL_SESSION_SECRET must be at least 32 characters")
        self.key = secret.encode()

    def dumps(self, payload: dict) -> str:
        body = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode().rstrip("=")
        sig = hmac.new(self.key, body.encode(), hashlib.sha256).hexdigest()
        return f"{body}.{sig}"

    def loads(self, token: str | None) -> dict | None:
        if not token or "." not in token:
            return None
        body, sig = token.rsplit(".", 1)
        expected = hmac.new(self.key, body.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected):
            return None
        try:
            payload = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
        except (ValueError, json.JSONDecodeError):
            return None
        if payload.get("exp", 0) < time.time():
            return None
        return payload


def session_payload(user: User) -> dict:
    return {**asdict(user), "exp": int(time.time()) + SESSION_TTL}


def user_from_payload(payload: dict | None) -> User | None:
    if not payload:
        return None
    payload = {k: v for k, v in payload.items() if k != "exp"}
    return User(**payload)


@dataclass
class AuthConfig:
    mode: str  # github | dev
    secret: str
    client_id: str = ""
    client_secret: str = ""
    public_url: str = "http://localhost:8080"

    @classmethod
    def from_env(cls) -> "AuthConfig":
        mode = (os.environ.get("VRIXEL_AUTH") or "github").lower()
        secret = os.environ.get("VRIXEL_SESSION_SECRET") or ""
        if not secret:
            if mode != "dev":
                raise RuntimeError("Set VRIXEL_SESSION_SECRET (32+ random characters) for GitHub sign-in")
            secret = secrets.token_hex(32)  # dev only: sessions reset on restart
        cfg = cls(mode=mode, secret=secret,
                  client_id=os.environ.get("VRIXEL_GITHUB_OAUTH_CLIENT_ID", ""),
                  client_secret=os.environ.get("VRIXEL_GITHUB_OAUTH_CLIENT_SECRET", ""),
                  public_url=(os.environ.get("VRIXEL_PUBLIC_URL") or "http://localhost:8080").rstrip("/"))
        if mode == "github" and not (cfg.client_id and cfg.client_secret):
            raise RuntimeError("GitHub sign-in needs VRIXEL_GITHUB_OAUTH_CLIENT_ID and VRIXEL_GITHUB_OAUTH_CLIENT_SECRET")
        if mode not in ("github", "dev"):
            raise RuntimeError(f"VRIXEL_AUTH must be github or dev, not {mode!r}")
        return cfg

    @property
    def secure_cookies(self) -> bool:
        return self.public_url.startswith("https://")

    @property
    def redirect_uri(self) -> str:
        return f"{self.public_url}/api/auth/callback"


class GitHubOAuth:
    """The OAuth web flow plus the three reads that establish identity."""

    def __init__(self, cfg: AuthConfig, opener=None, api: str = "https://api.github.com",
                 web: str = "https://github.com") -> None:
        self.cfg, self.api, self.web = cfg, api.rstrip("/"), web.rstrip("/")
        self.opener = opener or build_opener()

    def authorize_url(self, state: str) -> str:
        q = urlencode({"client_id": self.cfg.client_id, "redirect_uri": self.cfg.redirect_uri,
                       "scope": "read:org", "state": state, "allow_signup": "false"})
        return f"{self.web}/login/oauth/authorize?{q}"

    def _json(self, req: Request) -> dict | list:
        with self.opener.open(req, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def exchange(self, code: str) -> str:
        data = urlencode({"client_id": self.cfg.client_id, "client_secret": self.cfg.client_secret,
                          "code": code, "redirect_uri": self.cfg.redirect_uri}).encode()
        res = self._json(Request(f"{self.web}/login/oauth/access_token", data=data, method="POST",
                                 headers={"Accept": "application/json"}))
        if "access_token" not in res:
            raise PermissionError(f"GitHub sign-in failed: {res.get('error_description') or res.get('error')}")
        return res["access_token"]

    def identity(self, token: str) -> User:
        def get(path: str):
            return self._json(Request(f"{self.api}{path}", headers={
                "Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "vrixel-agentic-sdlc"}))

        me = get("/user")
        orgs = [o["login"] for o in get("/user/orgs?per_page=100")]
        teams = [f"{t['organization']['login']}/{t['slug']}" for t in get("/user/teams?per_page=100")]
        return User(login=me["login"], name=me.get("name") or "", avatar_url=me.get("avatar_url") or "",
                    orgs=orgs, teams=teams)


def new_state() -> str:
    return secrets.token_urlsafe(24)


Clock = Callable[[], float]

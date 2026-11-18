"""Sign-in and authorization.

GitHub sign-in (OAuth web flow, scopes read:org and user:email) records who a person is and which orgs and teams
they belong to. The session is an HMAC-signed cookie holding only that identity; the GitHub token
is used once at sign-in and never stored.

Approvers in a lane's product.yaml are GitHub handles: "org/team-slug" for a team (preferred) or
"login" for a person. A gate item may only be decided by someone matching its approvers.
Dev sign-in (VALXEL_AUTH=dev) lets you pick any identity; never enable it in production.
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

SESSION_COOKIE = "valxel_session"
STATE_COOKIE = "valxel_oauth_state"
NEXT_COOKIE = "valxel_next"  # the page to return to after sign-in
SESSION_TTL = 12 * 3600
# A workspace's admins and approvers sign in again after 4 hours (the sign-in design, A4): their
# session can approve a release or change who else can.
PRIVILEGED_TTL = 4 * 3600
OIDC_COOKIE = "valxel_oidc"  # state, nonce and PKCE verifier between the redirect and the callback

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
    email: str = ""  # verified, when the sign-in provides one
    idp: str = "github"  # how they signed in: github, cognito or dev; mcp for a call from api/mcp.py
    # Two factors at sign-in: GitHub (which requires two-factor sign-in of everyone who contributes
    # code) and dev sign-in count; through Cognito, only a passkey (web/identity.py). A workspace's
    # admins and group approvers need it to act (api/app.py).
    strong: bool = True
    signed_in_at: int = 0  # epoch seconds
    # Read from the workspaces on every request, never trusted from the cookie (valxel_sdlc.workspaces):
    groups: list[str] = field(default_factory=list)  # "<workspace>/<group>"
    roles: dict[str, str] = field(default_factory=dict)  # workspace -> role
    products: dict[str, list[str]] = field(default_factory=dict)  # workspace -> its products they may see ([] = all)

    def matches(self, approver: str) -> bool:
        a = approver.strip().lstrip("@").lower()
        if a.startswith("group:"):  # a workspace's approver group
            return a[len("group:"):] in {g.lower() for g in self.groups}
        if "/" in a:
            return a in {t.lower() for t in self.teams}
        return a == self.login.lower()

    def member_of(self, org: str) -> bool:
        return org.lower() in {o.lower() for o in self.orgs}


def can_decide(user: User, item: dict) -> bool:
    """A GitHub team or login decides as before. A workspace group decides only from a two-factor
    sign-in (a passkey through Cognito): its members may have joined with nothing but an email."""
    approvers = item.get("approvers") or []
    if any(user.matches(a) for a in approvers if not a.strip().lower().startswith("group:")):
        return True
    return user.strong and any(user.matches(a) for a in approvers)


def needs_passkey(user: User, approvers: list[str]) -> bool:
    """They'd decide through a workspace group if they had signed in with a passkey."""
    return not user.strong and not can_decide(user, {"approvers": approvers}) and any(
        user.matches(a) for a in approvers)


PASSKEY_NEEDED = ("Approving through a workspace group needs a passkey sign-in. Choose 'Use a passkey' "
                  "(add one first from your workspace page), then try again.")


class SessionSigner:
    """Signs session cookies with the current key and accepts any key in the ring, so rotating
    VALXEL_SESSION_SECRET signs no one out (valxel_sdlc.secret_ring). `secret` is a key, or a
    function returning the ring, current first."""

    def __init__(self, secret: str | Callable[[], list[str]], previous: tuple[str, ...] = ()) -> None:
        self._ring = secret if callable(secret) else (lambda: [secret, *previous])
        self._check(self._ring())

    @staticmethod
    def _check(keys: list[str]) -> list[str]:
        if not keys or any(len(k) < 32 for k in keys):
            raise ValueError("VALXEL_SESSION_SECRET must be at least 32 characters")
        return keys

    def _keys(self) -> list[bytes]:
        return [k.encode() for k in self._check(self._ring())]

    def dumps(self, payload: dict) -> str:
        body = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode().rstrip("=")
        sig = hmac.new(self._keys()[0], body.encode(), hashlib.sha256).hexdigest()
        return f"{body}.{sig}"

    def loads(self, token: str | None) -> dict | None:
        if not token or "." not in token:
            return None
        body, sig = token.rsplit(".", 1)
        if not any(hmac.compare_digest(sig, hmac.new(k, body.encode(), hashlib.sha256).hexdigest())
                   for k in self._keys()):
            return None
        try:
            payload = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
        except (ValueError, json.JSONDecodeError):
            return None
        if payload.get("exp", 0) < time.time():
            return None
        return payload


IDENTITY = ("login", "name", "avatar_url", "orgs", "teams", "email", "idp", "strong", "signed_in_at")  # the cookie's


def session_payload(user: User) -> dict:
    d = asdict(user)
    now = int(time.time())
    return {**{k: d[k] for k in IDENTITY}, "signed_in_at": d["signed_in_at"] or now, "exp": now + SESSION_TTL}


def user_from_payload(payload: dict | None) -> User | None:
    if not payload:
        return None
    return User(**{k: v for k, v in payload.items() if k in IDENTITY})


@dataclass
class AuthConfig:
    mode: str  # github | cognito | dev (any identity; never in production)
    secret: str
    client_id: str = ""  # GitHub OAuth
    client_secret: str = ""
    public_url: str = "http://localhost:8080"
    from_store: bool = False  # the key ring is re-read from VALXEL_SECRETS_DIR or the environment
    # Google, email and passkeys through a Cognito user pool (web/identity.py, deploy/cognito.yaml)
    cognito_domain: str = ""
    cognito_client_id: str = ""
    cognito_client_secret: str = ""
    cognito_issuer: str = ""
    cognito_google: bool = True

    @property
    def github_enabled(self) -> bool:
        return bool(self.client_id and self.client_secret)

    @property
    def cognito_enabled(self) -> bool:
        return bool(self.cognito_domain and self.cognito_client_id and self.cognito_client_secret and self.cognito_issuer)

    @classmethod
    def from_env(cls) -> "AuthConfig":
        from ..secret_ring import current, weak

        mode = (os.environ.get("VALXEL_AUTH") or "github").lower()
        secret = current("VALXEL_SESSION_SECRET")
        from_store = bool(secret)
        if not secret:
            if mode != "dev":
                raise RuntimeError("Set VALXEL_SESSION_SECRET (32+ random characters) for GitHub sign-in")
            secret = secrets.token_hex(32)  # dev only: sessions reset on restart
        elif mode != "dev" and weak(secret):
            raise RuntimeError(f"VALXEL_SESSION_SECRET is {weak(secret)}: anyone could sign in as anyone. "
                               "Use 32+ random characters, e.g. `openssl rand -hex 32`")
        cfg = cls(mode=mode, secret=secret, from_store=from_store,
                  client_id=os.environ.get("VALXEL_GITHUB_OAUTH_CLIENT_ID", ""),
                  client_secret=current("VALXEL_GITHUB_OAUTH_CLIENT_SECRET"),
                  public_url=(os.environ.get("VALXEL_PUBLIC_URL") or "http://localhost:8080").rstrip("/"),
                  cognito_domain=os.environ.get("VALXEL_COGNITO_DOMAIN", ""),
                  cognito_client_id=os.environ.get("VALXEL_COGNITO_CLIENT_ID", ""),
                  cognito_client_secret=current("VALXEL_COGNITO_CLIENT_SECRET"),
                  cognito_issuer=os.environ.get("VALXEL_COGNITO_ISSUER", ""),
                  cognito_google=(os.environ.get("VALXEL_COGNITO_GOOGLE") or "true").lower() != "false")
        if mode not in ("github", "cognito", "dev"):
            raise RuntimeError(f"VALXEL_AUTH must be github, cognito or dev, not {mode!r}")
        # GitHub mode needs GitHub's OAuth app and may add Cognito; Cognito mode the reverse.
        if mode == "github" and not cfg.github_enabled:
            raise RuntimeError("GitHub sign-in needs VALXEL_GITHUB_OAUTH_CLIENT_ID and VALXEL_GITHUB_OAUTH_CLIENT_SECRET")
        if mode == "cognito" and not cfg.cognito_enabled:
            raise RuntimeError("Cognito sign-in needs VALXEL_COGNITO_DOMAIN, VALXEL_COGNITO_CLIENT_ID, "
                               "VALXEL_COGNITO_CLIENT_SECRET and VALXEL_COGNITO_ISSUER")
        return cfg

    def ring(self) -> Callable[[], list[str]]:
        """The session keys, current first: re-read on each use when they come from the store."""
        if not self.from_store:
            return lambda: [self.secret]
        from ..secret_ring import values

        return lambda: values("VALXEL_SESSION_SECRET") or [self.secret]

    @property
    def secure_cookies(self) -> bool:
        return self.public_url.startswith("https://")

    @property
    def redirect_uri(self) -> str:
        return f"{self.public_url}/api/auth/callback"

    @property
    def oidc_redirect_uri(self) -> str:
        return f"{self.public_url}/api/auth/oidc/callback"

    def cognito(self, opener=None):
        """The Cognito identity provider, or None when it isn't configured."""
        if not self.cognito_enabled:
            return None
        from .identity import CognitoProvider

        return CognitoProvider(domain=self.cognito_domain, client_id=self.cognito_client_id,
                               client_secret=self.cognito_client_secret, issuer=self.cognito_issuer,
                               redirect_uri=self.oidc_redirect_uri, google=self.cognito_google, opener=opener)


class GitHubOAuth:
    """The OAuth web flow plus the three reads that establish identity."""

    def __init__(self, cfg: AuthConfig, opener=None, api: str = "https://api.github.com",
                 web: str = "https://github.com") -> None:
        self.cfg, self.api, self.web = cfg, api.rstrip("/"), web.rstrip("/")
        self.opener = opener or build_opener()

    def authorize_url(self, state: str) -> str:
        q = urlencode({"client_id": self.cfg.client_id, "redirect_uri": self.cfg.redirect_uri,
                       "scope": "read:org user:email", "state": state, "allow_signup": "false"})
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
                "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "valxel-sdlc"}))

        me = get("/user")
        orgs = [o["login"] for o in get("/user/orgs?per_page=100")]
        teams = [f"{t['organization']['login']}/{t['slug']}" for t in get("/user/teams?per_page=100")]
        try:  # the primary address, and only if GitHub verified it: an invitation can be accepted by email
            email = next((e["email"] for e in get("/user/emails") if e.get("primary") and e.get("verified")), "")
        except Exception:  # an app installed before user:email was asked for
            email = ""
        return User(login=me["login"], name=me.get("name") or "", avatar_url=me.get("avatar_url") or "",
                    orgs=orgs, teams=teams, email=(email or "").lower())


def new_state() -> str:
    return secrets.token_urlsafe(24)


Clock = Callable[[], float]

"""Tools that connect through the vendor's OAuth consent screen: Jira, Lucid, Sentry and PagerDuty
(the sign-in design, A8).

One OAuth client per vendor, registered once by whoever runs Valxel; its id and secret come from
the environment and the secret store (VALXEL_<TOOL>_OAUTH_CLIENT_ID and _CLIENT_SECRET; Sentry's
public integration also needs VALXEL_SENTRY_OAUTH_APP_SLUG). A tool whose client isn't set shows
on the connections page as not available.

    start     an admin clicks Connect: Valxel signs a state (workspace, tool, who, a PKCE verifier
              where the vendor takes one) and sends them to the vendor's consent screen. The state
              also goes in a short-lived cookie, since Sentry's install doesn't hand state back.
    callback  the vendor sends them back with a code: Valxel exchanges it, asks the vendor who and
              what the token reaches (the Jira sites, the Lucid user, the Sentry org, the PagerDuty
              account), stores the tokens in the vault and records the connection.
    use       `access_token()` hands out a token, refreshing it first when it expires within five
              minutes. Atlassian's refresh tokens work once: a refresh holds a lock and reads the
              vault again inside it, so two callers don't spend the same refresh token, and a
              refused refresh is retried once with whatever the vault holds by then.
    health    the hourly check refreshes when due and makes the same who-am-I read.

Tokens never go in the database: the vault keeps them at `valxel/workspaces/<ws>/<tool>/<account>`
(AWS Secrets Manager, or files under VALXEL_SECRETS_DIR; in memory on a laptop). Disconnecting
deletes them.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
import secrets
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable
from urllib.error import HTTPError
from urllib.parse import quote, urlencode, urlparse
from urllib.request import Request, build_opener

log = logging.getLogger(__name__)

REFRESH_AHEAD = 300  # refresh tokens that expire within five minutes


class OAuthError(Exception):
    """What went wrong, in words the connections page can show."""


@dataclass(frozen=True)
class Vendor:
    tool: str
    name: str
    authorize: str  # the consent screen; "{slug}" is filled in for Sentry
    token: str  # "{installation}" is filled in for Sentry
    scopes: tuple[str, ...] = ()
    extra: dict = field(default_factory=dict)  # more authorize parameters
    pkce: bool = False
    json_body: bool = False  # the token endpoint takes JSON rather than a form
    revoke_hint: str = ""


VENDORS: dict[str, Vendor] = {v.tool: v for v in (
    Vendor("jira", "Jira", "https://auth.atlassian.com/authorize", "https://auth.atlassian.com/oauth/token",
           ("read:jira-work", "write:jira-work", "read:jira-user", "offline_access"),
           {"audience": "api.atlassian.com", "prompt": "consent"}, json_body=True),
    Vendor("lucid", "Lucid", "https://lucid.app/oauth2/authorize", "https://api.lucid.co/oauth2/token",
           ("lucidchart.document.app", "user.profile", "offline_access"), json_body=True),
    Vendor("sentry", "Sentry", "https://sentry.io/sentry-apps/{slug}/external-install/",
           "https://sentry.io/api/0/sentry-app-installations/{installation}/authorizations/", json_body=True),
    Vendor("pagerduty", "PagerDuty", "https://identity.pagerduty.com/oauth/authorize",
           "https://identity.pagerduty.com/oauth/token",
           ("incidents.read", "incidents.write", "services.read", "users.read"), pkce=True),
)}


@dataclass(frozen=True)
class Client:
    client_id: str
    client_secret: str
    app_slug: str = ""  # Sentry's public integration


def clients_from_env(env=None) -> dict[str, Client]:
    """The vendors whose OAuth client is set: VALXEL_<TOOL>_OAUTH_CLIENT_ID and _CLIENT_SECRET."""
    from .secret_ring import current

    env = env if env is not None else os.environ
    out = {}
    for tool in VENDORS:
        base = f"VALXEL_{tool.upper()}_OAUTH"
        cid, secret = env.get(f"{base}_CLIENT_ID", ""), current(f"{base}_CLIENT_SECRET", env) or ""
        slug = env.get(f"{base}_APP_SLUG", "")
        if cid and secret and (tool != "sentry" or slug):
            out[tool] = Client(cid, secret, slug)
    return out


# ---------------------------------------------------------------------- the vault
def vault_path(workspace: str, tool: str, account: str, prefix: str = "valxel") -> str:
    slug = re.sub(r"[^a-z0-9._-]+", "-", account.lower()).strip("-") or "default"
    return f"{prefix}/workspaces/{workspace}/{tool}/{slug}"


class MemoryVault:
    """For laptops and tests: tokens last as long as the process."""

    def __init__(self) -> None:
        self.data: dict[str, dict] = {}

    def get(self, path: str) -> dict | None:
        got = self.data.get(path)
        return dict(got) if got else None

    def put(self, path: str, value: dict) -> None:
        self.data[path] = dict(value)

    def delete(self, path: str) -> None:
        self.data.pop(path, None)


class FileVault:
    """Files under a folder, readable only by Valxel's user (VALXEL_SECRETS_DIR, when it's writable)."""

    def __init__(self, folder: str | Path) -> None:
        self.folder = Path(folder)

    def _file(self, path: str) -> Path:
        return self.folder / f"{path}.json"

    def get(self, path: str) -> dict | None:
        try:
            return json.loads(self._file(path).read_text())
        except FileNotFoundError:
            return None

    def put(self, path: str, value: dict) -> None:
        f = self._file(path)
        f.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        tmp = f.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as out:
            json.dump(value, out)
        os.replace(tmp, f)

    def delete(self, path: str) -> None:
        self._file(path).unlink(missing_ok=True)


def vault_for(env=None):
    from .secret_ring import store_kind

    env = env if env is not None else os.environ
    kind = store_kind(env)
    if kind == "aws":
        from .cloud.aws.secrets import AwsVault

        return AwsVault(region=env.get("AWS_REGION", ""))
    if kind == "gcp":
        from .cloud.gcp import project
        from .cloud.gcp.secrets import GcpVault

        return GcpVault(project=project(env))
    if kind == "files":
        return FileVault(env.get("VALXEL_SECRETS_DIR", "") or "/run/secrets/valxel")
    log.warning("Tool connection tokens are kept in memory (VALXEL_SECRET_STORE is env): they go when Valxel restarts")
    return MemoryVault()


# ---------------------------------------------------------------------- the flow
def _challenge(verifier: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()


class OAuthTools:
    """The four vendors' consent, tokens and who-am-I reads."""

    def __init__(self, clients: dict[str, Client], *, redirect_uri: str, vault=None, opener=None,
                 clock: Callable[[], float] = time.time) -> None:
        self.clients, self.redirect_uri = clients, redirect_uri
        self.vault = vault if vault is not None else MemoryVault()
        self.opener = opener or build_opener()
        self.clock = clock
        self._locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()

    def available(self, tool: str) -> bool:
        return tool in self.clients

    # ---------------------------------------------------------------- consent
    def start(self, signer, *, workspace: str, tool: str, actor: str) -> tuple[str, str]:
        """(the consent screen's URL, the signed state to keep in a cookie as well)."""
        if tool not in VENDORS:
            raise OAuthError(f"{tool} doesn't connect through OAuth")
        client = self.clients.get(tool)
        if client is None:
            raise OAuthError(f"{VENDORS[tool].name} isn't set up on this Valxel: its OAuth client settings are missing")
        v = VENDORS[tool]
        st = {"ws": workspace, "tool": tool, "by": actor, "n": secrets.token_urlsafe(8),
              "exp": int(self.clock()) + 1800}
        if v.pkce:
            st["pv"] = secrets.token_urlsafe(48)
        state = signer.dumps(st)
        if tool == "sentry":  # Sentry's install keeps no state; the cookie carries it
            return v.authorize.format(slug=quote(client.app_slug)) + "?" + urlencode({"state": state}), state
        q = {"client_id": client.client_id, "response_type": "code", "redirect_uri": self.redirect_uri,
             "state": state, **v.extra}
        if v.scopes:
            q["scope"] = " ".join(v.scopes)
        if v.pkce:
            q.update(code_challenge=_challenge(st["pv"]), code_challenge_method="S256")
        return f"{v.authorize}?{urlencode(q)}", state

    def finish(self, signer, *, state: str, cookie_state: str, code: str, actor: str,
               installation: str = "", org: str = "") -> dict:
        """The vendor's redirect back: check the state, exchange the code, find out what it reaches,
        keep the tokens. Returns {workspace, tool, account, reach}."""
        st = signer.loads(cookie_state) if cookie_state else None
        if not st or (state and state != cookie_state):
            raise OAuthError("This connection was started in another browser or has expired; start it again from the "
                             "connections page")
        if int(st.get("exp", 0)) < self.clock():
            raise OAuthError("This connection took longer than 30 minutes; start it again")
        if st.get("by") != actor:
            raise OAuthError("Someone else started this connection; start your own from the connections page")
        tool = st.get("tool", "")
        if tool not in self.clients:
            raise OAuthError(f"{tool} isn't set up on this Valxel")
        if not code:
            raise OAuthError(f"{VENDORS[tool].name} didn't give Valxel access (the consent was declined or cancelled)")
        if tool == "sentry" and not (installation and org):
            raise OAuthError("Sentry didn't say which installation was made; install again")
        tokens = self._exchange(tool, {"grant_type": "authorization_code", "code": code,
                                       **({"code_verifier": st["pv"]} if st.get("pv") else {})}, installation)
        account, reach = self.whoami(tool, tokens["access_token"], org=org)
        if tool == "sentry":
            reach["installation"] = installation
        self.vault.put(vault_path(st["ws"], tool, account), {**tokens, "reach": reach})
        return {"workspace": st["ws"], "tool": tool, "account": account, "reach": reach}

    # ---------------------------------------------------------------- tokens
    def _json(self, req: Request) -> dict:
        try:
            with self.opener.open(req, timeout=15) as resp:
                return json.loads(resp.read().decode("utf-8") or "{}")
        except HTTPError as exc:
            body = ""
            try:
                body = exc.read().decode("utf-8", "replace")[:300]
            except Exception:
                pass
            raise OAuthError(f"{urlparse(req.full_url).netloc} said {exc.code}" + (f": {body}" if body else "")) from exc

    def _exchange(self, tool: str, grant: dict, installation: str = "") -> dict:
        v, c = VENDORS[tool], self.clients[tool]
        body = {**grant, "client_id": c.client_id, "client_secret": c.client_secret}
        if grant.get("grant_type") == "authorization_code" and tool != "sentry":
            body["redirect_uri"] = self.redirect_uri
        url = v.token.format(installation=quote(installation))
        if v.json_body:
            req = Request(url, data=json.dumps(body).encode(), method="POST",
                          headers={"Content-Type": "application/json", "Accept": "application/json",
                                   "User-Agent": "valxel-sdlc"})
        else:
            req = Request(url, data=urlencode(body).encode(), method="POST",
                          headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json",
                                   "User-Agent": "valxel-sdlc"})
        got = self._json(req)
        access = got.get("access_token") or got.get("token") or ""
        if not access:
            raise OAuthError(f"{v.name} didn't return a token")
        if got.get("expires_in"):
            expires = self.clock() + float(got["expires_in"])
        elif got.get("expiresAt"):  # Sentry: an ISO time
            from datetime import datetime

            expires = datetime.fromisoformat(str(got["expiresAt"]).replace("Z", "+00:00")).timestamp()
        else:
            expires = 0.0  # doesn't expire
        return {"access_token": access, "refresh_token": got.get("refresh_token") or got.get("refreshToken") or "",
                "expires_at": expires, "scope": got.get("scope", "")}

    def _lock(self, path: str) -> threading.Lock:
        with self._guard:
            return self._locks.setdefault(path, threading.Lock())

    def access_token(self, workspace: str, tool: str, account: str) -> str:
        """A token that works for at least five more minutes, refreshed if need be."""
        path = vault_path(workspace, tool, account)
        held = self.vault.get(path)
        if not held:
            raise OAuthError(f"Valxel holds no {VENDORS[tool].name} token for {account}; reconnect it")
        if not self._due(held):
            return held["access_token"]
        with self._lock(path):
            held = self.vault.get(path) or held  # another caller may have refreshed while we waited
            if not self._due(held):
                return held["access_token"]
            try:
                fresh = self._refresh(tool, held)
            except OAuthError:
                again = self.vault.get(path) or {}
                if again.get("refresh_token") and again["refresh_token"] != held.get("refresh_token"):
                    held = again  # another process refreshed first: theirs is the live one
                    if not self._due(held):
                        return held["access_token"]
                    fresh = self._refresh(tool, held)
                else:
                    raise
            self.vault.put(path, {**held, **fresh, "refresh_token": fresh["refresh_token"] or held["refresh_token"]})
            return fresh["access_token"]

    def _due(self, held: dict) -> bool:
        exp = float(held.get("expires_at") or 0)
        return bool(exp) and exp - REFRESH_AHEAD <= self.clock()

    def _refresh(self, tool: str, held: dict) -> dict:
        if not held.get("refresh_token"):
            raise OAuthError(f"The {VENDORS[tool].name} token expired and can't be renewed; reconnect it")
        try:
            return self._exchange(tool, {"grant_type": "refresh_token", "refresh_token": held["refresh_token"]},
                                  (held.get("reach") or {}).get("installation", ""))
        except OAuthError as exc:
            raise OAuthError(f"{VENDORS[tool].name} wouldn't renew Valxel's access ({exc}); reconnect it") from exc

    def forget(self, workspace: str, tool: str, account: str) -> None:
        self.vault.delete(vault_path(workspace, tool, account))

    # ---------------------------------------------------------------- who and what
    def _get(self, url: str, token: str, headers: dict | None = None):
        return self._json(Request(url, headers={"Authorization": f"Bearer {token}", "Accept": "application/json",
                                                "User-Agent": "valxel-sdlc", **(headers or {})}))

    def whoami(self, tool: str, token: str, *, org: str = "") -> tuple[str, dict]:
        """(the account the connection is for, what it reaches)."""
        if tool == "jira":
            sites = self._get("https://api.atlassian.com/oauth/token/accessible-resources", token)
            sites = [s for s in sites or [] if any("jira" in sc for sc in s.get("scopes", []))] or list(sites or [])
            if not sites:
                raise OAuthError("The Atlassian account gave Valxel no Jira site; pick a site on the consent screen")
            host = urlparse(sites[0].get("url", "")).netloc or sites[0].get("name", "")
            return host, {"sites": [{"id": s.get("id"), "url": s.get("url"), "name": s.get("name")} for s in sites]}
        if tool == "lucid":
            me = self._get("https://api.lucid.co/users/me/profile", token, {"Lucid-Api-Version": "1"})
            who = me.get("email") or me.get("username") or str(me.get("id", ""))
            return who, {"user": who, "account_id": me.get("accountId") or me.get("account_id")}
        if tool == "sentry":
            got = self._get(f"https://sentry.io/api/0/organizations/{quote(org)}/", token)
            return got.get("slug") or org, {"org": got.get("slug") or org, "name": got.get("name", "")}
        if tool == "pagerduty":
            me = self._get("https://api.pagerduty.com/users/me", token,
                           {"Accept": "application/vnd.pagerduty+json;version=2"}).get("user") or {}
            sub = urlparse(me.get("html_url", "")).netloc.split(".")[0]
            if not sub:
                raise OAuthError("PagerDuty didn't say which account this is")
            return sub, {"subdomain": sub, "user": me.get("email", "")}
        raise OAuthError(f"{tool} doesn't connect through OAuth")

    def health(self, tool: str) -> Callable[[dict], str]:
        """The hourly check for one tool's connections: renew when due, then the who-am-I read."""

        def check(c: dict) -> str:
            try:
                token = self.access_token(c["workspace"], tool, c["account"])
                self.whoami(tool, token, org=(c.get("reach") or {}).get("org", ""))
            except OAuthError as exc:
                return str(exc)
            return ""

        return check


def __getattr__(name: str):
    """Names that moved to valxel_sdlc.cloud.aws.secrets (ADR 0010), still importable from here."""
    if name in ("AwsVault",):
        from .cloud.aws import secrets

        return getattr(secrets, name)
    raise AttributeError(name)

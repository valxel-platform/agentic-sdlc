"""Valxel over MCP: a person's assistant reads their inbox, features, documents and traces, starts
intents, and decides gates, as that person.

Served at /mcp (streamable HTTP) when VALXEL_MCP_CLIENT_IDS is set. Every call carries an access
token from the platform's Cognito user pool (deploy/cognito.yaml's MCP client: authorization code
with PKCE). The token has to be a person's: issued to one of the listed clients, with a `username`
and the openid scope. A client-credentials token (an agent or a service, no person behind it) is
refused outright, for reading as much as for deciding.

Each tool calls the HTTP API in-process as that person, so the API's rules apply unchanged: which
products they see, who may decide a gate, the four-hour sign-in limit for approvers. A decision
records the person as its approver and `via: mcp` (the call's session says idp mcp). An access token says nothing of a passkey, so
approving through a workspace group, which needs one, is refused here with the API's own message:
decide those in the web app. Approving many gates at once is not offered.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.request import Request, build_opener

from ..web.auth import SESSION_COOKIE, AuthConfig, SessionSigner

USERINFO_TTL = 300  # seconds a token's verified email is reused for


def default_opener() -> Any:
    return build_opener()


class NotAPerson(Exception):
    pass


@dataclass
class PersonVerifier:
    """Checks a Cognito access token and finds the person it was issued to."""

    issuer: str  # https://cognito-idp.<region>.amazonaws.com/<pool id>
    domain: str  # the pool's managed login domain, for /oauth2/userInfo
    client_ids: frozenset[str]
    opener: Any = None
    _keys: dict = field(default_factory=dict)
    _keys_at: float = 0.0
    _people: dict = field(default_factory=dict)  # sha256(token) -> (until, person)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def __post_init__(self) -> None:
        self.issuer = self.issuer.rstrip("/")
        self.domain = self.domain.removeprefix("https://").rstrip("/")
        self.opener = self.opener or default_opener()

    def _get(self, url: str, token: str = "") -> dict:
        headers = {"Accept": "application/json"} | ({"Authorization": f"Bearer {token}"} if token else {})
        with self.opener.open(Request(url, headers=headers), timeout=15) as resp:
            return json.loads(resp.read())

    def _key(self, kid: str) -> Any:
        import jwt

        with self._lock:
            if kid not in self._keys and time.time() - self._keys_at > 60:
                self._keys = {k["kid"]: k for k in self._get(f"{self.issuer}/.well-known/jwks.json").get("keys", [])}
                self._keys_at = time.time()
            if kid not in self._keys:
                raise NotAPerson("signed with a key the user pool doesn't publish")
            return jwt.PyJWK(self._keys[kid]).key

    def claims(self, token: str) -> dict:
        """The token's claims, once it is a person's access token for one of the MCP clients."""
        import jwt

        try:
            kid = jwt.get_unverified_header(token).get("kid", "")
            claims = jwt.decode(token, key=self._key(kid), algorithms=["RS256"], issuer=self.issuer,
                                options={"require": ["exp", "iat", "iss", "sub"], "verify_aud": False}, leeway=30)
        except NotAPerson:
            raise
        except Exception as exc:  # a bad signature, the wrong issuer, expired, not a JWT
            raise NotAPerson(f"the token didn't check out: {type(exc).__name__}") from exc
        if claims.get("token_use") != "access":
            raise NotAPerson("not an access token")
        if claims.get("client_id") not in self.client_ids:
            raise NotAPerson("issued to a client that isn't one of Valxel's MCP clients")
        # A person signed in: client-credentials tokens carry no username and can't hold openid.
        if not claims.get("username") or "openid" not in str(claims.get("scope", "")).split():
            raise NotAPerson("not issued to a person")
        return claims

    def person(self, token: str) -> dict:
        """{login, name, email, signed_in_at, strong} for the person the token was issued to."""
        digest = hashlib.sha256(token.encode()).hexdigest()
        now = time.time()
        with self._lock:
            hit = self._people.get(digest)
            if hit and hit[0] > now:
                return hit[1]
        claims = self.claims(token)
        info = self._get(f"https://{self.domain}/oauth2/userInfo", token)
        email = str(info.get("email") or "").strip().lower()
        if not email or str(info.get("email_verified")).lower() != "true":
            raise NotAPerson("the person's email address isn't verified")
        if str(info.get("sub") or "") != str(claims["sub"]):
            raise NotAPerson("the user pool answered for someone else")
        person = {"login": email, "email": email, "name": str(info.get("name") or email.split("@", 1)[0]),
                  # Approvers sign in again every four hours: counted from this token's sign-in.
                  "signed_in_at": int(claims.get("auth_time") or claims["iat"]),
                  # An access token carries no proof of a passkey: never two factors here.
                  "strong": False}
        with self._lock:
            self._people = {k: v for k, v in self._people.items() if v[0] > now}
            self._people[digest] = (min(now + USERINFO_TTL, float(claims["exp"])), person)
        return person


def settings_from_env(auth: AuthConfig) -> dict | None:
    """What /mcp needs, or None when it is off."""
    ids = frozenset(c.strip() for c in os.environ.get("VALXEL_MCP_CLIENT_IDS", "").split(",") if c.strip())
    if not ids:
        return None
    if not (auth.cognito_issuer and auth.cognito_domain):
        raise RuntimeError("VALXEL_MCP_CLIENT_IDS needs the Cognito user pool: set VALXEL_COGNITO_ISSUER and "
                           "VALXEL_COGNITO_DOMAIN")
    return {"issuer": auth.cognito_issuer, "domain": auth.cognito_domain, "client_ids": ids,
            "resource": f"{auth.public_url}/mcp"}


def build(app: Any, signer: SessionSigner, verifier: PersonVerifier, resource: str) -> Any:
    """The MCP server, its tools calling `app` (the API) as the token's person."""
    import httpx
    from mcp.server.auth.middleware.auth_context import get_access_token
    from mcp.server.auth.provider import AccessToken
    from mcp.server.auth.settings import AuthSettings
    from mcp.server.mcpserver import MCPServer
    from mcp.server.mcpserver.exceptions import ToolError

    class Verifier:
        async def verify_token(self, token: str) -> AccessToken | None:
            import anyio

            try:
                person = await anyio.to_thread.run_sync(verifier.person, token)
                claims = verifier.claims(token)
            except NotAPerson:
                return None
            except Exception:  # noqa: BLE001 - the user pool unreachable: no access, never an error page
                return None
            return AccessToken(token=token, client_id=str(claims["client_id"]), scopes=str(claims["scope"]).split(),
                               expires_at=int(claims["exp"]), subject=str(claims["sub"]),
                               claims={"person": person})

    server = MCPServer(
        name="valxel",
        instructions="Valxel's software delivery: your inbox of gates to decide, features and their documents, "
                     "traces, and new intents. You act as the person who signed in; Valxel's own rules decide "
                     "what they may see and decide.",
        token_verifier=Verifier(),
        # Cognito access tokens name no audience: the verifier checks the client they were issued to.
        auth=AuthSettings(issuer_url=verifier.issuer, resource_server_url=resource, required_scopes=["openid"],
                          validate_token_resource=False),
    )

    async def call(method: str, path: str, body: dict | None = None, params: dict | None = None) -> Any:
        token = get_access_token()
        person = ((token.claims or {}).get("person") if token else None)
        if not person:
            raise ToolError("Sign in first")
        now = int(time.time())
        # A session for this one call, never sent anywhere: the API then applies its own rules.
        cookie = signer.dumps({**person, "orgs": [], "teams": [], "avatar_url": "", "idp": "mcp", "exp": now + 60})
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://valxel.internal",
                                     cookies={SESSION_COOKIE: cookie}, timeout=60) as client:
            r = await client.request(method, "/api" + path, json=body, params=params)
        if r.status_code >= 400:
            try:
                detail = r.json().get("detail")
            except ValueError:
                detail = r.text[:300]
            raise ToolError(f"{r.status_code}: {detail}")
        return r.json() if r.headers.get("content-type", "").startswith("application/json") else r.text

    @server.tool()
    async def whoami() -> dict:
        """Who you are to Valxel, and the products you can see."""
        return await call("GET", "/me")

    @server.tool()
    async def inbox(mine: bool = True, product_id: str = "") -> list[dict]:
        """Gates and escalations waiting for a decision. mine=true: only those you may decide."""
        return await call("GET", "/inbox", params={"mine": str(mine).lower(), **({"product_id": product_id} if product_id else {})})

    @server.tool()
    async def features(product_id: str = "") -> list[dict]:
        """The features you can see, with where each one is."""
        return await call("GET", "/features", params={"product_id": product_id} if product_id else None)

    @server.tool()
    async def feature(feature_id: str) -> dict:
        """One feature: its phases, gates, decisions so far and what it is waiting for."""
        return await call("GET", f"/features/{feature_id}")

    @server.tool()
    async def artifact(feature_id: str, kind: str) -> Any:
        """A feature's document, e.g. prd, spec, hld, plan or release_notes."""
        return await call("GET", f"/features/{feature_id}/artifacts/{kind}")

    @server.tool()
    async def decision_brief(feature_id: str, interrupt_id: str) -> dict:
        """What a waiting gate asks you to decide, and what's known to decide it."""
        return await call("GET", f"/features/{feature_id}/decisions/{interrupt_id}/brief")

    @server.tool()
    async def decide(feature_id: str, interrupt_id: str, decision: str, feedback: str = "") -> dict:
        """Decide a gate you are an approver of, as yourself: approve, revise (with feedback) or abort.

        Ask the person first and pass their words as feedback: the decision is recorded as theirs."""
        return await call("POST", f"/features/{feature_id}/decisions/{interrupt_id}",
                          {"decision": decision, "feedback": feedback})

    @server.tool()
    async def trace(feature_id: str) -> dict:
        """From the feature's goals and criteria to its PRs, tests, deployments and incidents."""
        return await call("GET", f"/features/{feature_id}/trace")

    @server.tool()
    async def start_intent(text: str) -> dict:
        """Start describing something you want built; the intake agent answers and asks what it needs."""
        session = await call("POST", "/intake", {})
        return await call("POST", f"/intake/{session['id']}/messages", {"text": text})

    @server.tool()
    async def continue_intent(intent_id: str, text: str) -> dict:
        """Answer the intake agent on an intent you started."""
        return await call("POST", f"/intake/{intent_id}/messages", {"text": text})

    @server.tool()
    async def submit_intent(intent_id: str) -> dict:
        """Submit a finished intent: it becomes a feature in its product's pipeline."""
        return await call("POST", f"/intake/{intent_id}/submit", {})

    @server.tool()
    async def intents() -> list[dict]:
        """The intents you've started, submitted or not."""
        return await call("GET", "/intake")

    return server

"""Sign-in through an OpenID Connect provider: Google and email through Amazon Cognito.

GitHub stays on its own OAuth flow (web/auth.py): GitHub isn't a standard OpenID provider, and its
sign-in also brings the person's orgs and teams. Everything else goes through an IdentityProvider,
so the vendor behind it can change; Cognito is the first one.

Cognito's managed login offers, from one user pool (deploy/cognito.yaml):
- Google, federated through the pool;
- a one-time code by email (passwordless);
- a passkey (WebAuthn), which a person adds after signing in (`passkey_url`).

The flow is the authorization code flow with PKCE, a state and a nonce. The ID token comes straight
from the token endpoint over TLS, and its signature is still checked against the pool's published
keys, with its issuer, audience, expiry, use and nonce. Only a verified email signs anyone in: an
invitation can be accepted by email (valxel_sdlc.workspaces), so an unverified one could claim
someone else's.

How someone signed in decides what they may do: a passkey is two factors (something you have and
a biometric or PIN), and Cognito says so in the token (`amr` swk or hwk, `acr` level 3 or more).
An email code or a Google sign-in Cognito can't vouch for isn't, so a workspace's admins and group
approvers step up to a passkey before they act (api/app.py).
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Protocol
from urllib.parse import urlencode
from urllib.request import Request, build_opener

STRONG_AMR = {"swk", "hwk", "mfa"}  # a passkey (platform or roaming), or two factors
STEP_UP_ACR = "urn:cognito:loa:3"  # a passkey on its own, or a second factor


class SignInError(PermissionError):
    pass


@dataclass(frozen=True)
class Identity:
    """Who signed in, as the provider vouches for them."""

    subject: str  # the provider's stable id for the person
    email: str  # verified, lower case
    name: str = ""
    picture: str = ""
    method: str = ""  # google, email, passkey
    strong: bool = False  # two factors (a passkey): may act as an admin or group approver


class IdentityProvider(Protocol):
    name: str

    def options(self) -> list[dict]: ...
    def authorize_url(self, *, option: str, state: str, nonce: str, verifier: str, step_up: bool = False,
                      hint: str = "") -> str: ...
    def identity(self, code: str, *, verifier: str, nonce: str) -> Identity: ...


def pkce() -> tuple[str, str]:
    """A PKCE verifier and its S256 challenge."""
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    return verifier, challenge


def _b64json(part: str) -> dict:
    return json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))


@dataclass
class CognitoProvider:
    """A Cognito user pool's managed login, as an OpenID provider (VALXEL_COGNITO_*)."""

    domain: str  # the pool's managed login domain, e.g. auth.valxel.dev or <prefix>.auth.<region>.amazoncognito.com
    client_id: str
    client_secret: str
    issuer: str  # https://cognito-idp.<region>.amazonaws.com/<pool id>
    redirect_uri: str  # <public url>/api/auth/oidc/callback
    google: bool = True  # the pool federates Google
    opener: object = None
    name: str = "cognito"
    _keys: dict = field(default_factory=dict)
    _keys_at: float = 0.0
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def __post_init__(self) -> None:
        self.domain = self.domain.removeprefix("https://").rstrip("/")
        self.issuer = self.issuer.rstrip("/")
        self.opener = self.opener or build_opener()

    def options(self) -> list[dict]:
        """The sign-in buttons this provider adds."""
        out = [{"id": "google", "label": "Continue with Google"}] if self.google else []
        return out + [{"id": "email", "label": "Continue with email"}]

    def authorize_url(self, *, option: str, state: str, nonce: str, verifier: str, step_up: bool = False,
                      hint: str = "") -> str:
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
        q = {"response_type": "code", "client_id": self.client_id, "redirect_uri": self.redirect_uri,
             "scope": "openid email profile", "state": state, "nonce": nonce,
             "code_challenge": challenge, "code_challenge_method": "S256"}
        if option == "google" and self.google and not step_up:
            q["identity_provider"] = "Google"  # straight to Google, past the pool's own page
        if step_up:  # ask for a passkey, even of someone signed in another way
            q["acr_values"], q["prompt"] = STEP_UP_ACR, "login"
        if hint:
            q["login_hint"] = hint
        return f"https://{self.domain}/oauth2/authorize?{urlencode(q)}"

    def passkey_url(self) -> str:
        """Managed login's page for adding a passkey to the signed-in person's account."""
        return f"https://{self.domain}/passkeys/add?{urlencode({'client_id': self.client_id, 'redirect_uri': self.redirect_uri})}"

    def _post(self, url: str, form: dict) -> dict:
        basic = base64.b64encode(f"{self.client_id}:{self.client_secret}".encode()).decode()
        req = Request(url, data=urlencode(form).encode(), method="POST", headers={
            "Authorization": f"Basic {basic}", "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json"})
        with self.opener.open(req, timeout=15) as resp:  # type: ignore[attr-defined]
            return json.loads(resp.read())

    def _jwks(self, kid: str) -> dict:
        """The pool's signing key `kid`, fetched again when an unknown one appears (key rotation)."""
        with self._lock:
            if kid not in self._keys and time.time() - self._keys_at > 60:
                req = Request(f"{self.issuer}/.well-known/jwks.json", headers={"Accept": "application/json"})
                with self.opener.open(req, timeout=15) as resp:  # type: ignore[attr-defined]
                    self._keys = {k["kid"]: k for k in json.loads(resp.read()).get("keys", [])}
                self._keys_at = time.time()
            if kid not in self._keys:
                raise SignInError("The sign-in token was signed with a key the user pool doesn't publish")
            return self._keys[kid]

    def verify(self, id_token: str, nonce: str) -> dict:
        """The ID token's claims, once its signature and claims check out."""
        import jwt

        try:
            header = _b64json(id_token.split(".", 1)[0])
            key = jwt.PyJWK(self._jwks(header.get("kid", "")))
            claims = jwt.decode(id_token, key=key.key, algorithms=["RS256"], audience=self.client_id,
                                issuer=self.issuer, options={"require": ["exp", "iat", "iss", "aud", "sub"]},
                                leeway=30)
        except SignInError:
            raise
        except Exception as exc:  # a bad signature, the wrong audience or issuer, expired
            raise SignInError(f"The sign-in token didn't check out: {exc}") from exc
        if claims.get("token_use") != "id":
            raise SignInError("That isn't an ID token")
        if not nonce or not secrets.compare_digest(str(claims.get("nonce", "")), nonce):
            raise SignInError("The sign-in token wasn't issued for this sign-in")
        return claims

    def identity(self, code: str, *, verifier: str, nonce: str) -> Identity:
        tokens = self._post(f"https://{self.domain}/oauth2/token", {
            "grant_type": "authorization_code", "code": code, "redirect_uri": self.redirect_uri,
            "client_id": self.client_id, "code_verifier": verifier})
        claims = self.verify(tokens.get("id_token", ""), nonce)
        email = str(claims.get("email") or "").strip().lower()
        if not email or str(claims.get("email_verified")).lower() != "true":
            raise SignInError("Your email address isn't verified, so it can't sign you in")
        amr = {str(a) for a in (claims.get("amr") or [])}
        acr = str(claims.get("acr") or "")
        level = int(acr.rsplit(":", 1)[-1]) if acr.startswith("urn:cognito:loa:") and acr[-1:].isdigit() else 0
        federated = "fed" in amr or bool(claims.get("identities"))
        method = ("passkey" if amr & {"swk", "hwk"} else "google" if federated else
                  "email" if "email_otp" in amr else "")
        return Identity(subject=str(claims["sub"]), email=email, name=str(claims.get("name") or ""),
                        picture=str(claims.get("picture") or ""), method=method,
                        strong=bool(amr & STRONG_AMR) or level >= 3)

"""Sign-in through an OpenID Connect provider: Google and email through Amazon Cognito, or the
team's own identity provider (Okta, Entra ID, Google Workspace, Keycloak...) through OIDCProvider.

GitHub stays on its own OAuth flow (web/auth.py): GitHub isn't a standard OpenID provider, and its
sign-in also brings the person's orgs and teams. Everything else goes through an IdentityProvider,
so the vendor behind it can change.

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
    method: str = ""  # google, email, passkey; oidc
    strong: bool = False  # two factors (a passkey): may act as an admin or group approver
    groups: tuple[str, ...] = ()  # the identity provider's groups, when it sends them (OIDCProvider)


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


# ------------------------------------------------------------------------ any OIDC provider (T-148)

# Claims profiles (ADR 0010): what a known provider needs beyond the defaults, so a cloud's own
# sign-in is one setting (VALXEL_OIDC_PROFILE). Google (accounts.google.com, for Google Workspace
# on Google Cloud) sends email_verified and `hd`, the Workspace domain, which hosted_domain pins;
# Entra ID sends no email_verified, and the tenant's issuer vouches for its addresses.
OIDC_PROFILES: dict[str, dict[str, str]] = {
    "google": {"issuer": "https://accounts.google.com", "label": "Sign in with Google", "email_verified": "claim"},
    "entra": {"email_verified": "issuer", "groups_claim": "groups"},
}
OIDC_STRONG_AMR = {"mfa", "hwk", "swk"}  # RFC 8176: more than one factor, or a hardware or software key
ALGORITHMS = ["RS256", "RS384", "RS512", "PS256", "PS384", "PS512", "ES256", "ES384", "ES512"]


@dataclass
class OIDCProvider:
    """The team's own identity provider, found from its issuer (VALXEL_OIDC_*).

    Discovery (`<issuer>/.well-known/openid-configuration`) gives the endpoints and the keys. The flow
    is the same as Cognito's: the authorization code with PKCE, a state and a nonce, and the ID
    token's signature, issuer, audience, expiry and nonce checked. Only a verified email signs anyone
    in: `email_verified` must be true, unless `email_verified="issuer"`, for a provider that doesn't
    send the claim (Entra ID) and whose issuer is the team's own tenant, where the team controls
    every address. Two factors are read from `amr` (mfa, hwk, swk) or `acr` (strong_acr). Groups,
    when `groups_claim` names the claim, become `idp:<group>` approvers."""

    issuer: str
    client_id: str
    client_secret: str
    redirect_uri: str  # <public url>/api/auth/oidc/callback
    label: str = "Sign in with your company account"
    scopes: str = "openid email profile"
    groups_claim: str = ""
    email_verified: str = "claim"  # claim | issuer
    step_up_acr: str = ""  # asked for when someone must sign in again with two factors
    strong_acr: tuple[str, ...] = ()  # acr values that mean two factors
    hosted_domain: str = ""  # Google: only accounts of this Workspace domain (the `hd` claim)
    opener: object = None
    name: str = "oidc"
    _config: dict = field(default_factory=dict)
    _keys: dict = field(default_factory=dict)
    _keys_at: float = 0.0
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def __post_init__(self) -> None:
        self.issuer = self.issuer.rstrip("/")
        if not self.issuer.startswith("https://"):
            raise ValueError("VALXEL_OIDC_ISSUER must be https://")
        if self.email_verified not in ("claim", "issuer"):
            raise ValueError("VALXEL_OIDC_EMAIL_VERIFIED is claim or issuer")
        self.opener = self.opener or build_opener()

    def _get(self, url: str) -> dict:
        with self.opener.open(Request(url, headers={"Accept": "application/json"}), timeout=15) as resp:  # type: ignore[attr-defined]
            return json.loads(resp.read())

    def config(self) -> dict:
        """The provider's discovery document, read once; its issuer must be the one configured."""
        with self._lock:
            if not self._config:
                got = self._get(f"{self.issuer}/.well-known/openid-configuration")
                if str(got.get("issuer", "")).rstrip("/") != self.issuer:
                    raise SignInError("The identity provider's discovery document names another issuer")
                for key in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
                    if not str(got.get(key, "")).startswith("https://"):
                        raise SignInError(f"The identity provider's discovery document has no https {key}")
                self._config = got
            return self._config

    def options(self) -> list[dict]:
        return [{"id": "oidc", "label": self.label}]

    def authorize_url(self, *, option: str = "oidc", state: str, nonce: str, verifier: str, step_up: bool = False,
                      hint: str = "") -> str:
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
        q = {"response_type": "code", "client_id": self.client_id, "redirect_uri": self.redirect_uri,
             "scope": self.scopes, "state": state, "nonce": nonce,
             "code_challenge": challenge, "code_challenge_method": "S256"}
        if step_up:  # sign in again, now, and with two factors where the provider can be asked
            q["prompt"], q["max_age"] = "login", "0"
            if self.step_up_acr:
                q["acr_values"] = self.step_up_acr
        if hint:
            q["login_hint"] = hint
        if self.hosted_domain:
            q["hd"] = self.hosted_domain  # Google shows only that domain's accounts; the claim is checked too
        return f"{self.config()['authorization_endpoint']}?{urlencode(q)}"

    def _token(self, form: dict) -> dict:
        cfg = self.config()
        methods = cfg.get("token_endpoint_auth_methods_supported") or ["client_secret_basic"]
        headers = {"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"}
        if "client_secret_basic" in methods:
            from urllib.parse import quote

            pair = f"{quote(self.client_id, safe='')}:{quote(self.client_secret, safe='')}"
            headers["Authorization"] = "Basic " + base64.b64encode(pair.encode()).decode()
        else:  # client_secret_post
            form = {**form, "client_secret": self.client_secret}
        req = Request(cfg["token_endpoint"], data=urlencode(form).encode(), method="POST", headers=headers)
        with self.opener.open(req, timeout=15) as resp:  # type: ignore[attr-defined]
            return json.loads(resp.read())

    def _jwks(self, kid: str) -> dict:
        """The provider's signing key `kid`, fetched again when an unknown one appears (key rotation)."""
        with self._lock:
            stale = time.time() - self._keys_at > 60
        if kid not in self._keys and stale:
            keys = {k.get("kid", ""): k for k in self._get(self.config()["jwks_uri"]).get("keys", [])}
            with self._lock:
                self._keys, self._keys_at = keys, time.time()
        if kid not in self._keys:
            raise SignInError("The sign-in token was signed with a key the identity provider doesn't publish")
        return self._keys[kid]

    def verify(self, id_token: str, nonce: str) -> dict:
        import jwt

        try:
            header = _b64json(id_token.split(".", 1)[0])
            if header.get("alg") not in ALGORITHMS:
                raise SignInError(f"The sign-in token's algorithm {header.get('alg')!r} isn't accepted")
            key = jwt.PyJWK(self._jwks(header.get("kid", "")), algorithm=header["alg"])
            claims = jwt.decode(id_token, key=key.key, algorithms=[header["alg"]], audience=self.client_id,
                                issuer=self.issuer, options={"require": ["exp", "iat", "iss", "aud", "sub"]},
                                leeway=30)
        except SignInError:
            raise
        except Exception as exc:  # a bad signature, the wrong audience or issuer, expired
            raise SignInError(f"The sign-in token didn't check out: {exc}") from exc
        aud = claims.get("aud")
        if isinstance(aud, list) and len(aud) > 1 and claims.get("azp") != self.client_id:
            raise SignInError("The sign-in token was issued to another application")
        if not nonce or not secrets.compare_digest(str(claims.get("nonce", "")), nonce):
            raise SignInError("The sign-in token wasn't issued for this sign-in")
        return claims

    def identity(self, code: str, *, verifier: str, nonce: str) -> Identity:
        tokens = self._token({"grant_type": "authorization_code", "code": code, "redirect_uri": self.redirect_uri,
                              "client_id": self.client_id, "code_verifier": verifier})
        claims = self.verify(tokens.get("id_token", ""), nonce)
        email = str(claims.get("email") or "").strip().lower()
        verified = str(claims.get("email_verified")).lower() == "true" or self.email_verified == "issuer"
        if not email or "@" not in email or not verified:
            raise SignInError("Your email address isn't verified, so it can't sign you in")
        if self.hosted_domain and (str(claims.get("hd") or "").lower() != self.hosted_domain.lower()
                                   or not email.endswith("@" + self.hosted_domain.lower())):
            raise SignInError(f"Sign in with your {self.hosted_domain} account")
        amr = {str(a).lower() for a in (claims.get("amr") or [])}
        acr = str(claims.get("acr") or "")
        strong = bool(amr & OIDC_STRONG_AMR) or (bool(acr) and acr in set(self.strong_acr) | {self.step_up_acr} - {""})
        raw = claims.get(self.groups_claim) if self.groups_claim else None
        groups = tuple(sorted({str(g).strip() for g in (raw if isinstance(raw, list) else [])
                               if str(g).strip()}))[:200]
        return Identity(subject=str(claims["sub"]), email=email, name=str(claims.get("name") or ""),
                        picture=str(claims.get("picture") or ""), method="oidc", strong=strong, groups=groups)

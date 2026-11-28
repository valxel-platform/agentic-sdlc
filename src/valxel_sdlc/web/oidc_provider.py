"""The tool as an OpenID Connect provider, for Grafana and MLflow only (valxel_sdlc.support).

Support reaches a product's dashboards and agent traces through the tool: its Support page opens
them, and this is their only sign-in. The standard authorization-code flow, kept small:

- Registered clients only (VALXEL_OIDC_CLIENTS), each with one exact redirect URI and a secret kept
  as its SHA-256. PKCE (S256) is required of every client. Response type `code`, nothing else.
- A sign-in is for one product, chosen on the Support page: /api/support/open leaves a short-lived
  signed note of who is opening what (`valxel_support`, two minutes), and authorize honours only
  that. Opening Grafana or MLflow directly has no note, so it is refused with a pointer to the page.
- What the person may open is decided again at authorize (support.access), from the workspace as it
  is now, and recorded in the workspace's audit.
- Codes are random, single-use, kept as their hash, and expire in 60 seconds (Postgres across
  replicas). ID and access tokens are RS256 or ES256 JWTs that last 5 minutes; Grafana and
  oauth2-proxy keep their own sessions after that.

Claims: sub (login), name, preferred_username, email (verified when the sign-in gave one; otherwise a
placeholder, email_verified false), and groups, scoped to the client and product:
Grafana "<PRODUCT>/<Viewer|Editor>", MLflow "mlflow:<PRODUCT>".
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Any

CODE_TTL = 60
TOKEN_TTL = 300
NOTE_TTL = 120
NOTE_COOKIE = "valxel_support"


class OIDCError(Exception):
    """A request the provider refuses: (status, OAuth error code, description)."""

    def __init__(self, status: int, error: str, description: str) -> None:
        super().__init__(description)
        self.status, self.error, self.description = status, error, description


@dataclass(frozen=True)
class Client:
    id: str
    secret_sha256: str
    redirect_uri: str
    kind: str                  # "grafana" or "mlflow"
    product: str = ""          # mlflow: the plane's product
    plane: str = ""            # mlflow: nonprod or prod

    def check_secret(self, secret: str) -> bool:
        return hmac.compare_digest(hashlib.sha256((secret or "").encode()).hexdigest(), self.secret_sha256)


def clients_from_env(raw: str | None = None) -> dict[str, Client]:
    """VALXEL_OIDC_CLIENTS: a JSON list of {"id", "secret_sha256", "redirect_uri", "kind", and for
    MLflow "product" and "plane"}."""
    raw = os.environ.get("VALXEL_OIDC_CLIENTS", "") if raw is None else raw
    out: dict[str, Client] = {}
    for c in json.loads(raw or "[]"):
        client = Client(id=c["id"], secret_sha256=c["secret_sha256"].lower(), redirect_uri=c["redirect_uri"],
                        kind=c["kind"], product=c.get("product", ""), plane=c.get("plane", ""))
        if client.kind not in ("grafana", "mlflow"):
            raise ValueError(f"OIDC client {client.id}: kind must be grafana or mlflow")
        if client.kind == "mlflow" and not (client.product and client.plane):
            raise ValueError(f"OIDC client {client.id}: an MLflow client names its product and plane")
        if not client.redirect_uri.startswith("https://") and not client.redirect_uri.startswith("http://localhost"):
            raise ValueError(f"OIDC client {client.id}: redirect_uri must be https")
        out[client.id] = client
    return out


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


class Keys:
    """The signing key (VALXEL_OIDC_SIGNING_KEY, PEM: RSA for RS256 or P-256 for ES256) and its JWKS."""

    def __init__(self, pem: str) -> None:
        import jwt
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import ec, rsa

        self._jwt = jwt
        key = serialization.load_pem_private_key(pem.encode(), password=None)
        self.private: rsa.RSAPrivateKey | ec.EllipticCurvePrivateKey
        if isinstance(key, rsa.RSAPrivateKey):
            self.private, self.alg = key, "RS256"
            jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
        elif isinstance(key, ec.EllipticCurvePrivateKey) and key.curve.name == "secp256r1":
            self.private, self.alg = key, "ES256"
            jwk = json.loads(jwt.algorithms.ECAlgorithm.to_jwk(key.public_key()))
        else:
            raise ValueError("VALXEL_OIDC_SIGNING_KEY must be an RSA or P-256 private key")
        # RFC 7638 thumbprint as the key id, so a new key gets a new id.
        members = ("e", "kty", "n") if jwk["kty"] == "RSA" else ("crv", "kty", "x", "y")
        canonical = json.dumps({k: jwk[k] for k in members}, separators=(",", ":"), sort_keys=True)
        self.kid = _b64(hashlib.sha256(canonical.encode()).digest())
        self.public_jwk = {**jwk, "kid": self.kid, "use": "sig", "alg": self.alg}

    def sign(self, claims: dict) -> str:
        return self._jwt.encode(claims, self.private, algorithm=self.alg, headers={"kid": self.kid})

    def verify(self, token: str, audience: str, issuer: str) -> dict:
        return self._jwt.decode(token, self.private.public_key(), algorithms=[self.alg], audience=audience,
                                issuer=issuer)

    def jwks(self) -> dict:
        return {"keys": [self.public_jwk]}


class MemoryCodes:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._codes: dict[str, tuple[float, dict]] = {}

    def put(self, code: str, payload: dict) -> None:
        with self._lock:
            now = time.time()
            self._codes = {k: v for k, v in self._codes.items() if v[0] > now}
            self._codes[_hash(code)] = (now + CODE_TTL, payload)

    def take(self, code: str) -> dict | None:
        with self._lock:
            found = self._codes.pop(_hash(code), None)
        return found[1] if found and found[0] > time.time() else None


class PostgresCodes:
    """Codes for every replica: kept as their hash, taken once (delete ... returning)."""

    DDL = ("create table if not exists valxel_oidc_codes (code_sha256 text primary key, payload jsonb not null,"
           " expires timestamptz not null)",)

    def __init__(self, pool: Any) -> None:
        self.pool = pool
        with pool.connection() as conn:
            for statement in self.DDL:
                conn.execute(statement)

    def put(self, code: str, payload: dict) -> None:
        with self.pool.connection() as conn:
            conn.execute("delete from valxel_oidc_codes where expires < now()")
            conn.execute("insert into valxel_oidc_codes (code_sha256, payload, expires)"
                         " values (%s, %s, now() + make_interval(secs => %s))", (_hash(code), json.dumps(payload), CODE_TTL))

    def take(self, code: str) -> dict | None:
        with self.pool.connection() as conn:
            row = conn.execute("delete from valxel_oidc_codes where code_sha256 = %s and expires > now()"
                               " returning payload", (_hash(code),)).fetchone()
        if not row:
            return None
        value = row[0] if not isinstance(row, dict) else row["payload"]
        return value if isinstance(value, dict) else json.loads(value)


def _hash(code: str) -> str:
    return hashlib.sha256(code.encode()).hexdigest()


def _s256(verifier: str) -> str:
    return _b64(hashlib.sha256(verifier.encode()).digest())


@dataclass
class Provider:
    issuer: str
    keys: Keys
    clients: dict[str, Client]
    codes: Any = field(default_factory=MemoryCodes)

    def discovery(self) -> dict:
        return {
            "issuer": self.issuer,
            "authorization_endpoint": f"{self.issuer}/authorize",
            "token_endpoint": f"{self.issuer}/token",
            "userinfo_endpoint": f"{self.issuer}/userinfo",
            "jwks_uri": f"{self.issuer}/jwks",
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code"],
            "subject_types_supported": ["public"],
            "id_token_signing_alg_values_supported": [self.keys.alg],
            "token_endpoint_auth_methods_supported": ["client_secret_basic", "client_secret_post"],
            "code_challenge_methods_supported": ["S256"],
            "scopes_supported": ["openid", "profile", "email", "groups"],
            "claims_supported": ["sub", "name", "preferred_username", "email", "email_verified", "groups"],
        }

    def check_authorize(self, q: dict) -> Client:
        """The request's client, if it is one we know asking the way we allow; raises otherwise.
        Before this passes, nothing is sent back to the redirect URI."""
        client = self.clients.get(q.get("client_id", ""))
        if client is None:
            raise OIDCError(400, "invalid_client", "Unknown client")
        if q.get("redirect_uri") != client.redirect_uri:
            raise OIDCError(400, "invalid_request", "redirect_uri isn't the one registered for this client")
        if q.get("response_type") != "code":
            raise OIDCError(400, "unsupported_response_type", "Only response_type=code")
        if "openid" not in (q.get("scope") or "").split():
            raise OIDCError(400, "invalid_scope", "scope must include openid")
        if q.get("code_challenge_method") != "S256" or len(q.get("code_challenge") or "") < 43:
            raise OIDCError(400, "invalid_request", "PKCE with S256 is required")
        return client

    def issue_code(self, client: Client, q: dict, claims: dict) -> str:
        code = secrets.token_urlsafe(32)
        self.codes.put(code, {"client": client.id, "redirect_uri": client.redirect_uri, "nonce": q.get("nonce", ""),
                              "challenge": q["code_challenge"], "claims": claims, "at": int(time.time())})
        return code

    def token(self, form: dict, basic: tuple[str, str] | None) -> dict:
        client_id, secret = basic or (form.get("client_id", ""), form.get("client_secret", ""))
        client = self.clients.get(client_id)
        if client is None or not client.check_secret(secret):
            raise OIDCError(401, "invalid_client", "Client authentication failed")
        if form.get("grant_type") != "authorization_code":
            raise OIDCError(400, "unsupported_grant_type", "Only authorization_code")
        held = self.codes.take(form.get("code", ""))
        if held is None or held["client"] != client.id:
            raise OIDCError(400, "invalid_grant", "The code is unknown, used or expired")
        if form.get("redirect_uri") != held["redirect_uri"]:
            raise OIDCError(400, "invalid_grant", "redirect_uri doesn't match the authorization request")
        if not hmac.compare_digest(_s256(form.get("code_verifier", "")), held["challenge"]):
            raise OIDCError(400, "invalid_grant", "code_verifier doesn't match")
        now = int(time.time())
        base = {"iss": self.issuer, "sub": held["claims"]["sub"], "iat": now, "exp": now + TOKEN_TTL}
        id_token = self.keys.sign({**base, **held["claims"], "aud": client.id, "auth_time": held["at"],
                                   **({"nonce": held["nonce"]} if held["nonce"] else {})})
        access = self.keys.sign({**base, **held["claims"], "aud": f"{self.issuer}/userinfo", "client_id": client.id})
        return {"access_token": access, "token_type": "Bearer", "expires_in": TOKEN_TTL, "id_token": id_token,
                "scope": "openid profile email groups"}

    def userinfo(self, bearer: str) -> dict:
        try:
            claims = self.keys.verify(bearer, audience=f"{self.issuer}/userinfo", issuer=self.issuer)
        except Exception as exc:  # noqa: BLE001 - any bad token is the same answer
            raise OIDCError(401, "invalid_token", "The access token is invalid or expired") from exc
        keep = ("sub", "name", "preferred_username", "email", "email_verified", "groups")
        return {k: claims[k] for k in keep if k in claims}


def claims_for(user, client: Client, product: str, grafana_role: str) -> dict:
    """What the client learns about the person, for this one product."""
    email = getattr(user, "email", "") or ""
    groups = [f"{product}/{grafana_role}"] if client.kind == "grafana" else [f"mlflow:{product}"]
    return {"sub": user.login, "preferred_username": user.login, "name": getattr(user, "name", "") or user.login,
            "email": email or f"{user.login}@users.valxel.invalid", "email_verified": bool(email), "groups": groups}


def provider_from_env() -> Provider | None:
    """The provider, when this installation gives Grafana or MLflow their sign-in: VALXEL_OIDC_ISSUER
    (this tool's public URL + /api/oidc), VALXEL_OIDC_SIGNING_KEY and VALXEL_OIDC_CLIENTS."""
    issuer, pem = os.environ.get("VALXEL_OIDC_ISSUER", "").rstrip("/"), os.environ.get("VALXEL_OIDC_SIGNING_KEY", "")
    if not (issuer and pem):
        return None
    return Provider(issuer=issuer, keys=Keys(pem), clients=clients_from_env())

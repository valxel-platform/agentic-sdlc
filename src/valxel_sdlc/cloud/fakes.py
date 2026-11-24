"""In-memory stand-ins for every port (ADR 0004, ADR 0010): what tests and laptops run on, and the
reference each cloud's adapters are measured against (`conformance.py`)."""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta


class MemorySecrets:
    def __init__(self, values: dict[str, list[str]] | None = None) -> None:
        self.values = {k: list(v) for k, v in (values or {}).items()}

    def versions(self, name: str) -> list[str]:
        return list(self.values.get(name, []))[:2]

    def rotate(self, name: str, value: str) -> None:  # how a cloud's rotation leaves it: new, then old
        self.values[name] = [value, *self.values.get(name, [])][:2]


class MemorySigner:
    """RS256 with a key made on the spot; `public_pem` is what a verifier checks against."""

    def __init__(self) -> None:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa

        self._key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.public_pem = self._key.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()

    def sign(self, message: bytes) -> bytes:
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding

        return self._key.sign(message, padding.PKCS1v15(), hashes.SHA256())


class MemoryAnchors:
    def __init__(self) -> None:
        self.by: dict[str, list[dict]] = {}

    def put(self, chain: str, h: dict) -> None:
        self.by.setdefault(chain, []).append({**h, "at": datetime.now(UTC).isoformat()})

    def list(self, chain: str, limit: int = 0) -> list[dict]:
        rows = self.by.get(chain, [])
        return rows[-limit:] if limit else list(rows)


class MemoryImmutable:
    def __init__(self, clock=lambda: datetime.now(UTC)) -> None:
        self.objects: dict[str, tuple[bytes, datetime]] = {}
        self._clock = clock

    def put(self, key: str, data: bytes, *, retain_days: int, content_type: str = "application/octet-stream",
            encoding: str = "", meta: dict[str, str] | None = None, scope: dict[str, str] | None = None) -> None:
        if retain_days < 1:
            raise ValueError("retain_days must be at least 1")
        if key in self.objects:
            raise FileExistsError(f"{key} is already written, and is locked until its retention ends")
        self.objects[key] = (bytes(data), self._clock() + timedelta(days=retain_days))

    def get(self, key: str) -> bytes:
        return self.objects[key][0]

    def keys(self, prefix: str = "") -> list[str]:
        return sorted(k for k in self.objects if k.startswith(prefix))

    def retain_until(self, key: str) -> datetime:
        return self.objects[key][1]


class FakeIdentity:
    """Tokens are `fake.<base64 json claims>`; only this issuer's are valid."""

    def __init__(self, issuer: str = "https://issuer.invalid") -> None:
        self.issuer = issuer

    def token(self, **claims) -> str:
        body = {"iss": self.issuer, **claims}
        return "fake." + base64.urlsafe_b64encode(json.dumps(body).encode()).decode()

    def claims(self, id_token: str) -> dict:
        head, _, body = id_token.partition(".")
        if head != "fake" or not body:
            raise ValueError("not a token from this issuer")
        try:
            claims = json.loads(base64.urlsafe_b64decode(body.encode()))
        except ValueError as exc:
            raise ValueError("the token's claims don't parse") from exc
        if claims.get("iss") != self.issuer:
            raise ValueError(f"issued by {claims.get('iss')!r}, not {self.issuer!r}")
        return claims

    def user(self, claims: dict) -> dict:
        email = str(claims.get("email") or "")
        login = claims.get("preferred_username") or email.partition("@")[0] or claims.get("sub") or ""
        return {"login": str(login), "email": email, "name": str(claims.get("name") or ""),
                "groups": [str(g) for g in claims.get("groups") or []]}


class FakeAccounts:
    """Accounts that answer: `connected` maps (workspace, account) to the external ID the roles trust."""

    JOBS = ("deploy", "agents")

    def __init__(self, connected: dict[tuple[str, str], str] | None = None) -> None:
        self.connected = dict(connected or {})

    def roles(self, workspace: str, account: str) -> dict[str, str]:
        return {job: f"fake://{account}/valxel-{workspace}-{job}" for job in self.JOBS}

    def check(self, workspace: str, *, account: str, external_id: str) -> dict:
        from ..connections import ConnectionError_

        if self.connected.get((workspace, account)) != external_id:
            raise ConnectionError_(f"Valxel can't reach {account} for {workspace}: not connected, "
                                   "or another external ID")
        return {"account_id": account, **{f"{j}_role": r for j, r in self.roles(workspace, account).items()}}


class FakeCosts:
    """Daily rows in; the four CostSource answers out. `cloud` rows: {day, account, product_id,
    feature_id, service, usd}; `compute` rows: {day, namespace, project, env, component, usd}."""

    def __init__(self, cloud: list[dict] | None = None, compute: list[dict] | None = None) -> None:
        self.cloud, self.compute = list(cloud or []), list(compute or [])

    @staticmethod
    def _in(row: dict, start: datetime, end: datetime) -> bool:
        return start.date().isoformat() <= row["day"] < end.date().isoformat()

    def component_costs(self, *, namespace, project, env, components, start, end):
        out = dict.fromkeys(components, 0.0)
        for r in self.compute:
            if (r["namespace"], r["project"], r["env"]) == (namespace, project, env) and r["component"] in out \
                    and self._in(r, start, end):
                out[r["component"]] += float(r["usd"])
        return out

    def aws_costs(self, *, product_id, feature_id, account, start, end):
        out: dict[str, float] = {}
        for r in self.cloud:
            mine = (r["account"], r["product_id"], r["feature_id"]) == (account, product_id, feature_id)
            if mine and self._in(r, start, end):
                out[r["service"]] = out.get(r["service"], 0.0) + float(r["usd"])
        return out

    def component_costs_daily(self, *, namespace, project, env, start, end):
        return [{"day": r["day"], "component": r["component"], "usd": float(r["usd"])} for r in self.compute
                if (r["namespace"], r["project"], r["env"]) == (namespace, project, env) and self._in(r, start, end)]

    def aws_costs_daily(self, *, product_id, account, start, end):
        return [{"day": r["day"], "service": r["service"], "feature_id": r["feature_id"], "usd": float(r["usd"])}
                for r in self.cloud
                if (r["account"], r["product_id"]) == (account, product_id) and self._in(r, start, end)]


class FakeInventory:
    def __init__(self, items: list[dict] | None = None) -> None:
        self.items = list(items or [])  # {account, id, type, labels}

    def resources(self, *, account: str, product_id: str) -> list[dict]:
        return [{"id": i["id"], "type": i["type"], "feature_id": i["labels"].get("valxel_feature", ""),
                 "labels": dict(i["labels"])}
                for i in self.items if i["account"] == account and i["labels"].get("valxel_product") == product_id]


class FakeSignals:
    """Events `{"kind": "fake-alarm", "product", "env", "name", "firing": bool}`."""

    kind = "fake-alarm"

    def parse(self, payload: dict) -> list[dict]:
        if payload.get("kind") != self.kind:
            return []
        return [{"source": self.kind, "category": "cloud", "status": "firing" if payload.get("firing") else "resolved",
                 "product_id": str(payload.get("product") or ""), "component": "",
                 "env": str(payload.get("env") or "production"),
                 "severity": "sev3", "title": str(payload.get("name") or ""), "detail": "",
                 "fingerprint": f"{self.kind}:{payload.get('name')}", "url": "", "at": datetime.now(UTC).isoformat(),
                 "labels": {}}]

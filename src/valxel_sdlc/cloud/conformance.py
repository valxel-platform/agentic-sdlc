"""What every adapter of a port must do (ADR 0010). Each check takes an implementation, and
whatever it needs to set up or observe its cloud, and returns what it got wrong ([] if nothing).

tests/test_cloud_ports.py runs them against the fakes; a cloud's own tests run them against its
adapters, recorded (cassettes) in CI and live in that cloud's sandbox every night.
"""

from __future__ import annotations

import base64
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from . import ports

SIGNAL_KEYS = {"source", "category", "status", "product_id", "component", "env", "severity", "title", "detail",
               "fingerprint", "url", "at", "labels"}


def _shape(impl, port) -> list[str]:
    return [] if isinstance(impl, port) else [f"{type(impl).__name__} doesn't have {port.__name__}'s methods"]


def secret_store(store, *, set_value: Callable[[str, str], None], name: str = "VALXEL_CONFORMANCE_SECRET") -> list[str]:
    """`set_value(name, value)` writes a new version as the cloud's own rotation would."""
    out = _shape(store, ports.SecretStore)
    if store.versions(name + "_ABSENT") != []:
        out.append("versions() of a secret that isn't set must be []")
    set_value(name, "first")
    if store.versions(name)[:1] != ["first"]:
        out.append("after one version, versions() must start with it")
    set_value(name, "second")
    if store.versions(name) != ["second", "first"]:
        out.append(f"after a rotation, versions() must be [current, previous], got {store.versions(name)}")
    return out


def signer(sig, *, public_pem: str) -> list[str]:
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding, rsa

    out = _shape(sig, ports.Signer)
    key = serialization.load_pem_public_key(public_pem.encode())
    if not isinstance(key, rsa.RSAPublicKey):
        return [*out, "the public key is not an RSA key: RS256 needs one"]
    msg = b"valxel.conformance." + base64.b64encode(str(time.time()).encode())
    try:
        key.verify(sig.sign(msg), msg, padding.PKCS1v15(), hashes.SHA256())
    except InvalidSignature:
        out.append("the signature doesn't verify as RS256 (PKCS#1 v1.5, SHA-256) with the public key")
    return out


def anchor_store(store, chain: str = "conformance") -> list[str]:
    out = _shape(store, ports.AnchorStore)
    for n in (1, 2, 3):
        store.put(chain, {"count": n, "hash": f"h{n}"})
    got = [a["count"] for a in store.list(chain)]
    if got[-3:] != [1, 2, 3]:
        out.append(f"list() must return anchors oldest first, got {got}")
    if [a["count"] for a in store.list(chain, limit=1)] != [3]:
        out.append("list(limit=1) must return only the newest anchor")
    return out


def immutable_store(store, *, prefix: str = "conformance/") -> list[str]:
    out = _shape(store, ports.ImmutableStore)
    key = f"{prefix}{datetime.now(UTC).strftime('%Y%m%dT%H%M%S%f')}"
    store.put(key, b"anchored", retain_days=1)
    if store.get(key) != b"anchored":
        out.append("get() must return what put() wrote")
    try:
        store.put(key, b"rewritten", retain_days=1)
        out.append("a second put() of the same key must raise: objects are written once")
    except Exception:  # noqa: BLE001 - any refusal will do; what matters is that it refuses
        if store.get(key) != b"anchored":
            out.append("a refused rewrite must leave the object as it was")
    if key not in store.keys(prefix):
        out.append("keys(prefix) must list what put() wrote")
    try:
        store.get(prefix + "absent")
        out.append("get() of a key that isn't there must raise KeyError")
    except KeyError:
        pass
    return out


def identity_provider(idp, *, good_token: str, foreign_token: str) -> list[str]:
    out = _shape(idp, ports.IdentityProvider)
    claims = idp.claims(good_token)
    if claims.get("iss") != idp.issuer:
        out.append("claims() must return the token's own issuer")
    user = idp.user(claims)
    if not ({"login", "email", "name", "groups"} <= set(user) and isinstance(user["groups"], list)):
        out.append("user() must return login, email, name and a list of groups")
    for bad in (foreign_token, "not-a-token"):
        try:
            idp.claims(bad)
            out.append(f"claims() must refuse {bad[:20]!r}")
        except ValueError:
            pass
    return out


def cloud_accounts(accounts, *, workspace: str, account: str, external_id: str) -> list[str]:
    from ..connections import ConnectionError_

    out = _shape(accounts, ports.CloudAccounts)
    roles = accounts.roles(workspace, account)
    if not {"deploy", "agents"} <= set(roles):
        out.append("roles() must name at least the deploy and agents jobs")
    got = accounts.check(workspace, account=account, external_id=external_id)
    if str(got.get("account_id")) != account:
        out.append("check() must answer with the account it reached")
    try:
        accounts.check(workspace, account=account, external_id=external_id + "-wrong")
        out.append("check() with the wrong external ID must raise ConnectionError_")
    except ConnectionError_:
        pass
    return out


def cost_source(src, *, product_id: str, feature_id: str, account: str, day: str,
                expect: dict[str, float]) -> list[str]:
    """`expect`: service -> USD the source holds for the feature on `day`."""
    out = _shape(src, ports.CostSource)
    start = datetime.fromisoformat(day).replace(tzinfo=UTC)
    end = start + timedelta(days=1)
    got = src.aws_costs(product_id=product_id, feature_id=feature_id, account=account, start=start, end=end)
    if {k: round(v, 2) for k, v in got.items()} != {k: round(v, 2) for k, v in expect.items()}:
        out.append(f"the feature's costs must be {expect}, got {got}")
    daily = src.aws_costs_daily(product_id=product_id, account=account, start=start, end=end)
    if any(not {"day", "service", "feature_id", "usd"} <= set(r) for r in daily):
        out.append("daily rows must carry day, service, feature_id and usd")
    if round(sum(r["usd"] for r in daily if r["feature_id"] == feature_id), 2) != round(sum(expect.values()), 2):
        out.append("the daily rows must add up to the feature's costs")
    return out


def resource_inventory(inv, *, account: str, product_id: str, expect_ids: set[str]) -> list[str]:
    out = _shape(inv, ports.ResourceInventory)
    rows = inv.resources(account=account, product_id=product_id)
    if {r["id"] for r in rows} != expect_ids:
        out.append(f"resources() must be exactly {sorted(expect_ids)}, got {sorted(r['id'] for r in rows)}")
    if any(not {"id", "type", "feature_id", "labels"} <= set(r) for r in rows):
        out.append("each resource must carry id, type, feature_id and labels")
    return out


def signal_source(src, *, firing: dict, resolved: dict, unrelated: dict) -> list[str]:
    out = _shape(src, ports.SignalSource)
    if src.parse(unrelated) != []:
        out.append("parse() of another kind of event must be []")
    for payload, status in ((firing, "firing"), (resolved, "resolved")):
        got = src.parse(payload)
        if len(got) != 1 or got[0].get("status") != status:
            out.append(f"parse() must give one {status} signal")
        elif not set(got[0]) >= SIGNAL_KEYS:
            out.append(f"a signal must carry {sorted(SIGNAL_KEYS - set(got[0]))}")
    fired, cleared = src.parse(firing), src.parse(resolved)
    if fired and cleared and fired[0]["fingerprint"] != cleared[0]["fingerprint"]:
        out.append("firing and resolved must share a fingerprint, so one resolves the other")
    return out

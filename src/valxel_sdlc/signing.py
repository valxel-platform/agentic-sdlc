"""Signing with a key the tool may never hold: the GitHub App's JWTs.

A `Signer` signs bytes with RSASSA-PKCS1-v1_5 and SHA-256 (RS256, what GitHub requires):

    KmsSigner     AWS KMS, with the App key imported as a sign-only key: the key never leaves KMS,
                  so nothing that reads the tool's memory or files can take it.
    RsaKeySigner  a PEM key in memory, for laptops and small self-hosted installs.

Google Cloud KMS, Azure Key Vault and Vault's Transit engine sign RS256 the same way: each is
another `Signer` with `sign(message)`.

`github_app_signer(app=...)` picks one for each of the two GitHub Apps (valxel_sdlc.provision_queue):

    delivery     VALXEL_GITHUB_APP_ID with VALXEL_GITHUB_APP_KMS_KEY_ID (a KMS key id or ARN), else the
                 PEM VALXEL_GITHUB_APP_PRIVATE_KEY in the secret store, else the file
                 VALXEL_GITHUB_APP_PRIVATE_KEY_FILE
    provisioner  VALXEL_GITHUB_PROVISIONER_APP_ID with VALXEL_GITHUB_PROVISIONER_APP_KMS_KEY_ID, else
                 VALXEL_GITHUB_PROVISIONER_PRIVATE_KEY, else VALXEL_GITHUB_PROVISIONER_PRIVATE_KEY_FILE
"""

from __future__ import annotations

import base64
import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Protocol


class Signer(Protocol):
    def sign(self, message: bytes) -> bytes: ...


class RsaKeySigner:
    def __init__(self, pem: str) -> None:
        from cryptography.hazmat.primitives import (
            serialization,  # pip install .[github]
        )

        self._key = serialization.load_pem_private_key(pem.encode(), password=None)

    def sign(self, message: bytes) -> bytes:
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding

        return self._key.sign(message, padding.PKCS1v15(), hashes.SHA256())


class KmsSigner:
    def __init__(self, key_id: str, *, client=None, region: str = "") -> None:
        if client is None:
            import boto3  # pip install .[aws]

            client = boto3.client("kms", **({"region_name": region} if region else {}))
        self.key_id, self.client = key_id, client

    def sign(self, message: bytes) -> bytes:
        got = self.client.sign(KeyId=self.key_id, Message=message, MessageType="RAW",
                               SigningAlgorithm="RSASSA_PKCS1_V1_5_SHA_256")
        return got["Signature"]


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def jwt_rs256(claims: dict, signer: Signer) -> str:
    """A compact RS256 JWT, signed by `signer`."""
    head = _b64(json.dumps({"alg": "RS256", "typ": "JWT"}, separators=(",", ":")).encode())
    body = _b64(json.dumps(claims, separators=(",", ":")).encode())
    signing_input = f"{head}.{body}".encode()
    return f"{head}.{body}.{_b64(signer.sign(signing_input))}"


# The two GitHub Apps (valxel_sdlc.provision_queue): their settings' names.
APPS = {
    "delivery": "VALXEL_GITHUB_APP",
    "provisioner": "VALXEL_GITHUB_PROVISIONER_APP",
}


def _names(app: str) -> dict:
    base = APPS[app]
    key = base.replace("_APP", "") if app == "provisioner" else "VALXEL_GITHUB_APP"
    return {"id": f"{base}_ID", "kms": f"{base}_KMS_KEY_ID", "pem": f"{key}_PRIVATE_KEY",
            "file": f"{key}_PRIVATE_KEY_FILE"}


def github_app_configured(env: Mapping[str, str] | None = None, *, app: str = "delivery") -> bool:
    from .secret_ring import current

    env = env if env is not None else os.environ
    n = _names(app)
    return bool(env.get(n["id"])) and bool(env.get(n["kms"]) or env.get(n["file"]) or current(n["pem"], env))


def github_app_signer(env: Mapping[str, str] | None = None, *, app: str = "delivery") -> Signer:
    from .secret_ring import current

    env = env if env is not None else os.environ
    n = _names(app)
    key_id = env.get(n["kms"], "")
    if key_id:
        return KmsSigner(key_id, region=env.get("AWS_REGION", ""))
    pem = current(n["pem"], env)
    if not pem and env.get(n["file"]):
        pem = Path(env[n["file"]]).read_text()
    if not pem:
        raise RuntimeError(f"The {app} GitHub App needs {n['kms']}, {n['pem']} in the secret store, or {n['file']}")
    return RsaKeySigner(pem)


def github_app_tokens(*, api: str, permissions: dict[str, str] | None = None, env: Mapping[str, str] | None = None,
                      app: str = "delivery"):
    """A GitHub App's token provider, signing with whichever signer is configured for it."""
    from .governance.sources import GitHubAppTokenProvider

    env = env if env is not None else os.environ
    return GitHubAppTokenProvider(env[_names(app)["id"]], github_app_signer(env, app=app), api=api,
                                  permissions=permissions)


def provisioner_key_reachable(env: Mapping[str, str] | None = None) -> list[str]:
    """Settings through which this process could sign for the Provisioning App."""
    from .secret_ring import current

    env = env if env is not None else os.environ
    n = _names("provisioner")
    out = [k for k in (n["kms"], n["file"]) if env.get(k)]
    try:
        if current(n["pem"], env):
            out.append(n["pem"])
    except Exception:
        pass  # the store refuses this process: as it should
    return out

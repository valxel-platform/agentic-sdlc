"""Azure Key Vault: the tool's secrets (secret_ring) and tool connection tokens
(oauth_connections).

Secret names allow only letters, digits and `-`, so the tool's paths are spelled with `--` for
`/`, `-` for `_` and `-x<hex>-` for any other character (`valxel/platform/VALXEL_X` is
`valxel--platform--VALXEL-X`).

Key Vault has no previous stage. The current value is the newest enabled version and the previous
one the enabled version before it: rotating is setting a new version, and finishing a rotation is
disabling the old one. The vault keeps soft delete and purge protection on, so a deleted
connection's token stays recoverable for the vault's retention, then goes; a connection made again
under the same name recovers it and writes a new version over it."""

from __future__ import annotations

import json
import re
import threading
import time
from collections.abc import Callable
from typing import Any

from ...secret_ring import secret_id
from . import credential, not_found


def kv_name(path: str) -> str:
    out = path.replace("/", "--").replace("_", "-")
    return re.sub(r"[^A-Za-z0-9-]", lambda m: f"-x{ord(m.group()):02x}-", out)[:127]


def _client(vault_url: str, client: Any) -> Any:
    if client is None:
        from azure.keyvault.secrets import SecretClient  # the 'azure' extra

        client = SecretClient(vault_url=vault_url, credential=credential())
    return client


class AzureSecretStore:
    """The newest two enabled versions of `secret_id(name)`, cached for `ttl` seconds."""

    def __init__(self, *, vault_url: str, prefix: str = "valxel", client: Any = None, ttl: float = 60.0,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.client, self.prefix, self.ttl, self.clock = _client(vault_url, client), prefix, ttl, clock
        self._cache: dict[str, tuple[float, list[str]]] = {}
        self._lock = threading.Lock()

    def _versions(self, name: str) -> list[str]:
        kv = kv_name(secret_id(name, self.prefix))
        try:
            found = [v for v in self.client.list_properties_of_secret_versions(kv) if v.enabled]
        except Exception as exc:
            if not_found(exc):
                return []
            raise
        found.sort(key=lambda v: v.created_on, reverse=True)
        return [self.client.get_secret(kv, v.version).value for v in found[:2]]

    def versions(self, name: str) -> list[str]:
        now = self.clock()
        with self._lock:
            hit = self._cache.get(name)
            if hit and hit[0] > now:
                return list(hit[1])
        got = self._versions(name)
        with self._lock:
            self._cache[name] = (now + self.ttl, got)
        return list(got)


class AzureVault:
    """Connection tokens as secrets tagged valxel-kind=connection-token."""

    def __init__(self, *, vault_url: str, client: Any = None) -> None:
        self.client = _client(vault_url, client)

    def get(self, path: str) -> dict | None:
        try:
            got = self.client.get_secret(kv_name(path))
        except Exception as exc:
            if not_found(exc):
                return None
            raise
        return json.loads(got.value or "null")

    def put(self, path: str, value: dict) -> None:
        name = kv_name(path)
        try:
            self.client.set_secret(name, json.dumps(value), tags={"valxel-kind": "connection-token"},
                                   content_type="application/json")
        except Exception as exc:
            if "ObjectIsDeletedButRecoverable" not in str(exc):
                raise
            self.client.begin_recover_deleted_secret(name).wait()
            self.client.set_secret(name, json.dumps(value), tags={"valxel-kind": "connection-token"},
                                   content_type="application/json")

    def delete(self, path: str) -> None:
        try:
            self.client.begin_delete_secret(kv_name(path)).wait()
        except Exception as exc:
            if not not_found(exc):
                raise

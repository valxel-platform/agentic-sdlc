"""Google Secret Manager: the tool's secrets (secret_ring) and tool connection tokens
(oauth_connections).

Secret IDs allow only letters, digits, `_` and `-`, so the tool's paths are spelled with `__` for
`/` (`valxel/platform/NAME` is `valxel__platform__NAME`), and any other character as `_<hex>_`.

Secret Manager has no AWSPREVIOUS stage. The current value is the newest enabled version and the
previous one the enabled version before it: rotating is adding a version, and finishing a rotation
is disabling the old one."""

from __future__ import annotations

import json
import re
import threading
import time
from collections.abc import Callable
from typing import Any

from ...secret_ring import secret_id
from . import not_found


def gcp_id(path: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", lambda m: f"_{ord(m.group()):02x}_", path.replace("/", "__"))[:255]


def _client(client: Any) -> Any:
    if client is None:
        from google.cloud import secretmanager  # the 'gcp' extra

        client = secretmanager.SecretManagerServiceClient()
    return client


class GcpSecretStore:
    """The newest two enabled versions of `secret_id(name)`, cached for `ttl` seconds."""

    def __init__(self, *, project: str, prefix: str = "valxel", client: Any = None, ttl: float = 60.0,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.client, self.project, self.prefix, self.ttl, self.clock = _client(client), project, prefix, ttl, clock
        self._cache: dict[str, tuple[float, list[str]]] = {}
        self._lock = threading.Lock()

    def _versions(self, name: str) -> list[str]:
        parent = f"projects/{self.project}/secrets/{gcp_id(secret_id(name, self.prefix))}"
        try:
            found = list(self.client.list_secret_versions(request={"parent": parent, "filter": "state:ENABLED"}))
        except Exception as exc:
            if not_found(exc):
                return []
            raise
        found.sort(key=lambda v: v.create_time, reverse=True)
        return [self.client.access_secret_version(request={"name": v.name}).payload.data.decode() for v in found[:2]]

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


class GcpVault:
    """Connection tokens as secrets labelled valxel-kind=connection-token; a deleted connection's
    secret is deleted with all its versions."""

    def __init__(self, *, project: str, client: Any = None) -> None:
        self.client, self.project = _client(client), project

    def _name(self, path: str) -> str:
        return f"projects/{self.project}/secrets/{gcp_id(path)}"

    def get(self, path: str) -> dict | None:
        try:
            got = self.client.access_secret_version(request={"name": f"{self._name(path)}/versions/latest"})
        except Exception as exc:
            if not_found(exc):
                return None
            raise
        return json.loads(got.payload.data.decode() or "null")

    def put(self, path: str, value: dict) -> None:
        payload = {"data": json.dumps(value).encode()}
        try:
            self.client.add_secret_version(request={"parent": self._name(path), "payload": payload})
        except Exception as exc:
            if not not_found(exc):
                raise
            self.client.create_secret(request={
                "parent": f"projects/{self.project}", "secret_id": gcp_id(path),
                "secret": {"replication": {"automatic": {}}, "labels": {"valxel-kind": "connection-token"}}})
            self.client.add_secret_version(request={"parent": self._name(path), "payload": payload})

    def delete(self, path: str) -> None:
        try:
            self.client.delete_secret(request={"name": self._name(path)})
        except Exception as exc:
            if not not_found(exc):
                raise

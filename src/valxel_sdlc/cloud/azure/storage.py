"""Azure Blob Storage with version-level immutability: the ImmutableStore behind the audit anchors
and the checkpoint archive (tracker CP-402).

The container has version-level immutability support, so each blob is written with its own
time-based retention policy in Locked mode: no one can shorten it, or change or delete the blob,
until it ends, the subscription owner included. Written once: the upload asks that no blob of
that name exist (`overwrite=False`). Encryption is the storage account's customer-managed key.

An object's scope goes in blob index tags (`valxel-workspace`...), which Azure ABAC role
conditions match, as S3 tags are on AWS: a reader's role assignment can be limited to its own
workspace's objects. Metadata names allow only letters, digits and `_`."""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from typing import Any

from . import credential, exists, not_found


def _settings(content_type: str, encoding: str, until: datetime) -> tuple[Any, Any]:
    """The SDK's ContentSettings and ImmutabilityPolicy; plain records where it isn't installed (a
    fake client in tests: the real client needs the SDK anyway)."""
    try:
        from azure.storage.blob import ContentSettings, ImmutabilityPolicy  # the 'azure' extra
    except ImportError:
        from types import SimpleNamespace as ContentSettings  # type: ignore[assignment]
        from types import SimpleNamespace as ImmutabilityPolicy  # type: ignore[assignment]
    return (ContentSettings(content_type=content_type, content_encoding=encoding or None),
            ImmutabilityPolicy(expiry_time=until, policy_mode="Locked"))


def _meta_key(k: str) -> str:
    out = "".join(c if c.isalnum() or c == "_" else "_" for c in k)
    return out if out[:1].isalpha() else f"m_{out}"


class AzureBlobImmutable:
    def __init__(self, container: str, *, account_url: str = "", client: Any = None, clock=time.time) -> None:
        self.container, self.account_url, self._client, self.clock = container, account_url, client, clock

    @property
    def client(self) -> Any:
        """The container's client."""
        if self._client is None:
            from azure.storage.blob import ContainerClient  # the 'azure' extra

            self._client = ContainerClient(self.account_url, self.container, credential=credential())
        return self._client

    @property
    def where(self) -> str:
        return f"{self.account_url.rstrip('/')}/{self.container}"

    def put(self, key: str, data: bytes, *, retain_days: int, content_type: str = "application/octet-stream",
            encoding: str = "", meta: dict[str, str] | None = None, scope: dict[str, str] | None = None) -> None:
        until = datetime.fromtimestamp(self.clock(), UTC) + timedelta(days=retain_days)
        settings, policy = _settings(content_type, encoding, until)
        try:
            self.client.upload_blob(
                key, data, overwrite=False, content_settings=settings,
                metadata={_meta_key(k): v for k, v in (meta or {}).items()} or None,
                tags={f"valxel-{k}": v for k, v in (scope or {}).items()} or None,
                immutability_policy=policy)
        except Exception as exc:
            if exists(exc):
                raise FileExistsError(f"{self.where}/{key} is already written") from exc
            raise

    def get(self, key: str) -> bytes:
        try:
            return bytes(self.client.download_blob(key).readall())
        except Exception as exc:
            if not_found(exc):
                raise KeyError(key) from exc
            raise

    def keys(self, prefix: str = "") -> list[str]:
        return sorted(b.name for b in self.client.list_blobs(name_starts_with=prefix or None))

"""Cloud Storage with locked object retention: the ImmutableStore behind the audit anchors and
the checkpoint archive.

The bucket has object retention enabled (`--enable-per-object-retention`), so each object is
written with its own retention in Locked mode: no one can shorten it, or change or delete the
object, until it ends. Written once: the upload asks that no object of that name exist
(`if_generation_match=0`). Encryption is the bucket's default key (CMEK). Cloud Storage has no tags
for a read rule, so an object's scope is its metadata and its path: IAM Conditions on the object
name's prefix (checkpoints/<workspace>/) limit who reads it."""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from typing import Any

from . import not_found, precondition_failed


class GcsImmutable:
    def __init__(self, bucket: str, *, client: Any = None, clock=time.time) -> None:
        self.bucket_name, self._client, self.clock = bucket, client, clock

    @property
    def client(self) -> Any:
        if self._client is None:
            from google.cloud import storage  # the 'gcp' extra

            self._client = storage.Client()
        return self._client

    @property
    def where(self) -> str:
        return f"gs://{self.bucket_name}"

    def put(self, key: str, data: bytes, *, retain_days: int, content_type: str = "application/octet-stream",
            encoding: str = "", meta: dict[str, str] | None = None, scope: dict[str, str] | None = None) -> None:
        blob = self.client.bucket(self.bucket_name).blob(key)
        blob.metadata = {**(meta or {}), **{f"valxel-{k}": v for k, v in (scope or {}).items()}} or None
        if encoding:
            blob.content_encoding = encoding
        blob.retention.mode = "Locked"
        blob.retention.retain_until_time = datetime.fromtimestamp(self.clock(), UTC) + timedelta(days=retain_days)
        try:
            blob.upload_from_string(data, content_type=content_type, if_generation_match=0)
        except Exception as exc:
            if precondition_failed(exc):
                raise FileExistsError(f"{self.where}/{key} is already written") from exc
            raise

    def get(self, key: str) -> bytes:
        try:
            return self.client.bucket(self.bucket_name).blob(key).download_as_bytes()
        except Exception as exc:
            if not_found(exc):
                raise KeyError(key) from exc
            raise

    def keys(self, prefix: str = "") -> list[str]:
        return sorted(b.name for b in self.client.list_blobs(self.bucket_name, prefix=prefix))

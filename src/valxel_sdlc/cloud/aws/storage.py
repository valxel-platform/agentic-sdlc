"""S3 with Object Lock in compliance mode: the ImmutableStore behind the audit anchors and the
checkpoint archive. The bucket encrypts with KMS; each object is locked until its retention ends."""

from __future__ import annotations

import base64
import json
import time
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode


class S3Immutable:
    def __init__(self, bucket: str, *, s3: Any = None, clock=time.time) -> None:
        self.bucket, self._s3, self.clock = bucket, s3, clock

    @property
    def s3(self) -> Any:
        if self._s3 is None:
            import boto3  # the 'aws' extra

            self._s3 = boto3.client("s3")
        return self._s3

    @property
    def where(self) -> str:
        return f"s3://{self.bucket}"

    @staticmethod
    def _code(exc: Exception) -> str:
        return (getattr(exc, "response", None) or {}).get("Error", {}).get("Code", "")

    def put(self, key: str, data: bytes, *, retain_days: int, content_type: str = "application/octet-stream",
            encoding: str = "", meta: dict[str, str] | None = None, scope: dict[str, str] | None = None) -> None:
        now = datetime.fromtimestamp(self.clock(), UTC)
        kw: dict[str, Any] = {
            "Bucket": self.bucket, "Key": key, "Body": data, "ContentType": content_type,
            "ChecksumAlgorithm": "SHA256", "ServerSideEncryption": "aws:kms",
            "ObjectLockMode": "COMPLIANCE", "ObjectLockRetainUntilDate": now + timedelta(days=retain_days),
            "IfNoneMatch": "*",  # written once: S3 refuses a key that exists
        }
        if encoding:
            kw["ContentEncoding"] = encoding
        if meta:
            kw["Metadata"] = dict(meta)
        if scope:
            # The data key carries the scope (no bucket key, which would drop it), and the tags are
            # what the bucket's read rule matches against a reader's own.
            context = {f"valxel:{k}": v for k, v in scope.items()}
            kw["SSEKMSEncryptionContext"] = base64.b64encode(json.dumps(context).encode()).decode()
            kw["BucketKeyEnabled"] = False
            kw["Tagging"] = urlencode(scope)
        try:
            self.s3.put_object(**kw)
        except Exception as exc:
            if self._code(exc) in ("PreconditionFailed", "ConditionalRequestConflict"):
                raise FileExistsError(f"{self.where}/{key} is already written") from exc
            raise

    def get(self, key: str) -> bytes:
        try:
            return self.s3.get_object(Bucket=self.bucket, Key=key)["Body"].read()
        except KeyError:
            raise
        except Exception as exc:
            if self._code(exc) in ("NoSuchKey", "404"):
                raise KeyError(key) from exc
            raise

    def keys(self, prefix: str = "") -> list[str]:
        out: list[str] = []
        token = None
        while True:
            kw = {"Bucket": self.bucket, "Prefix": prefix}
            if token:
                kw["ContinuationToken"] = token
            page = self.s3.list_objects_v2(**kw)
            out += [o["Key"] for o in page.get("Contents") or []]
            if not page.get("IsTruncated"):
                return sorted(out)
            token = page.get("NextContinuationToken")

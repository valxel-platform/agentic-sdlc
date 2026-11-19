"""The checkpoints of regulated lanes, archived before they're pruned.

After each run the engine keeps only a thread's latest checkpoint (engine._prune): the earlier
ones are every step's state, which is what an agent saw and decided at that step. For a lane whose
data class is regulated (phi, payments), that history is the forensic record, so it is archived
first: every checkpoint the thread holds, in every namespace, with its metadata and pending writes,
serialized exactly as the checkpointer stores them, as one gzipped JSON Lines file:

    s3://<VALXEL_CHECKPOINT_ARCHIVE_BUCKET>/checkpoints/<workspace>/<product>/<thread>/<UTC time>-<sha256 prefix>.jsonl.gz

Each file belongs to its workspace and product, three ways: its path; its tags (`workspace`,
`product`), which the bucket's policy matches against a reader's own `valxel-workspace` tag, so a
reader opens only their workspace's files; and its KMS encryption context (`valxel:workspace`,
`valxel:product`, without the bucket key, so every file's data key carries it), so decrypting a
file needs a grant for that workspace too.

The bucket encrypts with the platform's KMS key and holds each file under S3 Object Lock
(compliance mode) for VALXEL_CHECKPOINT_ARCHIVE_RETENTION_DAYS (2190 by default, six years): no
one can delete or change it before then, the tool included (it may only write). The file's SHA-256
is in its metadata and its name, so a copy can be checked.

If a regulated lane's checkpoints can't be archived (no bucket set, or the upload fails) they are
not pruned: the history stays in Postgres until an archive succeeds. Other lanes prune as before.
The latest checkpoint is kept in Postgres and is archived again with the next run's; a file may
repeat one checkpoint, never miss one.
"""

from __future__ import annotations

import base64
import gzip
import hashlib
import json
import logging
import os
import time
from typing import Any

from .profiles import REGULATED

log = logging.getLogger(__name__)
FORMAT = "valxel.checkpoints.v1"


class NotArchived(RuntimeError):
    pass


def _typed(serde: Any, value: Any) -> dict:
    kind, data = serde.dumps_typed(value)
    return {"type": kind, "data": base64.b64encode(data).decode()}


def bundle(saver: Any, thread_id: str) -> tuple[bytes, int]:
    """Every checkpoint of the thread as gzipped JSON Lines, and how many there are."""
    lines = []
    for t in saver.list({"configurable": {"thread_id": thread_id}}):
        conf = t.config["configurable"]
        lines.append(json.dumps({
            "format": FORMAT,
            "thread_id": thread_id,
            "checkpoint_ns": conf.get("checkpoint_ns", ""),
            "checkpoint_id": conf.get("checkpoint_id", ""),
            "parent_checkpoint_id": ((t.parent_config or {}).get("configurable") or {}).get("checkpoint_id"),
            "metadata": _typed(saver.serde, t.metadata),
            "checkpoint": _typed(saver.serde, t.checkpoint),
            "pending_writes": [{"task_id": w[0], "channel": w[1], "value": _typed(saver.serde, w[2])}
                               for w in t.pending_writes or []],
        }, separators=(",", ":"), sort_keys=True))
    raw = ("\n".join(lines) + "\n").encode() if lines else b""
    return gzip.compress(raw, mtime=0), len(lines)


def read(data: bytes, serde: Any) -> list[dict]:
    """An archive file back into checkpoints (for forensics and the tests)."""
    out = []
    for line in gzip.decompress(data).decode().splitlines():
        rec = json.loads(line)
        for k in ("metadata", "checkpoint"):
            rec[k] = serde.loads_typed((rec[k]["type"], base64.b64decode(rec[k]["data"])))
        for w in rec["pending_writes"]:
            w["value"] = serde.loads_typed((w["value"]["type"], base64.b64decode(w["value"]["data"])))
        out.append(rec)
    return out


class Archive:
    def __init__(self, bucket: str, retention_days: int = 2190, *, s3: Any = None,
                 clock: Any = time.time) -> None:
        self.bucket, self.retention_days, self.clock = bucket, retention_days, clock
        self._s3 = s3

    @classmethod
    def from_env(cls) -> Archive | None:
        bucket = os.environ.get("VALXEL_CHECKPOINT_ARCHIVE_BUCKET", "")
        if not bucket:
            return None
        return cls(bucket, int(os.environ.get("VALXEL_CHECKPOINT_ARCHIVE_RETENTION_DAYS") or 2190))

    @property
    def s3(self) -> Any:
        if self._s3 is None:
            import boto3

            self._s3 = boto3.client("s3")
        return self._s3

    def put(self, saver: Any, product_id: str, thread_id: str, workspace: str = "") -> str:
        """Archive the thread's checkpoints under its workspace and product; the object's key.
        Raises NotArchived."""
        from datetime import UTC, datetime, timedelta
        from urllib.parse import urlencode

        workspace = workspace or "unassigned"
        for part in (workspace, product_id):
            if not part or "/" in part or part.startswith("."):
                raise NotArchived(f"not a workspace or product name: {part!r}")

        try:
            data, n = bundle(saver, thread_id)
        except Exception as exc:
            raise NotArchived(f"couldn't read {thread_id}'s checkpoints: {type(exc).__name__}") from exc
        if not n:
            return ""
        digest = hashlib.sha256(data).hexdigest()
        now = datetime.fromtimestamp(self.clock(), UTC)
        key = (f"checkpoints/{workspace}/{product_id}/{thread_id}/"
               f"{now.strftime('%Y%m%dT%H%M%SZ')}-{digest[:16]}.jsonl.gz")
        context = {"valxel:workspace": workspace, "valxel:product": product_id}
        try:
            self.s3.put_object(
                Bucket=self.bucket, Key=key, Body=data, ContentType="application/x-ndjson",
                ContentEncoding="gzip", ChecksumAlgorithm="SHA256",
                ServerSideEncryption="aws:kms",
                # The data key carries the workspace and product: no bucket key, which would drop it.
                SSEKMSEncryptionContext=base64.b64encode(json.dumps(context).encode()).decode(),
                BucketKeyEnabled=False,
                Tagging=urlencode({"workspace": workspace, "product": product_id}),
                ObjectLockMode="COMPLIANCE",
                ObjectLockRetainUntilDate=now + timedelta(days=self.retention_days),
                Metadata={"sha256": digest, "checkpoints": str(n), "format": FORMAT,
                          "workspace": workspace, "product": product_id, "thread": thread_id})
        except Exception as exc:
            raise NotArchived(f"upload to {self.bucket} failed: {type(exc).__name__}") from exc
        log.info("archived %d checkpoints of %s to s3://%s/%s", n, thread_id, self.bucket, key)
        return key


def regulated(services: Any, product_id: str) -> bool:
    """Whether the lane's data class needs its history kept. Unknown counts as regulated."""
    try:
        return services.snapshot(product_id).product.data_class in REGULATED
    except Exception:  # noqa: BLE001 - governance unreadable: keep the history
        return True

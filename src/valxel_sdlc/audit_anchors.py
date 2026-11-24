"""Anchors for the tool's hash chains: each chain's head, kept where the tool can't rewrite it
(engineering review finding 7, T-135).

The workspace audit logs and the production change log are hash chains in the tool's own database.
A chain alone shows an edit only to someone who didn't recompute the hashes after it: whoever can
disable the append-only trigger can also rewrite every later hash, or cut the tail off. So every
hour the watcher writes each chain's head (how many entries, the last hash) as an object under S3
Object Lock in COMPLIANCE mode (VALXEL_AUDIT_ANCHOR_BUCKET), which no one can change or delete until
it expires, and sends it to the audit sink as an `audit.anchor` record too.

`valxel audit verify` then checks each chain against its anchors: an anchored entry rewritten, or a
chain shorter than an anchor says it was, is tampering however well the hashes were recomputed.
What was written since the last anchor (an hour at most) has only the chain's own protection.
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import UTC, datetime
from typing import Any

from .cloud.ports import AnchorStore  # the port (ADR 0010)

log = logging.getLogger(__name__)
GENESIS = "0" * 64


def head(entries: list[dict]) -> dict:
    """A chain's head: how many entries, and the hash of the last one that has one."""
    last = next((e.get("hash") for e in reversed(entries) if e.get("hash")), None)
    return {"count": len(entries), "hash": last or GENESIS}


def check(entries: list[dict], anchors: list[dict]) -> list[str]:
    """What the chain contradicts: each anchor's entry must still carry the anchored hash."""
    out = []
    for a in sorted(anchors, key=lambda a: a["count"]):
        n = int(a["count"])
        if n == 0:
            continue
        if len(entries) < n:
            out.append(f"{len(entries)} entries, but {a.get('at', 'an anchor')} saw {n}: the tail was removed")
            continue
        have = next((e.get("hash") for e in reversed(entries[:n]) if e.get("hash")), None) or GENESIS
        if have != a["hash"]:
            out.append(f"entry {n} no longer has the hash anchored at {a.get('at', '?')}: rewritten")
    return out


class MemoryAnchors:
    def __init__(self) -> None:
        self.by: dict[str, list[dict]] = {}

    def put(self, chain, h):
        self.by.setdefault(chain, []).append({**h, "at": datetime.now(UTC).isoformat()})

    def list(self, chain, limit=0):
        rows = self.by.get(chain, [])
        return rows[-limit:] if limit else list(rows)


class ObjectAnchors:
    """anchors/<chain>/<time>.json in an ImmutableStore (S3 Object Lock in compliance mode, or Cloud
    Storage's locked retention): kept unchanged for retention_days."""

    def __init__(self, store: Any, retention_days: int = 2190, *, clock=time.time) -> None:
        self.store, self.retention_days, self.clock = store, retention_days, clock

    @classmethod
    def from_env(cls, env=None) -> ObjectAnchors | None:
        from .cloud import immutable_store

        e = os.environ if env is None else env
        bucket = e.get("VALXEL_AUDIT_ANCHOR_BUCKET", "")
        if not bucket:
            return None
        return cls(immutable_store(bucket, env=e), int(e.get("VALXEL_AUDIT_ANCHOR_RETENTION_DAYS") or 2190))

    @staticmethod
    def _safe(chain: str) -> str:
        if not chain or "/" in chain or chain.startswith(".") or len(chain) > 200:
            raise ValueError(f"not a chain name: {chain!r}")
        return chain

    def put(self, chain, h):
        now = datetime.fromtimestamp(self.clock(), UTC)
        body = json.dumps({**h, "chain": chain, "at": now.isoformat()}, sort_keys=True).encode()
        key = f"anchors/{self._safe(chain)}/{now.strftime('%Y%m%dT%H%M%S%fZ')}.json"
        self.store.put(key, body, retain_days=self.retention_days, content_type="application/json")

    def list(self, chain, limit=0):
        keys = self.store.keys(f"anchors/{self._safe(chain)}/")  # the time in the key sorts them
        if limit:
            keys = keys[-limit:]
        return [json.loads(self.store.get(k)) for k in keys]


class S3Anchors(ObjectAnchors):
    """ObjectAnchors on an S3 Object Lock bucket, by name (as before ADR 0010)."""

    def __init__(self, bucket: str, retention_days: int = 2190, *, s3: Any = None, clock=time.time) -> None:
        from .cloud.aws.storage import S3Immutable

        super().__init__(S3Immutable(bucket, s3=s3, clock=clock), retention_days, clock=clock)
        self.bucket = bucket


def chains(workspaces, changes) -> dict[str, list[dict]]:
    """Every chain the tool keeps, oldest entry first."""
    store = getattr(workspaces, "store", None)
    out = {f"workspace-{ws}": store.audit_chain(ws)
           for ws in sorted({w.get("id", "") for w in store.list("workspace")} - {""})} if store is not None else {}
    if changes is not None:
        out["changes"] = changes.all()
    return out


class Anchorer:
    """The watcher's anchor_audit_chains job: a chain's head is written when it moved since the last."""

    def __init__(self, store: AnchorStore | None, workspaces, changes, audit=None) -> None:
        self.store, self.workspaces, self.changes, self.audit = store, workspaces, changes, audit
        self.last: dict[str, dict] = {}

    def run(self) -> int:
        if self.store is None:
            return 0
        n = 0
        for chain, entries in chains(self.workspaces, self.changes).items():
            h = head(entries)
            if self.last.get(chain) == h or h["count"] == 0:
                continue
            self.store.put(chain, h)
            self.last[chain] = h
            n += 1
            if self.audit is not None:  # a second copy, in the audit sink's archive
                self.audit.record("audit.anchor", actor="valxel", actor_type="service",
                                  resource=("audit-chain", [chain]), details={"count": h["count"], "hash": h["hash"]})
        return n


def verify(store: AnchorStore, workspaces, changes, limit: int = 0) -> dict[str, list[str]]:
    """chain -> what contradicts its anchors (empty: consistent)."""
    return {chain: check(entries, store.list(chain, limit)) for chain, entries in chains(workspaces, changes).items()}

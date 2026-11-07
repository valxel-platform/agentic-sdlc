"""The change log: one record per production change, written as it happens, for audit and lookback.

Every way the tool changes production writes one:

    release   a feature reaching production at G6 (its components, AWS resources, store apps and
              flag), or a launch turning its features' flags on
    flag      a feature's flag turned off (or on) outside a release
    rollback  a component rolled back to its previous release

and each is one of three types:

    standard   went through the gates: who approved what, and the evidence (pull requests, the
               quality suites each stage ran, accepted security findings)
    emergency  a hotfix: the expedited track an incident starts
    automatic  the tool's own mitigation under the platform's remediation policy (VX-3 exception),
               reviewed afterwards: the incident's postmortem is that review

Records are append-only and hash-chained: each carries the hash of the one before it, so a record
changed or removed after the fact breaks the chain (`vrixel changes verify`). The link between an
incident and the change behind it (the release whose flag or rollback mitigated it) is kept beside
the chain, since it is learned later; it is what change failure rate counts.
"""

from __future__ import annotations

import hashlib
import json
import threading
from datetime import datetime, timezone
from typing import Protocol

KINDS = ("release", "flag", "rollback")
TYPES = ("standard", "emergency", "automatic")
GENESIS = "0" * 64
_CHAINED = ("id", "key", "at", "product_id", "env", "kind", "type", "action", "summary", "targets", "feature_id",
            "release_id", "incident_id", "requested_by", "approved_by", "approvals", "evidence", "outcome", "detail")


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def digest(prev: str, rec: dict) -> str:
    body = json.dumps({k: rec.get(k) for k in _CHAINED}, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256((prev + body).encode()).hexdigest()


def new(*, key: str, product_id: str, kind: str, type: str, action: str, summary: str, targets: list[dict],
        requested_by: str = "", approved_by: list[str] | None = None, approvals: list[dict] | None = None,
        evidence: dict | None = None, feature_id: str = "", release_id: str = "", incident_id: str = "",
        outcome: str = "done", detail: str = "", env: str = "production") -> dict:
    if kind not in KINDS or type not in TYPES:
        raise ValueError(f"a change is one of {KINDS} and {TYPES}")
    return {"key": key, "at": now(), "product_id": product_id, "env": env, "kind": kind, "type": type,
            "action": action, "summary": summary[:500], "targets": targets, "feature_id": feature_id,
            "release_id": release_id, "incident_id": incident_id, "requested_by": requested_by,
            "approved_by": list(approved_by or []), "approvals": list(approvals or []), "evidence": evidence or {},
            "outcome": outcome, "detail": detail[:1000]}


def verify(records: list[dict]) -> dict:
    """Check the chain, oldest first: {"ok", "records", "broken_at" (first bad id, if any)}."""
    prev = GENESIS
    for r in records:
        if r.get("prev_hash") != prev or r.get("hash") != digest(prev, r):
            return {"ok": False, "records": len(records), "broken_at": r.get("id")}
        prev = r["hash"]
    return {"ok": True, "records": len(records), "broken_at": None}


class ChangeLog(Protocol):
    def record(self, rec: dict) -> dict: ...  # the stored record (an earlier one with the same key wins)
    def list(self, product_id: str | None = None, since: str | None = None, until: str | None = None) -> list[dict]: ...
    def link(self, change_id: str, incident_id: str, why: str) -> None: ...
    def links(self, product_id: str | None = None) -> list[dict]: ...  # [{change_id, incident_id, why, at}]
    def all(self) -> list[dict]: ...  # every record, oldest first (for verify)


def _filter(rows: list[dict], product_id, since, until) -> list[dict]:
    return [r for r in rows if (not product_id or r["product_id"] == product_id)
            and (not since or r["at"] >= since) and (not until or r["at"] < until)]


class InMemoryChangeLog:
    def __init__(self) -> None:
        self.rows: list[dict] = []
        self._links: list[dict] = []
        self._lock = threading.Lock()

    def record(self, rec: dict) -> dict:
        with self._lock:
            found = next((r for r in self.rows if r["key"] == rec["key"]), None)
            if found:
                return dict(found)
            prev = self.rows[-1]["hash"] if self.rows else GENESIS
            stored = {**rec, "id": f"CHG-{len(self.rows) + 1}", "prev_hash": prev}
            stored["hash"] = digest(prev, stored)
            self.rows.append(stored)
            return dict(stored)

    def list(self, product_id=None, since=None, until=None) -> list[dict]:
        with self._lock:
            return [dict(r) for r in _filter(self.rows, product_id, since, until)]

    def all(self) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self.rows]

    def link(self, change_id, incident_id, why) -> None:
        with self._lock:
            if not any(x["change_id"] == change_id and x["incident_id"] == incident_id for x in self._links):
                product = next((r["product_id"] for r in self.rows if r["id"] == change_id), "")
                self._links.append({"change_id": change_id, "incident_id": incident_id, "why": why, "at": now(),
                                    "product_id": product})

    def links(self, product_id=None) -> list[dict]:
        with self._lock:
            return [dict(x) for x in self._links if not product_id or x["product_id"] == product_id]


class PostgresChangeLog:
    """Append-only in Postgres. Grant the tool's role INSERT and SELECT only on these tables so
    nothing it runs can rewrite history; the hash chain shows it if anything else does."""

    DDL = (
        """CREATE TABLE IF NOT EXISTS vrixel_changes (
             seq BIGSERIAL PRIMARY KEY, id TEXT UNIQUE NOT NULL, key TEXT UNIQUE NOT NULL,
             product_id TEXT NOT NULL, at TIMESTAMPTZ NOT NULL, data JSONB NOT NULL,
             prev_hash TEXT NOT NULL, hash TEXT NOT NULL)""",
        "CREATE INDEX IF NOT EXISTS vrixel_changes_product ON vrixel_changes (product_id, at)",
        """CREATE TABLE IF NOT EXISTS vrixel_change_incidents (
             change_id TEXT NOT NULL, incident_id TEXT NOT NULL, product_id TEXT NOT NULL, why TEXT NOT NULL,
             at TIMESTAMPTZ NOT NULL DEFAULT now(), PRIMARY KEY (change_id, incident_id))""",
    )

    def __init__(self, pool) -> None:
        self.pool = pool
        with self.pool.connection() as conn:
            for statement in self.DDL:
                conn.execute(statement)

    @staticmethod
    def _row(r) -> dict:
        return {**dict(r["data"]), "id": r["id"], "prev_hash": r["prev_hash"], "hash": r["hash"]}

    def record(self, rec: dict) -> dict:
        with self.pool.connection() as conn, conn.transaction():
            # One writer at a time keeps the chain linear across replicas.
            conn.execute("SELECT pg_advisory_xact_lock(hashtext('vrixel_changes'))")
            found = conn.execute("SELECT * FROM vrixel_changes WHERE key = %s", (rec["key"],)).fetchone()
            if found:
                return self._row(found)
            last = conn.execute("SELECT seq, hash FROM vrixel_changes ORDER BY seq DESC LIMIT 1").fetchone()
            prev = last["hash"] if last else GENESIS
            stored = {**rec, "id": f"CHG-{(last['seq'] if last else 0) + 1}", "prev_hash": prev}
            stored["hash"] = digest(prev, stored)
            data = {k: v for k, v in stored.items() if k not in ("id", "prev_hash", "hash")}
            conn.execute("INSERT INTO vrixel_changes (id, key, product_id, at, data, prev_hash, hash) "
                         "VALUES (%s, %s, %s, %s, %s::jsonb, %s, %s)",
                         (stored["id"], rec["key"], rec["product_id"], rec["at"], json.dumps(data, default=str),
                          prev, stored["hash"]))
            return stored

    def list(self, product_id=None, since=None, until=None) -> list[dict]:
        q, args = "SELECT * FROM vrixel_changes WHERE true", []
        if product_id:
            q, args = q + " AND product_id = %s", [*args, product_id]
        if since:
            q, args = q + " AND at >= %s", [*args, since]
        if until:
            q, args = q + " AND at < %s", [*args, until]
        with self.pool.connection() as conn:
            return [self._row(r) for r in conn.execute(q + " ORDER BY seq", args).fetchall()]

    def all(self) -> list[dict]:
        return self.list()

    def link(self, change_id, incident_id, why) -> None:
        with self.pool.connection() as conn:
            conn.execute("INSERT INTO vrixel_change_incidents (change_id, incident_id, product_id, why) "
                         "SELECT id, %s, product_id, %s FROM vrixel_changes WHERE id = %s "
                         "ON CONFLICT DO NOTHING", (incident_id, why, change_id))

    def links(self, product_id=None) -> list[dict]:
        q = "SELECT change_id, incident_id, product_id, why, at FROM vrixel_change_incidents"
        with self.pool.connection() as conn:
            rows = conn.execute(q + (" WHERE product_id = %s" if product_id else "") + " ORDER BY at",
                                [product_id] if product_id else []).fetchall()
        return [{**dict(r), "at": r["at"].isoformat() if hasattr(r["at"], "isoformat") else r["at"]} for r in rows]


# --- what a release's record says ------------------------------------------------------------

def approvals_of(state: dict) -> list[dict]:
    """Every gate decision that let the feature through: G1 to G6, who and when."""
    return [{"gate": g.get("gate", ""), "lane": g.get("lane", ""), "approver": g.get("approver", ""),
             "at": g.get("at", "")}
            for g in state.get("gate_decisions") or [] if g.get("decision") in ("approve", "handback")]


def evidence_of(state: dict) -> dict:
    """What the release was checked with: its pull requests, each stage's quality suites, and any
    security findings accepted with an expiry."""
    prs = [p.get("url", "") for p in (state.get("pull_requests") or {}).values() if p.get("url")]
    quality = {}
    for stage, suites in (state.get("quality") or {}).items():
        if isinstance(suites, dict):
            quality[stage] = {k: (v.get("status") or v.get("result") or v.get("ok")) if isinstance(v, dict) else v
                              for k, v in suites.items()}
    waivers = [{"finding": w.get("finding") or w.get("id", ""), "expires": w.get("expires", ""),
                "by": w.get("approver") or w.get("by", "")} for w in state.get("waivers") or []]
    return {"pull_requests": prs, "quality": quality, "waivers": waivers}


def release_targets(deployments: dict, flag: str = "", flag_on: bool | None = None) -> list[dict]:
    out = []
    for key, d in sorted((deployments or {}).items()):
        if d.get("env") not in (None, "", "production") and d.get("kind") != "resource":
            continue
        kind = d.get("kind") or "component"
        out.append({"kind": "store_app" if kind == "store" else kind,
                    "name": d.get("name") or d.get("component") or d.get("repo") or key,
                    "repo": d.get("repo", ""), "release": d.get("release", "")})
    if flag:
        out.append({"kind": "flag", "name": flag, "on": bool(flag_on)})
    return out


def release_record(state: dict, approver: str, n: int) -> dict:
    """The record for a feature's G6 release (n: which G6 approval this is, for the key)."""
    hotfix = state.get("track") == "hotfix"
    release = state.get("release") or {}
    return new(
        key=f"{state['feature_id']}:g6:{n}", product_id=state["product_id"], kind="release",
        type="emergency" if hotfix else "standard", action="deploy",
        summary=f"{state['feature_id']} {state.get('title', '')}".strip(),
        targets=release_targets(state.get("deployments") or {}, release.get("flag", ""), release.get("flag_on")),
        feature_id=state["feature_id"], release_id=release.get("release_id", ""),
        incident_id=(state.get("hotfix") or {}).get("incident_id", "") if hotfix else "",
        requested_by=state.get("created_by", ""), approved_by=[a for a in approver.split(", ") if a],
        approvals=approvals_of(state), evidence=evidence_of(state))


def cause_of(log: ChangeLog, product_id: str, *, feature_id: str = "", component: str = "") -> dict | None:
    """The latest production release of a feature, or that reached a component: what an incident's
    flag off or rollback undid."""
    short = component.rsplit("/", 1)[-1]
    for r in reversed(log.list(product_id)):
        if r["kind"] != "release" or r["outcome"] != "done":
            continue
        if feature_id and r["feature_id"] == feature_id:
            return r
        if short and any(t.get("kind") == "component" and (t.get("repo", "").rsplit("/", 1)[-1] == short
                                                            or t.get("name") == short) for t in r["targets"]):
            return r
    return None

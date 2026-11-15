"""The findings register: one record per security alert on a product's default branch.

    {id, product_id, repo, kind, tool, rule, severity, title, url, path, package, patched, epss,
     validity, state,               GitHub's: open | fixed | dismissed
     status,                        ours, below
     outcome, why,                  hotfix | scheduled | backlog, and the policy's reason
     first_seen, deadline, release_by, fix_feature, release_id, attempts, incident_id,
     accepted: {approver, reason, expires}, history: [{at, what}], updated}

status:
    open        needs triage (new, reopened, or its fix didn't close it)
    planned     triaged; waits for a fix slot (max_open_fixes) or for Dependabot's own pull request
    fixing      a fix feature is on it (fix_feature)
    backlog     no deadline (info): recorded, fixed when a feature touches it
    accepted    a person accepted the risk until `accepted.expires`; it comes back then
    escalated   two fixes didn't close it, or its deadline passed: security approvers decide
    incident    an exposed secret: its incident (incident_id) handles it
    fixed       GitHub closed the alert on the default branch
    dismissed   dismissed in GitHub (a person's call, with GitHub's reason)

Releases a held fix joined (release joins) are kept here too: a release thread's members are fixed
when it's created, so a fix that rides along is recorded beside it (valxel_sdlc.engine.release_for).
"""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime
from typing import Protocol

OPEN_STATUSES = ("open", "planned", "fixing", "escalated", "incident")
CLOSED = ("fixed", "dismissed")
_FIELDS = ("repo", "kind", "tool", "rule", "severity", "title", "url", "path", "package", "patched", "epss",
           "validity", "number", "publicly_leaked")


def now() -> str:
    return datetime.now(UTC).isoformat()


def _merge(rec: dict | None, f: dict, product_id: str) -> tuple[dict, str | None]:
    """The record after GitHub's view `f`, and what changed: new, reopened, closed, updated or None."""
    at = now()
    if rec is None:
        rec = {"id": f["id"], "product_id": product_id, **{k: f.get(k) for k in _FIELDS}, "state": f["state"],
               "status": "open" if f["state"] == "open" else f["state"], "outcome": "", "why": "",
               "first_seen": at, "deadline": "", "release_by": "", "fix_feature": "", "release_id": "",
               "attempts": 0, "incident_id": "", "accepted": None,
               "history": [{"at": at, "what": f"seen: {f['state']}"}], "updated": at}
        return rec, "new"
    rec = {**rec, **{k: f.get(k) for k in _FIELDS if f.get(k) not in (None, "")}}
    change = None
    if f["state"] != rec["state"]:
        if f["state"] == "open":
            change = "reopened"
            rec["status"] = "open"
            rec["fix_feature"] = ""
        else:
            change = "closed"
            rec["status"] = f["state"]
        rec["state"] = f["state"]
        rec["history"] = [*(rec.get("history") or []), {"at": at, "what": f"GitHub: {f['state']}"}]
        rec["updated"] = at
    return rec, change


class FindingRegister(Protocol):
    def upsert(self, product_id: str, finding: dict) -> tuple[dict, str | None]: ...
    def get(self, finding_id: str) -> dict | None: ...
    def list(self, product_id: str | None = None, statuses: tuple[str, ...] | None = None) -> list[dict]: ...
    def update(self, finding_id: str, note: str = "", **fields) -> dict: ...
    def join(self, feature_id: str, release_id: str) -> None: ...
    def unjoin(self, feature_id: str) -> None: ...
    def release_of(self, feature_id: str) -> str | None: ...
    def joined(self, release_id: str) -> list[str]: ...


def _apply(rec: dict, note: str, fields: dict) -> dict:
    at = now()
    rec = {**rec, **fields, "updated": at}
    if note:
        rec["history"] = [*(rec.get("history") or []), {"at": at, "what": note}][-50:]
    return rec


class InMemoryFindingRegister:
    def __init__(self) -> None:
        self._rows: dict[str, dict] = {}
        self._joins: dict[str, str] = {}
        self._lock = threading.Lock()

    def upsert(self, product_id, finding):
        with self._lock:
            rec, change = _merge(self._rows.get(finding["id"]), finding, product_id)
            self._rows[rec["id"]] = rec
            return json.loads(json.dumps(rec)), change

    def get(self, finding_id):
        with self._lock:
            r = self._rows.get(finding_id)
            return json.loads(json.dumps(r)) if r else None

    def list(self, product_id=None, statuses=None):
        with self._lock:
            return [json.loads(json.dumps(r)) for r in sorted(self._rows.values(), key=lambda r: r["first_seen"])
                    if (product_id is None or r["product_id"] == product_id)
                    and (statuses is None or r["status"] in statuses)]

    def update(self, finding_id, note="", **fields):
        with self._lock:
            self._rows[finding_id] = _apply(self._rows[finding_id], note, fields)
            return json.loads(json.dumps(self._rows[finding_id]))

    def join(self, feature_id, release_id):
        with self._lock:
            self._joins[feature_id] = release_id

    def unjoin(self, feature_id):
        with self._lock:
            self._joins.pop(feature_id, None)

    def release_of(self, feature_id):
        with self._lock:
            return self._joins.get(feature_id)

    def joined(self, release_id):
        with self._lock:
            return sorted(f for f, r in self._joins.items() if r == release_id)


class PostgresFindingRegister:
    DDL = (
        """CREATE TABLE IF NOT EXISTS valxel_findings (
             id TEXT PRIMARY KEY, product_id TEXT NOT NULL, status TEXT NOT NULL, data JSONB NOT NULL,
             updated TIMESTAMPTZ NOT NULL DEFAULT now())""",
        "CREATE INDEX IF NOT EXISTS valxel_findings_product ON valxel_findings (product_id, status)",
        """CREATE TABLE IF NOT EXISTS valxel_release_joins (
             feature_id TEXT PRIMARY KEY, release_id TEXT NOT NULL, at TIMESTAMPTZ NOT NULL DEFAULT now())""",
    )

    def __init__(self, pool) -> None:
        self.pool = pool
        with pool.connection() as conn:
            for stmt in self.DDL:
                conn.execute(stmt)

    def _put(self, conn, rec: dict) -> None:
        conn.execute("INSERT INTO valxel_findings (id, product_id, status, data, updated) VALUES (%s, %s, %s, %s::jsonb, now()) "
                     "ON CONFLICT (id) DO UPDATE SET product_id = EXCLUDED.product_id, status = EXCLUDED.status, "
                     "data = EXCLUDED.data, updated = now()",
                     (rec["id"], rec["product_id"], rec["status"], json.dumps(rec)))

    def upsert(self, product_id, finding):
        with self.pool.connection() as conn, conn.transaction():
            conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (finding["id"],))
            row = conn.execute("SELECT data FROM valxel_findings WHERE id = %s", (finding["id"],)).fetchone()
            rec, change = _merge(row["data"] if row else None, finding, product_id)
            self._put(conn, rec)
            return rec, change

    def get(self, finding_id):
        with self.pool.connection() as conn:
            row = conn.execute("SELECT data FROM valxel_findings WHERE id = %s", (finding_id,)).fetchone()
        return row["data"] if row else None

    def list(self, product_id=None, statuses=None):
        q, args = "SELECT data FROM valxel_findings WHERE true", []
        if product_id is not None:
            q, args = q + " AND product_id = %s", [*args, product_id]
        if statuses is not None:
            q, args = q + " AND status = ANY(%s)", [*args, list(statuses)]
        with self.pool.connection() as conn:
            rows = conn.execute(q, args).fetchall()
        return sorted((r["data"] for r in rows), key=lambda r: r["first_seen"])

    def update(self, finding_id, note="", **fields):
        with self.pool.connection() as conn, conn.transaction():
            conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (finding_id,))
            row = conn.execute("SELECT data FROM valxel_findings WHERE id = %s", (finding_id,)).fetchone()
            rec = _apply(row["data"], note, fields)
            self._put(conn, rec)
            return rec

    def join(self, feature_id, release_id):
        with self.pool.connection() as conn:
            conn.execute("INSERT INTO valxel_release_joins (feature_id, release_id) VALUES (%s, %s) "
                         "ON CONFLICT (feature_id) DO UPDATE SET release_id = EXCLUDED.release_id, at = now()",
                         (feature_id, release_id))

    def unjoin(self, feature_id):
        with self.pool.connection() as conn:
            conn.execute("DELETE FROM valxel_release_joins WHERE feature_id = %s", (feature_id,))

    def release_of(self, feature_id):
        with self.pool.connection() as conn:
            row = conn.execute("SELECT release_id FROM valxel_release_joins WHERE feature_id = %s", (feature_id,)).fetchone()
        return row["release_id"] if row else None

    def joined(self, release_id):
        with self.pool.connection() as conn:
            rows = conn.execute("SELECT feature_id FROM valxel_release_joins WHERE release_id = %s ORDER BY feature_id",
                                (release_id,)).fetchall()
        return [r["feature_id"] for r in rows]

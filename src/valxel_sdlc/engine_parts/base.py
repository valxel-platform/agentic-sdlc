"""What the engine and its parts share: thread kinds, the feature index and the progress rail."""

from __future__ import annotations

import os
import threading
from typing import Protocol






KINDS = {"FEAT": "feature", "INI": "initiative", "REL": "release", "INC": "incident", "PRD": "inception",
         "REV": "review"}


def kind_of(thread_id: str) -> str:
    """CLN-FEAT-3 -> feature, CLN-INI-1 -> initiative, CLN-REL-2 -> release."""
    parts = thread_id.split("-")
    return KINDS.get(parts[1], "feature") if len(parts) >= 3 else "feature"


class FeatureIndex(Protocol):
    """Every thread the engine has started: features, initiatives and releases."""

    def add(self, feature_id: str, product_id: str, title: str, seq: int, kind: str = "feature",
            workspace: str = "") -> None: ...
    def next_number(self, product_id: str, kind: str = "feature") -> int: ...
    def all(self) -> list[dict]: ...


class InMemoryIndex:
    def __init__(self) -> None:
        self._rows: list[dict] = []
        self._lock = threading.Lock()

    def add(self, feature_id, product_id, title, seq, kind="feature", workspace="") -> None:
        with self._lock:
            self._rows.append({"feature_id": feature_id, "product_id": product_id, "title": title, "seq": seq,
                               "kind": kind, "workspace": workspace})

    def next_number(self, product_id, kind="feature") -> int:
        with self._lock:
            return 1 + max((r["seq"] for r in self._rows if r["product_id"] == product_id and r["kind"] == kind),
                           default=0)

    def all(self) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._rows]


class PostgresIndex:
    DDL = """
    CREATE TABLE IF NOT EXISTS valxel_features (
      feature_id TEXT PRIMARY KEY, product_id TEXT NOT NULL, title TEXT NOT NULL,
      seq INT NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    );
    """

    def __init__(self, pool) -> None:
        self.pool = pool
        with self.pool.connection() as conn:
            conn.execute(self.DDL)
            # Initiatives and releases share the index (added after features; existing rows are features).
            conn.execute("ALTER TABLE valxel_features ADD COLUMN IF NOT EXISTS kind TEXT NOT NULL DEFAULT 'feature'")
            # The thread's workspace, for row-level security (valxel_sdlc.tenancy).
            conn.execute("ALTER TABLE valxel_features ADD COLUMN IF NOT EXISTS workspace TEXT NOT NULL DEFAULT ''")

    def add(self, feature_id, product_id, title, seq, kind="feature", workspace="") -> None:
        with self.pool.connection() as conn:
            conn.execute("INSERT INTO valxel_features (feature_id, product_id, title, seq, kind, workspace) "
                         "VALUES (%s, %s, %s, %s, %s, %s)", (feature_id, product_id, title, seq, kind, workspace))

    def next_number(self, product_id, kind="feature") -> int:
        from ..tenancy import PLATFORM, bound

        # Numbers are shared across workspaces (one INCEPTION series): count every thread, read nothing else.
        with bound(PLATFORM), self.pool.connection() as conn:
            row = conn.execute("SELECT COALESCE(MAX(seq), 0) + 1 AS n FROM valxel_features "
                               "WHERE product_id = %s AND kind = %s", (product_id, kind)).fetchone()
        return int(row["n"])

    def all(self) -> list[dict]:
        with self.pool.connection() as conn:
            rows = conn.execute("SELECT feature_id, product_id, title, seq, kind FROM valxel_features "
                                "ORDER BY created_at").fetchall()
        return [dict(r) for r in rows]


def openchoreo_auth():
    """The tool's OpenChoreo client-credentials token, for OpenChoreo's agents (when configured)."""
    from ..secret_ring import get

    url, secret = os.environ.get("VALXEL_OPENCHOREO_TOKEN_URL", ""), get("VALXEL_OPENCHOREO_CLIENT_SECRET")
    if not (url and secret):
        return None
    from ..integrations.mcp_client import ClientCredentials

    return ClientCredentials(url, os.environ.get("VALXEL_OPENCHOREO_CLIENT_ID") or "service_mcp_client", secret,
                             scope=os.environ.get("VALXEL_OPENCHOREO_SCOPE", ""))


RAIL = [("discovery", "phase"), ("g1", "gate"), ("spec", "phase"), ("g2", "gate"), ("design", "phase"),
        ("g3", "gate"), ("architecture", "phase"), ("g4", "gate"), ("plan", "phase"), ("build", "phase"),
        ("g5", "gate"), ("release", "phase"), ("g6", "gate"), ("operate", "phase")]


WAIT_STATION = {"ci": "build", "build": "build", "dev": "build", "staging": "release", "production": "operate",
                "rollout": "operate", "flag_step": "operate", "release_train": "g6", "fix_window": "g6"}
EXPEDITED = ("discovery", "g1", "spec", "g2", "design", "g3", "architecture", "g4")


def rail(s: dict, pending: list[dict], *, running: bool) -> list[dict]:
    """Each station's state for the UI: done, skipped, waiting (on a human), current, stopped or todo."""
    status = s.get("status")
    phase_status, approvals = s.get("phase_status") or {}, s.get("approvals") or {}
    waiting_gates = {p.get("gate") for p in pending if p["type"] in ("gate", "handback")}
    waiting_phases = {p.get("phase") for p in pending if p["type"] == "escalation"}
    if "provisioning" in waiting_phases:
        waiting_phases.add("plan")
    if "delivery" in waiting_phases:  # a failed build or deploy: show it on the release station
        waiting_phases.add("release")
    for p in pending:  # blocking findings: show them where the scan ran
        if p["type"] == "escalation" and p.get("phase") == "security":
            waiting_phases.add({"pr": "build", "build": "build", "dev": "build", "staging": "release",
                                "production": "operate"}.get(p.get("stage"), "build"))
    merged = (s.get("change_set") or {}).get("merged")
    from ..profiles import get as _profile

    prof = _profile(s.get("profile"))
    skipped = set(prof.skips) | set(prof.skips_gates)
    stations = []
    for key, kind in RAIL:
        if kind == "gate":
            if key in waiting_gates:
                st = "waiting"
            elif approvals.get(key):
                st = "done"
            elif key == "g5" and merged:
                st = "skipped"  # low-risk change sets auto-merge
            else:
                st = "todo"
        else:
            ps = phase_status.get(key)
            if key in waiting_phases:
                st = "waiting"
            elif ps in ("passed", "resolved"):
                st = "done"
            else:
                st = "todo"
        if phase_status.get("design") == "skipped" and key in ("design", "g3"):
            st = "skipped"  # nothing people see changes (or G2 said so)
        if s.get("track") == "hotfix" and key in EXPEDITED:
            st = "skipped"  # an incident's hotfix starts at planning
        elif key in skipped:
            st = "skipped"  # the workflow profile leaves it out
        stations.append({"key": key, "kind": kind, "state": st})
    for p in pending:  # CI or OpenChoreo: work in progress, not a person's turn
        if p["type"] == "external":
            st = next((x for x in stations if x["key"] == (p.get("phase") or WAIT_STATION.get(p.get("wait"), "build"))), None)
            if st:
                st["state"] = "current"
    if running and not any(x["state"] in ("waiting", "current") for x in stations):
        nxt = next((x for x in stations if x["state"] == "todo"), None)
        if nxt:
            nxt["state"] = "current"
    if status == "aborted":
        nxt = next((x for x in stations if x["state"] in ("todo", "waiting")), None)
        if nxt:
            nxt["state"] = "stopped"
    return stations

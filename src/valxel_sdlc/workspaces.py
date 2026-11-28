"""Workspaces, members, roles and approver groups (the sign-in design, part A1).

A workspace is one customer: its products (the registry's `workspace` field, which also names its
model-provider workspace), its members and its approver groups. Members are profiles an admin
creates and invites; a person matches one by their GitHub login or verified email when they sign in.

    roles      owner > admin > approver > member > viewer
    groups     named sets of members (Product, Design, Security, Release, On-call, ...)
    gates      per product, which groups approve each gate: approvers "group:<workspace>/<group>"
               added to the product's own (product.yaml), versioned, and pinned by each feature
               when it starts (pins["groups"]), as governance is pinned by commit
    support    per product, which groups may open its agent traces in MLflow (valxel_sdlc.support):
               none until an admin grants it, versioned
    audit      every change: who, when, what

Membership is read on every request, so a member removed from a group can't approve on their next
click. Self-hosted installs need none of this: product.yaml's GitHub teams work as before.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import threading
import uuid
from dataclasses import replace
from datetime import UTC, datetime
from typing import Protocol

from . import append_only

ROLES = ("viewer", "member", "approver", "admin", "owner")
GATES = ("g0", "g1", "g2", "g3", "g4", "g5", "g6", "security", "oncall")
DEFAULT_GROUPS = ("Product", "Design", "Architecture", "Security", "Release", "On-call", "Support")
STATUSES = ("invited", "active", "suspended")


class WorkspaceError(ValueError):
    pass


def now() -> str:
    return datetime.now(UTC).isoformat()


# --- the audit log's hash chain: one per workspace, each entry carrying the hash of the one before ---

GENESIS = "0" * 64
_CHAINED = ("workspace", "at", "actor", "action", "detail")


def stamp(at) -> str:
    return at.astimezone(UTC).isoformat() if hasattr(at, "astimezone") else str(at)


def digest(prev: str, entry: dict) -> str:
    body = json.dumps({k: entry.get(k) for k in _CHAINED}, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256((prev + body).encode()).hexdigest()


def chained(prev: str, entry: dict) -> dict:
    return {**entry, "prev_hash": prev, "hash": digest(prev, entry)}


def verify_chain(entries: list[dict]) -> dict:
    """Oldest first. {"ok", "entries", "unchained" (written before the chain), "broken_at" (index)}.
    Unchained entries may only come before the first chained one."""
    prev, unchained = GENESIS, 0
    for i, e in enumerate(entries):
        if e.get("hash") is None:
            if prev != GENESIS:
                return {"ok": False, "entries": len(entries), "unchained": unchained, "broken_at": i}
            unchained += 1
            continue
        if e.get("prev_hash") != prev or e["hash"] != digest(prev, e):
            return {"ok": False, "entries": len(entries), "unchained": unchained, "broken_at": i}
        prev = e["hash"]
    return {"ok": True, "entries": len(entries), "unchained": unchained, "broken_at": None}


def slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
    if not s:
        raise WorkspaceError("A name needs letters or digits")
    return s[:40]


def rank(role: str) -> int:
    return ROLES.index(role) if role in ROLES else -1


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class WorkspaceStore(Protocol):
    def get(self, kind: str, workspace: str, key: str) -> dict | None: ...
    def put(self, kind: str, workspace: str, key: str, data: dict) -> None: ...
    def delete(self, kind: str, workspace: str, key: str) -> None: ...
    def list(self, kind: str, workspace: str | None = None) -> list[dict]: ...
    def audit(self, workspace: str, actor: str, action: str, detail: dict) -> None: ...
    def audit_log(self, workspace: str, limit: int = 200) -> list[dict]: ...  # newest first
    def audit_chain(self, workspace: str) -> list[dict]: ...  # oldest first, for verify_chain


class InMemoryWorkspaceStore:
    def __init__(self) -> None:
        self._rows: dict[tuple[str, str, str], dict] = {}
        self._audit: list[dict] = []
        self._lock = threading.Lock()

    def get(self, kind, workspace, key):
        with self._lock:
            r = self._rows.get((kind, workspace, key))
            return json.loads(json.dumps(r)) if r else None

    def put(self, kind, workspace, key, data):
        with self._lock:
            self._rows[(kind, workspace, key)] = json.loads(json.dumps(data))

    def delete(self, kind, workspace, key):
        with self._lock:
            self._rows.pop((kind, workspace, key), None)

    def list(self, kind, workspace=None):
        with self._lock:
            return [json.loads(json.dumps(v)) for (k, w, _), v in sorted(self._rows.items())
                    if k == kind and (workspace is None or w == workspace)]

    def audit(self, workspace, actor, action, detail):
        with self._lock:
            mine = [a for a in self._audit if a["workspace"] == workspace]
            self._audit.append(chained(mine[-1]["hash"] if mine else GENESIS, {
                "workspace": workspace, "at": now(), "actor": actor, "action": action,
                "detail": json.loads(json.dumps(detail))}))
        if getattr(self, "on_audit", None):  # mirrored to the audit sink (valxel_sdlc.audit_events)
            self.on_audit(workspace, actor, action, detail)

    def audit_log(self, workspace, limit=200):
        with self._lock:
            return [dict(a) for a in reversed(self._audit) if a["workspace"] == workspace][:limit]

    def audit_chain(self, workspace):
        with self._lock:
            return [dict(a) for a in self._audit if a["workspace"] == workspace]


class PostgresWorkspaceStore:
    DDL = (
        """CREATE TABLE IF NOT EXISTS valxel_workspace_rows (
             kind TEXT NOT NULL, workspace TEXT NOT NULL, key TEXT NOT NULL, data JSONB NOT NULL,
             updated TIMESTAMPTZ NOT NULL DEFAULT now(), PRIMARY KEY (kind, workspace, key))""",
        """CREATE TABLE IF NOT EXISTS valxel_workspace_audit (
             id BIGSERIAL PRIMARY KEY, workspace TEXT NOT NULL, at TIMESTAMPTZ NOT NULL DEFAULT now(),
             actor TEXT NOT NULL, action TEXT NOT NULL, detail JSONB NOT NULL)""",
        "CREATE INDEX IF NOT EXISTS valxel_workspace_audit_ws ON valxel_workspace_audit (workspace, id)",
        # Rows written before the chain (T-114) have none; verify reports them as unchained.
        "ALTER TABLE valxel_workspace_audit ADD COLUMN IF NOT EXISTS prev_hash TEXT",
        "ALTER TABLE valxel_workspace_audit ADD COLUMN IF NOT EXISTS hash TEXT",
        *append_only.statements("valxel_workspace_audit"),
    )

    def __init__(self, pool) -> None:
        self.pool = pool
        with pool.connection() as conn:
            for stmt in self.DDL:
                conn.execute(stmt)

    def get(self, kind, workspace, key):
        with self.pool.connection() as conn:
            row = conn.execute("SELECT data FROM valxel_workspace_rows WHERE kind = %s AND workspace = %s AND key = %s",
                               (kind, workspace, key)).fetchone()
        return row["data"] if row else None

    def put(self, kind, workspace, key, data):
        with self.pool.connection() as conn:
            conn.execute("INSERT INTO valxel_workspace_rows (kind, workspace, key, data) VALUES (%s, %s, %s, %s::jsonb) "
                         "ON CONFLICT (kind, workspace, key) DO UPDATE SET data = EXCLUDED.data, updated = now()",
                         (kind, workspace, key, json.dumps(data)))

    def delete(self, kind, workspace, key):
        with self.pool.connection() as conn:
            conn.execute("DELETE FROM valxel_workspace_rows WHERE kind = %s AND workspace = %s AND key = %s",
                         (kind, workspace, key))

    def list(self, kind, workspace=None):
        with self.pool.connection() as conn:
            return [r["data"] for r in conn.execute(
                "SELECT data FROM valxel_workspace_rows WHERE kind = %(kind)s"
                " AND (%(ws)s::text IS NULL OR workspace = %(ws)s) ORDER BY workspace, key",
                {"kind": kind, "ws": workspace}).fetchall()]

    def audit(self, workspace, actor, action, detail):
        with self.pool.connection() as conn, conn.transaction():
            # One writer per workspace at a time keeps its chain linear across replicas.
            conn.execute("SELECT pg_advisory_xact_lock(hashtext('valxel_workspace_audit:' || %s))", (workspace,))
            last = conn.execute("SELECT hash FROM valxel_workspace_audit WHERE workspace = %s AND hash IS NOT NULL "
                                "ORDER BY id DESC LIMIT 1", (workspace,)).fetchone()
            row = chained(last["hash"] if last else GENESIS, {
                "workspace": workspace, "at": now(), "actor": actor, "action": action,
                "detail": json.loads(json.dumps(detail))})
            conn.execute("INSERT INTO valxel_workspace_audit (workspace, at, actor, action, detail, prev_hash, hash) "
                         "VALUES (%s, %s, %s, %s, %s::jsonb, %s, %s)",
                         (workspace, row["at"], actor, action, json.dumps(row["detail"]), row["prev_hash"], row["hash"]))
        if getattr(self, "on_audit", None):  # mirrored to the audit sink (valxel_sdlc.audit_events)
            self.on_audit(workspace, actor, action, detail)

    _NEWEST = ("SELECT at, actor, action, detail, prev_hash, hash FROM valxel_workspace_audit WHERE workspace = %s "
               "ORDER BY id DESC LIMIT %s")
    _OLDEST = ("SELECT at, actor, action, detail, prev_hash, hash FROM valxel_workspace_audit WHERE workspace = %s "
               "ORDER BY id ASC LIMIT %s")

    def _rows(self, workspace, newest_first, limit=None):
        with self.pool.connection() as conn:
            rows = conn.execute(self._NEWEST if newest_first else self._OLDEST,
                                (workspace, limit or 10_000_000)).fetchall()
        return [{"workspace": workspace, "at": stamp(r["at"]), "actor": r["actor"], "action": r["action"],
                 "detail": r["detail"], "prev_hash": r["prev_hash"], "hash": r["hash"]} for r in rows]

    def audit_log(self, workspace, limit=200):
        return self._rows(workspace, True, limit)

    def audit_chain(self, workspace):
        return self._rows(workspace, False)


class Workspaces:
    """Workspaces, members, groups and gate mappings over a store, with every change audited."""

    def __init__(self, store: WorkspaceStore, registry: dict) -> None:
        self.store = store
        self.registry = registry  # product id -> RegistryEntry (its .workspace)
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ workspaces

    def workspace_of(self, product_id: str) -> str:
        e = self.registry.get(product_id)
        return e.workspace if e else ""

    def products(self, workspace: str) -> list[str]:
        return sorted(pid for pid, e in self.registry.items() if e.workspace == workspace)

    def get(self, workspace: str) -> dict | None:
        return self.store.get("workspace", workspace, "")

    def create(self, workspace: str, *, name: str, actor: str, owner: dict | None = None, plan: str = "lite",
               self_serve: bool = False) -> dict:
        """A workspace with the default approver groups; `owner` ({name, email, github_login}) becomes
        its owner and joins every group, so a solo founder approves every gate."""
        if self.get(workspace):
            raise WorkspaceError(f"Workspace {workspace} already exists")
        # self_serve: made by sign-up, so its tools connect from its connections page and a feature waits
        # for them (valxel_sdlc.connections); a deployment's own workspaces were connected by hand.
        ws = {"id": workspace, "name": name.strip() or workspace, "plan": plan, "created": now(), "created_by": actor,
              **({"self_serve": True} if self_serve else {})}
        self.store.put("workspace", workspace, "", ws)
        for g in DEFAULT_GROUPS:
            self.store.put("group", workspace, slug(g), {"workspace": workspace, "slug": slug(g), "name": g, "members": []})
        self.store.audit(workspace, actor, "workspace.create", {"name": ws["name"], "plan": plan})
        if owner:
            m = self._new_member(workspace, owner, role="owner", actor=actor, status="active")
            for g in DEFAULT_GROUPS:
                self.set_group(workspace, slug(g), members=[*self.group(workspace, slug(g))["members"], m["id"]],
                               actor=actor)
        return ws

    def set_plan(self, workspace: str, plan: str, *, actor: str) -> dict:
        """A hosted workspace moves between Lite and Full (valxel_sdlc.billing). Self-hosted isn't a
        hosted workspace's plan: that's installing Valxel yourself."""
        ws = self.get(workspace)
        if not ws:
            raise WorkspaceError(f"No workspace {workspace}")
        if plan not in ("lite", "full"):
            raise WorkspaceError("A hosted workspace's plan is lite or full")
        if ws.get("plan") == "self-hosted":
            raise WorkspaceError("This workspace runs on your own install; its plan doesn't change here")
        if ws.get("plan") != plan:
            ws = {**ws, "plan": plan}
            self.store.put("workspace", workspace, "", ws)
            self.store.audit(workspace, actor, "workspace.plan", {"plan": plan})
        return ws

    def set_cloud_accounts(self, workspace: str, value: str, *, cloud: str, actor: str) -> dict:
        """The workspace's two shared accounts (cloud/onboarding.py), where every product that shares
        runs: 'non-production: <id>, production: <id>, region: <region>', or '' to clear them."""
        from .cloud import onboarding

        ws = self.get(workspace)
        if not ws:
            raise WorkspaceError(f"No workspace {workspace}")
        if not value.strip():
            ws = {k: v for k, v in ws.items() if k != "cloud_accounts"}
        else:
            got, why = onboarding.parse(cloud, value)
            if not got:
                raise WorkspaceError(why)
            ws = {**ws, "cloud_accounts": {**got["accounts"], "region": got["region"]}}
        self.store.put("workspace", workspace, "", ws)
        self.store.audit(workspace, actor, "workspace.cloud_accounts", {"cloud_accounts": ws.get("cloud_accounts")})
        return ws

    def ensure(self, workspace: str, actor: str = "valxel") -> dict:
        """The workspace, created (no owner) the first time a product names it: existing deployments
        become workspaces with nothing else changed."""
        ws = self.get(workspace)
        if ws is None:
            return self.create(workspace, name=workspace, actor=actor, plan="self-hosted")
        for g in DEFAULT_GROUPS:  # a default group added since the workspace was made (Support)
            if self.group(workspace, slug(g)) is None:
                self.store.put("group", workspace, slug(g), {"workspace": workspace, "slug": slug(g), "name": g,
                                                             "members": []})
                self.store.audit(workspace, actor, "group.create", {"group": slug(g), "default": True})
        return ws

    def bootstrap_owner(self, workspace: str, who: dict, *, actor: str = "valxel-cli") -> dict:
        """An owner for a workspace that has none (an existing deployment's): created if missing, in
        every approver group. Run from the command line by whoever runs the tool."""
        self.ensure(workspace, actor)
        if self._owners(workspace):
            raise WorkspaceError(f"{workspace} already has an owner: they add people from its settings page")
        m = self._new_member(workspace, who, role="owner", actor=actor, status="active")
        for g in self.groups(workspace):
            self.set_group(workspace, g["slug"], members=[*g["members"], m["id"]], actor=actor)
        return m

    # ------------------------------------------------------------------ members

    def members(self, workspace: str) -> list[dict]:
        return [self._public(m) for m in self.store.list("member", workspace)]

    def member(self, workspace: str, member_id: str) -> dict | None:
        m = self.store.get("member", workspace, member_id)
        return self._public(m) if m else None

    @staticmethod
    def _public(m: dict) -> dict:
        return {k: v for k, v in m.items() if k != "invite_hash"}

    def _new_member(self, workspace: str, who: dict, *, role: str, actor: str, status: str,
                    products: list[str] | None = None) -> dict:
        if role not in ROLES:
            raise WorkspaceError(f"role must be one of {list(ROLES)}")
        email = str(who.get("email") or "").strip().lower()
        login = str(who.get("github_login") or "").strip().lstrip("@")
        if not (email or login):
            raise WorkspaceError("A profile needs an email or a GitHub login")
        if email and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
            raise WorkspaceError(f"{email} isn't an email address")
        for m in self.store.list("member", workspace):
            if (email and m.get("email") == email) or (login and m.get("github_login", "").lower() == login.lower()):
                raise WorkspaceError(f"{email or login} is already a member")
        bad = [p for p in products or [] if self.workspace_of(p) != workspace]
        if bad:
            raise WorkspaceError(f"{', '.join(bad)} isn't in this workspace")
        m = {"id": uuid.uuid4().hex[:12], "workspace": workspace, "name": str(who.get("name") or email or login).strip(),
             "email": email, "github_login": login, "role": role, "products": sorted(products or []),
             "status": status, "invited_by": actor, "created": now(), "updated": now()}
        self.store.put("member", workspace, m["id"], m)
        self.store.audit(workspace, actor, "member.add", {"member": m["id"], "name": m["name"], "role": role,
                                                          "status": status})
        return m

    def invite(self, workspace: str, who: dict, *, role: str, actor: str, products: list[str] | None = None) -> tuple[dict, str]:
        """A profile in the invited state and its one-time invitation token (sent by email; only
        its hash is kept). Only an owner makes another owner."""
        if role == "owner":
            raise WorkspaceError("Make someone owner by transferring ownership, not by invitation")
        m = self._new_member(workspace, who, role=role, actor=actor, status="invited", products=products)
        token = secrets.token_urlsafe(24)
        self.store.put("member", workspace, m["id"], {**self.store.get("member", workspace, m["id"]),
                                                      "invite_hash": _hash(token)})
        return m, token

    def resend(self, workspace: str, member_id: str, *, actor: str) -> str:
        m = self._require(workspace, member_id)
        if m["status"] != "invited":
            raise WorkspaceError(f"{m['name']} has already accepted")
        token = secrets.token_urlsafe(24)
        self.store.put("member", workspace, member_id, {**m, "invite_hash": _hash(token), "updated": now()})
        self.store.audit(workspace, actor, "member.reinvite", {"member": member_id})
        return token

    def accept(self, workspace: str, token: str, *, login: str, email: str = "") -> dict:
        """The signed-in person accepts: the invitation must be theirs (its GitHub login or email)."""
        for m in self.store.list("member", workspace):
            if m.get("status") == "invited" and m.get("invite_hash") and secrets.compare_digest(m["invite_hash"], _hash(token)):
                mine = (m.get("github_login") and m["github_login"].lower() == login.lower()) or (
                    email and m.get("email") == email.lower())
                if not mine:
                    raise PermissionError("This invitation is for someone else: sign in as them")
                m = {**{k: v for k, v in m.items() if k != "invite_hash"}, "status": "active", "updated": now(),
                     "github_login": m.get("github_login") or login}
                self.store.put("member", workspace, m["id"], m)
                self.store.audit(workspace, login, "member.accept", {"member": m["id"]})
                return m
        raise WorkspaceError("This invitation isn't valid any more: ask an admin to resend it")

    def _require(self, workspace: str, member_id: str) -> dict:
        m = self.store.get("member", workspace, member_id)
        if not m:
            raise WorkspaceError(f"No member {member_id}")
        return m

    def update_member(self, workspace: str, member_id: str, *, actor: str, actor_role: str, role: str | None = None,
                      products: list[str] | None = None, status: str | None = None) -> dict:
        m = self._require(workspace, member_id)
        if m["role"] == "owner" and actor_role != "owner":
            raise PermissionError("Only an owner can change an owner")
        if role is not None:
            if role not in ROLES:
                raise WorkspaceError(f"role must be one of {list(ROLES)}")
            if role == "owner" and actor_role != "owner":
                raise PermissionError("Only an owner can make another owner")
            if m["role"] == "owner" and role != "owner" and self._owners(workspace) <= 1:
                raise WorkspaceError("A workspace keeps at least one owner")
            m["role"] = role
        if products is not None:
            bad = [p for p in products if self.workspace_of(p) != workspace]
            if bad:
                raise WorkspaceError(f"{', '.join(bad)} isn't in this workspace")
            m["products"] = sorted(products)
        if status is not None:
            if status not in ("active", "suspended") or m["status"] == "invited":
                raise WorkspaceError("Suspend or reactivate an accepted member")
            if m["role"] == "owner" and status == "suspended" and self._owners(workspace) <= 1:
                raise WorkspaceError("A workspace keeps at least one owner")
            m["status"] = status
        m["updated"] = now()
        self.store.put("member", workspace, member_id, m)
        self.store.audit(workspace, actor, "member.update", {"member": member_id, "role": role, "products": products,
                                                              "status": status})
        return self._public(m)

    def remove_member(self, workspace: str, member_id: str, *, actor: str, actor_role: str) -> None:
        m = self._require(workspace, member_id)
        if m["role"] == "owner" and (actor_role != "owner" or self._owners(workspace) <= 1):
            raise WorkspaceError("A workspace keeps at least one owner; only an owner removes another")
        self.store.delete("member", workspace, member_id)
        for g in self.groups(workspace):
            if member_id in g["members"]:
                self.set_group(workspace, g["slug"], members=[x for x in g["members"] if x != member_id], actor=actor)
        self.store.audit(workspace, actor, "member.remove", {"member": member_id, "name": m["name"]})

    def _owners(self, workspace: str) -> int:
        return sum(1 for m in self.store.list("member", workspace) if m["role"] == "owner" and m["status"] == "active")

    # ------------------------------------------------------------------ who someone is

    def memberships(self, login: str, email: str = "") -> list[dict]:
        """The person's active memberships, in every workspace (by GitHub login or verified email)."""
        login, email = login.lower(), email.lower()
        return [self._public(m) for m in self.store.list("member")
                if m.get("status") == "active" and ((login and m.get("github_login", "").lower() == login)
                                                    or (email and m.get("email") == email))]

    def groups_of(self, member_ids: set[str], workspaces: set[str]) -> list[str]:
        """"<workspace>/<group>" for each group the members are in."""
        return sorted(f"{g['workspace']}/{g['slug']}" for ws in workspaces for g in self.groups(ws)
                      if member_ids & set(g["members"]))

    # ------------------------------------------------------------------ groups

    def groups(self, workspace: str) -> list[dict]:
        return self.store.list("group", workspace)

    def group(self, workspace: str, group: str) -> dict | None:
        return self.store.get("group", workspace, group)

    def create_group(self, workspace: str, name: str, *, actor: str) -> dict:
        s = slug(name)
        if self.group(workspace, s):
            raise WorkspaceError(f"A group {name} already exists")
        g = {"workspace": workspace, "slug": s, "name": name.strip(), "members": []}
        self.store.put("group", workspace, s, g)
        self.store.audit(workspace, actor, "group.create", {"group": s})
        return g

    def set_group(self, workspace: str, group: str, *, members: list[str], actor: str) -> dict:
        g = self.group(workspace, group)
        if not g:
            raise WorkspaceError(f"No group {group}")
        known = {m["id"] for m in self.store.list("member", workspace)}
        missing = [m for m in members if m not in known]
        if missing:
            raise WorkspaceError(f"Not members of this workspace: {', '.join(missing)}")
        g["members"] = sorted(set(members))
        self.store.put("group", workspace, group, g)
        self.store.audit(workspace, actor, "group.members", {"group": group, "members": g["members"]})
        return g

    def delete_group(self, workspace: str, group: str, *, actor: str) -> None:
        used = [pid for pid in self.products(workspace)
                if any(group in gs for gs in (self.mapping(pid) or {}).get("gates", {}).values())]
        if used:
            raise WorkspaceError(f"{group} approves gates on {', '.join(used)}: change those first")
        traces = [pid for pid in self.products(workspace) if group in self.support(pid).get("mlflow_groups", [])]
        if traces:
            raise WorkspaceError(f"{group} may open agent traces on {', '.join(traces)}: change that first")
        self.store.delete("group", workspace, group)
        self.store.audit(workspace, actor, "group.delete", {"group": group})

    # ------------------------------------------------------------------ gate mappings

    def mapping(self, product_id: str, version: int | None = None) -> dict | None:
        """The product's gate-to-group mapping: a given version (a feature's pin) or the latest."""
        ws = self.workspace_of(product_id)
        versions = [m for m in self.store.list("gates", ws) if m["product"] == product_id]
        if not versions:
            return None
        if version is None:
            return max(versions, key=lambda m: m["version"])
        return next((m for m in versions if m["version"] == version), None)

    def set_mapping(self, product_id: str, gates: dict[str, list[str]], *, actor: str) -> dict:
        ws = self.workspace_of(product_id)
        if not ws:
            raise WorkspaceError(f"Unknown product {product_id}")
        unknown = sorted(set(gates) - set(GATES))
        if unknown:
            raise WorkspaceError(f"Unknown gates {unknown}: use {list(GATES)}")
        have = {g["slug"] for g in self.groups(ws)}
        missing = sorted({g for gs in gates.values() for g in gs} - have)
        if missing:
            raise WorkspaceError(f"No such groups: {', '.join(missing)}")
        with self._lock:
            cur = self.mapping(product_id)
            m = {"product": product_id, "version": (cur["version"] + 1) if cur else 1,
                 "gates": {g: sorted(set(v)) for g, v in gates.items() if v}, "by": actor, "at": now()}
            self.store.put("gates", ws, f"{product_id}:{m['version']:06d}", m)
        self.store.audit(ws, actor, "gates.set", {"product": product_id, "version": m["version"], "gates": m["gates"]})
        return m

    # ------------------------------------------------------------------ support access

    def support(self, product_id: str) -> dict:
        """The product's support settings, latest version: which groups may open its agent traces
        in MLflow (prompts and tool data, for products whose data class allows content). Empty until an
        admin grants it."""
        ws = self.workspace_of(product_id)
        versions = [m for m in self.store.list("support", ws) if m.get("product") == product_id] if ws else []
        return max(versions, key=lambda m: m["version"]) if versions else {"product": product_id, "mlflow_groups": [],
                                                                            "version": 0}

    def set_support(self, product_id: str, *, mlflow_groups: list[str], actor: str) -> dict:
        ws = self.workspace_of(product_id)
        if not ws:
            raise WorkspaceError(f"Unknown product {product_id}")
        have = {g["slug"] for g in self.groups(ws)}
        missing = sorted(set(mlflow_groups) - have)
        if missing:
            raise WorkspaceError(f"No such groups: {', '.join(missing)}")
        with self._lock:
            cur = self.support(product_id)
            m = {"product": product_id, "version": cur["version"] + 1, "mlflow_groups": sorted(set(mlflow_groups)),
                 "by": actor, "at": now()}
            self.store.put("support", ws, f"{product_id}:{m['version']:06d}", m)
        self.store.audit(ws, actor, "support.set", {"product": product_id, "version": m["version"],
                                                    "mlflow_groups": m["mlflow_groups"]})
        return m

    def approvers(self, product_id: str, version: int | None = None) -> dict[str, list[str]]:
        """Gate -> ["group:<workspace>/<group>", ...] from the mapping (pinned version or latest)."""
        m = self.mapping(product_id, version)
        ws = self.workspace_of(product_id)
        return {g: [f"group:{ws}/{s}" for s in gs] for g, gs in (m or {}).get("gates", {}).items()}

    def merge(self, snap, version: int | None):
        """A governance snapshot whose product approvers include the mapping's groups, and whose pins
        record the mapping version, so a feature started now keeps it."""
        extra = self.approvers(snap.product.product_id, version)
        m = self.mapping(snap.product.product_id, version)
        pins = {**snap.pins, "groups": m["version"] if m else 0}
        if not extra:
            return replace(snap, pins=pins)
        approvers = {k: list(v) for k, v in snap.product.approvers.items()}
        for gate, names in extra.items():
            approvers[gate] = [*approvers.get(gate, []), *[n for n in names if n not in approvers.get(gate, [])]]
        return replace(snap, pins=pins, product=replace(snap.product, approvers=approvers))

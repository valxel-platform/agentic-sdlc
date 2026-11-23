"""T-114: the workspace audit log and the change log can't be rewritten, and tampering shows."""

import os
import uuid

import pytest

from valxel_sdlc import append_only
from valxel_sdlc.workspaces import InMemoryWorkspaceStore, verify_chain

DB = os.environ.get("VALXEL_TEST_DATABASE_URL")
pg = pytest.mark.skipif(not DB, reason="set VALXEL_TEST_DATABASE_URL to run Postgres tests")


def test_each_workspace_has_its_own_chain_and_an_edit_breaks_it():
    s = InMemoryWorkspaceStore()
    for i in range(3):
        s.audit("acme", "ann", "member.add", {"member": f"m{i}"})
        s.audit("beta", "bob", "member.add", {"member": f"m{i}"})
    chain = s.audit_chain("acme")
    assert verify_chain(chain) == {"ok": True, "entries": 3, "unchained": 0, "broken_at": None}
    assert chain[0]["prev_hash"] == "0" * 64 and chain[1]["prev_hash"] == chain[0]["hash"]
    chain[1]["detail"] = {"member": "someone-else"}
    assert verify_chain(chain)["broken_at"] == 1
    removed = s.audit_chain("acme")
    del removed[1]
    assert verify_chain(removed) == {"ok": False, "entries": 2, "unchained": 0, "broken_at": 1}


def test_entries_from_before_the_chain_are_allowed_only_at_the_start():
    s = InMemoryWorkspaceStore()
    s.audit("acme", "ann", "workspace.create", {})
    old = [{"workspace": "acme", "at": "2026-01-01T00:00:00+00:00", "actor": "ann", "action": "x", "detail": {},
            "prev_hash": None, "hash": None}]
    assert verify_chain(old + s.audit_chain("acme"))["unchained"] == 1
    assert verify_chain(s.audit_chain("acme") + old)["ok"] is False


def test_only_table_names_get_ddl():
    with pytest.raises(ValueError):
        append_only.statements("x; DROP TABLE y")


@pg
def test_postgres_refuses_to_change_delete_or_truncate_audit_and_change_rows():
    import psycopg
    from psycopg.rows import dict_row
    from psycopg_pool import ConnectionPool

    from valxel_sdlc.ops import changes
    from valxel_sdlc.workspaces import PostgresWorkspaceStore

    with ConnectionPool(DB, kwargs={"autocommit": True, "row_factory": dict_row}, open=True) as pool:
        ws = f"t114-{uuid.uuid4().hex[:8]}"  # a database a previous run left rows in
        store = PostgresWorkspaceStore(pool)
        PostgresWorkspaceStore(pool)  # a second start installs nothing twice
        log = changes.PostgresChangeLog(pool)
        store.audit(ws, "ann", "member.add", {"member": "m1", "n": 1.5})
        store.audit(ws, "ann", "member.remove", {"member": "m1"})
        log.record(changes.new(key=ws, product_id="p", kind="flag", type="standard", action="off",
                               summary="off", targets=[]))
        assert verify_chain(store.audit_chain(ws))["ok"]
        assert store.audit_log(ws, 1)[0]["action"] == "member.remove"
        for sql in ("UPDATE valxel_workspace_audit SET actor = 'mallory' WHERE workspace = %s",
                    "DELETE FROM valxel_workspace_audit WHERE workspace = %s",
                    "TRUNCATE valxel_workspace_audit",
                    "UPDATE valxel_changes SET data = '{}'::jsonb",
                    "DELETE FROM valxel_changes",
                    "TRUNCATE valxel_changes CASCADE"):
            with pool.connection() as conn, pytest.raises(psycopg.errors.InsufficientPrivilege, match="append-only"):
                conn.execute(sql, (ws,) if "%s" in sql else None)
        assert len(store.audit_chain(ws)) == 2
        # Past the trigger (its owner dropped it): the chain still shows the edit.
        with pool.connection() as conn:
            conn.execute("ALTER TABLE valxel_workspace_audit DISABLE TRIGGER valxel_workspace_audit_append_only")
            try:
                conn.execute("UPDATE valxel_workspace_audit SET actor = 'mallory' WHERE workspace = %s", (ws,))
            finally:
                conn.execute("ALTER TABLE valxel_workspace_audit ENABLE TRIGGER valxel_workspace_audit_append_only")
        assert verify_chain(store.audit_chain(ws))["broken_at"] == 0


def test_the_cli_reports_each_workspace_and_fails_on_an_altered_one(capsys):
    from valxel_sdlc.cli import _verify_audit
    from valxel_sdlc.ops.changes import InMemoryChangeLog

    s = InMemoryWorkspaceStore()
    for ws in ("acme", "beta"):
        s.put("workspace", ws, "", {"id": ws})
        s.audit(ws, "ann", "workspace.create", {})
    assert _verify_audit(s, InMemoryChangeLog()) == 0
    assert "workspace acme: 1 entries intact" in capsys.readouterr().out
    s._audit[1]["actor"] = "mallory"
    assert _verify_audit(s, InMemoryChangeLog()) == 1
    assert "workspace beta: ALTERED at entry 1" in capsys.readouterr().out


def test_admins_can_check_the_chain(engine):
    from fastapi.testclient import TestClient

    from valxel_sdlc.api.app import create_app
    from valxel_sdlc.web.auth import AuthConfig

    with TestClient(create_app(engine, auth=AuthConfig(mode="dev", secret="x" * 40), web_dist=None)) as c:
        c.post("/api/auth/dev-login", json={"login": "founder", "email": "f@t114.example", "orgs": ["x"]})
        assert c.post("/api/workspaces", json={"id": "t114-ws", "name": "T114"}).status_code == 201
        r = c.get("/api/workspaces/t114-ws/audit/verify")
        assert r.status_code == 200 and r.json()["ok"] is True and r.json()["entries"] >= 1
        c.post("/api/auth/dev-login", json={"login": "stranger", "email": "s@else.example", "orgs": ["x"]})
        assert c.get("/api/workspaces/t114-ws/audit/verify").status_code in (403, 404)

"""Row-level security by workspace (valxel_sdlc.tenancy), against real Postgres with a login that is
neither a superuser nor BYPASSRLS, and owns the tables, as the tool's login does. Skipped without a DB."""

import os
import uuid

import pytest

from valxel_sdlc import tenancy
from valxel_sdlc.tenancy import PLATFORM, bound

DB = os.environ.get("VALXEL_TEST_DATABASE_URL")
needs_db = pytest.mark.skipif(not DB, reason="set VALXEL_TEST_DATABASE_URL to run Postgres tests")
CLINIC, SHARED, DENTAL = "wrkspc-clinic", "wrkspc-shared", "wrkspc-dental"


@pytest.fixture
def tenant_dsn():
    """A database owned by an ordinary login: the tool's login in production owns its tables."""
    import psycopg
    from psycopg import sql
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    name = f"valxel_rls_{uuid.uuid4().hex[:8]}"
    with psycopg.connect(DB, autocommit=True) as admin:
        admin.execute(sql.SQL("create role {} login password 'rls' nosuperuser nobypassrls").format(sql.Identifier(name)))
        admin.execute(sql.SQL("create database {} owner {}").format(sql.Identifier(name), sql.Identifier(name)))
    info = conninfo_to_dict(DB)
    yield make_conninfo(**{**info, "dbname": name, "user": name, "password": "rls"})
    with psycopg.connect(DB, autocommit=True) as admin:
        admin.execute(sql.SQL("drop database if exists {} with (force)").format(sql.Identifier(name)))
        admin.execute(sql.SQL("drop role if exists {}").format(sql.Identifier(name)))


def _pool(dsn):
    from psycopg.rows import dict_row

    return tenancy.make_pool(dsn, max_size=4, kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row})


@needs_db
def test_each_workspace_sees_only_its_rows_and_an_unscoped_query_sees_none(tenant_dsn, settings):
    import psycopg

    from valxel_sdlc.engine import PostgresIndex

    with _pool(tenant_dsn) as pool:
        with bound(PLATFORM):
            index = PostgresIndex(pool)
            assert set(tenancy.apply(pool, settings.registry, enforce=True).values()) >= {"on"}
            index.add("CLN-FEAT-1", "CLN", "Consent", 1, workspace=CLINIC)
            index.add("FARM-FEAT-1", "FARM", "Harvest", 1, workspace=SHARED)
            with pool.connection() as conn:
                assert tenancy.bypasses(conn) == ""
        with bound(CLINIC):
            assert [r["feature_id"] for r in index.all()] == ["CLN-FEAT-1"]
            with pytest.raises(psycopg.errors.InsufficientPrivilege):  # writing into another workspace
                index.add("FARM-FEAT-2", "FARM", "Sneaky", 2, workspace=SHARED)
        with bound([CLINIC, SHARED]):
            assert len(index.all()) == 2
        assert index.all() == []  # no scope: nothing
        with bound(PLATFORM):
            assert index.next_number("CLN") == 2
        with bound(DENTAL):  # numbering counts every workspace's threads, so ids never collide
            assert index.next_number("CLN") == 2 and index.all() == []


@needs_db
def test_turning_it_off_opens_the_tables_again(tenant_dsn, settings):
    from valxel_sdlc.engine import PostgresIndex

    with _pool(tenant_dsn) as pool:
        with bound(PLATFORM):
            index = PostgresIndex(pool)
            tenancy.apply(pool, settings.registry, enforce=True)
            index.add("CLN-FEAT-1", "CLN", "Consent", 1, workspace=CLINIC)
            assert set(tenancy.apply(pool, settings.registry, enforce=False).values()) <= {"off", "missing"}
        assert len(index.all()) == 1


@needs_db
def test_a_feature_runs_in_its_own_workspace_and_no_other_can_read_it(tenant_dsn, settings):
    from langgraph.checkpoint.postgres import PostgresSaver

    from valxel_sdlc.engine import FeatureEngine, PostgresIndex
    from valxel_sdlc.gateway import PostgresLedger
    from valxel_sdlc.services import build_services

    with _pool(tenant_dsn) as pool:
        with bound(PLATFORM):
            saver = PostgresSaver(pool)
            saver.setup()
            engine = FeatureEngine(build_services(settings, ledger=PostgresLedger(pool)), saver, PostgresIndex(pool))
            tenancy.apply(pool, settings.registry, enforce=True)
        with bound(CLINIC):  # someone in the clinic's workspace starts and approves it
            fid = engine.start(product_id="CLN", title="Consent check")["feature_id"]
            v = engine.auto_drive(fid)
            assert v["status"] == "done" and engine.services.ledger.records(fid)
        with bound(DENTAL):
            assert engine.graph.get_state(engine._config(fid)).values == {}  # checkpoints: invisible
            assert engine.services.ledger.records(fid) == [] and engine.index.all() == []
        with bound(CLINIC):
            assert engine._state(fid)["status"] == "done"
        assert engine.workspace_of(fid) == CLINIC


def test_each_request_runs_in_the_signed_in_persons_workspaces(engine):
    from fastapi.testclient import TestClient

    from valxel_sdlc.api.app import create_app
    from valxel_sdlc.web.auth import AuthConfig

    app = create_app(engine, auth=AuthConfig(mode="dev", secret="x" * 40), web_dist=None)
    app.add_api_route("/api/_scope", lambda: {"scope": tenancy.scope.get(), "who": tenancy.principal.get()})
    with TestClient(app) as c:
        assert c.get("/api/_scope").json() == {"scope": PLATFORM, "who": ""}  # nobody signed in
        c.post("/api/auth/dev-login", json={"login": "nurse", "teams": [], "orgs": ["acme-clinic"]})
        assert c.get("/api/_scope").json() == {"scope": CLINIC, "who": "nurse"}
        c.post("/api/auth/dev-login", json={"login": "pm", "teams": [], "orgs": ["acme-farm", "acme-shop"]})
        assert c.get("/api/_scope").json()["scope"] == SHARED  # FARM and SHOP share a workspace


@needs_db
def test_the_server_works_end_to_end_with_row_level_security_on(tenant_dsn, tmp_path, monkeypatch):
    """The app as deployed: its engine on Postgres as an ordinary login, VALXEL_TENANCY=rls."""
    import time

    from fastapi.testclient import TestClient

    from valxel_sdlc.api.app import create_app
    from valxel_sdlc.web.auth import AuthConfig

    for k, v in {"DATABASE_URL": tenant_dsn, "VALXEL_TENANCY": "rls", "VALXEL_MODE": "stub",
                 "VALXEL_ARTIFACT_DIR": str(tmp_path / "a"), "VALXEL_GOVERNANCE_SOURCE": "local",
                 "VALXEL_WATCH_INTERVAL_S": "3600"}.items():
        monkeypatch.setenv(k, v)
    teams = ["acme-clinic/product-owners", "acme-clinic/architects", "acme-clinic/design"]

    def pending(c, fid):
        for _ in range(200):
            v = c.get(f"/api/features/{fid}").json()
            if not v.get("running") and v.get("pending"):
                return [p["gate"] for p in v["pending"]]
            time.sleep(0.05)
        raise AssertionError(f"{fid} never stopped at a gate")

    with TestClient(create_app(auth=AuthConfig(mode="dev", secret="x" * 40), web_dist=None)) as c:
        c.post("/api/auth/dev-login", json={"login": "maria", "teams": teams, "orgs": ["acme-clinic"]})
        r = c.post("/api/features", json={"product_id": "CLN", "title": "Consent check on intake"})
        assert r.status_code == 201, r.text
        fid = r.json()["feature_id"]
        assert pending(c, fid) == ["g1"]
        [item] = [i for i in c.get("/api/inbox?mine=true").json() if i["feature_id"] == fid]
        r = c.post(f"/api/features/{fid}/decisions/{item['interrupt_id']}", json={"decision": "approve"})
        assert r.status_code == 202, r.text
        assert pending(c, fid) == ["g2"]

        c.post("/api/auth/dev-login", json={"login": "dentist", "teams": [], "orgs": ["acme-dental"]})
        assert [f for f in c.get("/api/features").json() if f["feature_id"] == fid] == []
        assert c.get(f"/api/features/{fid}").status_code in (403, 404)

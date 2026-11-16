import os

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from valxel_sdlc.config import load_settings
from valxel_sdlc.engine import FeatureEngine
from valxel_sdlc.services import build_services


def pytest_configure(config):
    """With pytest -n (pytest-xdist), each worker gets a fresh database of its own, made beside
    VALXEL_TEST_DATABASE_URL's: the Postgres tests expect to be alone in theirs. Runs before the test
    modules are imported, so the ones that read the variable at import see the worker's."""
    worker, url = os.environ.get("PYTEST_XDIST_WORKER"), os.environ.get("VALXEL_TEST_DATABASE_URL")
    if not (worker and url):
        return
    import psycopg
    from psycopg import sql
    from psycopg.conninfo import make_conninfo

    name = f"valxel_test_{worker}"
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name)))
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    os.environ["VALXEL_TEST_DATABASE_URL"] = make_conninfo(url, dbname=name)


@pytest.fixture
def settings(tmp_path):
    return load_settings(mode="stub", database_url="", artifact_dir=tmp_path / "artifacts", governance_source="local")


@pytest.fixture
def services(settings):
    return build_services(settings)


@pytest.fixture
def engine(services):
    return FeatureEngine(services, InMemorySaver())


def pending_of(engine, feature_id, kind=None):
    return [p for p in engine.pending(feature_id) if kind is None or p.get("gate") == kind or p["type"] == kind]


def approve(engine, feature_id, gate, **extra):
    [p] = [p for p in engine.pending(feature_id) if p.get("gate") == gate]
    return engine.decide(feature_id, p["interrupt_id"], {"decision": "approve", "approver": f"{gate}-owner", **extra})

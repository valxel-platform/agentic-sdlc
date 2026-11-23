import os

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from valxel_sdlc.config import load_settings
from valxel_sdlc.engine import FeatureEngine
from valxel_sdlc.services import build_services


def pytest_addoption(parser):
    parser.addoption("--shard", default="", help="Run one share of the tests, e.g. 2/3 (CI splits the suite "
                                                 "across parallel jobs)")


def pytest_collection_modifyitems(config, items):
    """--shard i/n keeps every n-th test, starting at the i-th, in collection order: dealt round-robin,
    so the slow end-to-end tests spread evenly instead of landing in one share."""
    shard = config.getoption("shard")
    if not shard:
        return
    i, n = (int(x) for x in shard.split("/"))
    if not 1 <= i <= n:
        raise ValueError(f"--shard {shard}: want i/n with 1 <= i <= n")
    keep = [it for k, it in enumerate(items) if k % n == i - 1]
    config.hook.pytest_deselected(items=[it for it in items if it not in keep])
    items[:] = keep


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
    from valxel_sdlc.secret_scrub import ScrubbingSerde, keep_excerpts

    return FeatureEngine(services, keep_excerpts(InMemorySaver(serde=ScrubbingSerde())))  # as production stores it


def pending_of(engine, feature_id, kind=None):
    return [p for p in engine.pending(feature_id) if kind is None or p.get("gate") == kind or p["type"] == kind]


def approve(engine, feature_id, gate, **extra):
    [p] = [p for p in engine.pending(feature_id) if p.get("gate") == gate]
    return engine.decide(feature_id, p["interrupt_id"], {"decision": "approve", "approver": f"{gate}-owner", **extra})


def one_sender_is_enough(root):
    """For tests about what the tool does once it may act alone: their alerts come from one sender,
    and the platform asks for two (remediation.yaml min_sources). test_remediation covers that rule."""
    p = root / "valxel-platform" / "sdlc-governance" / "platform" / "remediation.yaml"
    p.write_text(p.read_text().replace("min_sources: 2", "min_sources: 1", 1))
    return root

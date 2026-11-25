"""The release rehearsal (valxel_sdlc.rehearsal): naming a report, reading SQL, what passes, the
sidecar that copies the database through the cloud's port and publishes the report, and the steps
end to end against a real Postgres."""

import importlib
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from valxel_sdlc import rehearsal as R
from valxel_sdlc.cloud import fakes

DB = os.environ.get("VALXEL_TEST_DATABASE_URL", "")
CURRENT = """# a comment
apiVersion: v1
kind: ConfigMap
data:
  # the tool
  sdlc_tool_image: ghcr.io/valxel-platform/agentic-sdlc:main@sha256:aaa
  llm_gateway_image: ghcr.io/valxel-shared-services/ss-llm-gateway:main@sha256:bbb
"""
CANDIDATE = CURRENT.replace("sha256:aaa", "sha256:ccc")


@pytest.fixture
def work(tmp_path, monkeypatch):
    monkeypatch.setattr(R, "WORK", tmp_path / "work")
    monkeypatch.setattr(R, "FILES", tmp_path / "files")
    (tmp_path / "work").mkdir()
    (tmp_path / "files").mkdir()
    (tmp_path / "files" / "current.yaml").write_text(CURRENT)
    (tmp_path / "files" / "candidate.yaml").write_text(CANDIDATE)
    return tmp_path / "work"


def test_a_report_is_named_by_its_images():
    reordered = CURRENT.replace("  # the tool\n", "").replace("# a comment", "# another")
    assert R.key(CURRENT, CANDIDATE) == R.key(reordered, CANDIDATE)
    assert R.key(CURRENT, CANDIDATE) != R.key(CURRENT, CURRENT) != R.key(CANDIDATE, CURRENT)


def test_reads_the_sql_not_prose(tmp_path):
    (tmp_path / "store.py").write_text(
        'DDL = ("CREATE TABLE IF NOT EXISTS valxel_x (id TEXT);'
        ' ALTER TABLE valxel_x ADD COLUMN IF NOT EXISTS n INT",)\n'
        'NOT_SQL = "Create a table of contents"\n'
        't = "valxel_y"\nQ = f"CREATE INDEX IF NOT EXISTS {t}_i ON {t} (a)"\n')
    assert R.statements(tmp_path) == ["ALTER TABLE valxel_x ADD COLUMN IF NOT EXISTS n INT",
                                      "CREATE INDEX IF NOT EXISTS {}_i ON {} (a)",
                                      "CREATE TABLE IF NOT EXISTS valxel_x (id TEXT)"]


def test_reads_the_tools_own_schema():
    import valxel_sdlc

    found = R.statements(Path(valxel_sdlc.__file__).parent)
    assert len(found) > 30 and all(R.STATEMENT.match(s) for s in found)


def test_passes_only_when_every_step_did(work):
    for s in R.STEPS:
        R.record(s, True, time.time())
    assert R.report()["passed"]
    R.record("smoke-current-again", False, time.time())
    assert not R.report()["passed"]


def test_steps_that_never_ran_fail_it_and_the_sql_diff_is_reported(work):
    (work / "sql-current.txt").write_text("CREATE TABLE IF NOT EXISTS a (x INT)\n")
    (work / "sql-candidate.txt").write_text(
        "CREATE TABLE IF NOT EXISTS a (x INT)\nCREATE INDEX IF NOT EXISTS i ON a (x)\n")
    R.record("copy", True, time.time())
    got = R.report()
    assert not got["passed"] and "smoke-current" in got["missing"]
    assert got["schema_changed"] and got["sql_added"] == ["CREATE INDEX IF NOT EXISTS i ON a (x)"]


def test_the_driver_runs_where_only_the_standard_library_is(work):
    # The old image runs this file as a script: importing it must need nothing new.
    R.install()
    # -S: no site-packages, so not even valxel_sdlc; loading the file must still work.
    out = subprocess.run([sys.executable, "-I", "-S", str(work / "rehearsal.py"), "no-such-step"],
                         capture_output=True, text=True, env={"PATH": os.environ.get("PATH", "")}, check=False)
    assert out.returncode == 2 and "valxel rehearse serve" in out.stderr, out.stderr


def _serve(monkeypatch, env: dict, copies, reports):
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    cloud = importlib.import_module("valxel_sdlc.cloud")
    monkeypatch.setattr(cloud, "database_copies", lambda env=None: copies)
    monkeypatch.setattr(cloud, "report_store", lambda bucket, env=None, client=None: reports)
    import socket

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        monkeypatch.setattr(R, "PORT", probe.getsockname()[1])
    stop = threading.Event()
    t = threading.Thread(target=R.serve, kwargs={"stop": stop})
    t.start()
    return stop, t


def test_the_sidecar_copies_hands_out_the_url_only_on_loopback_then_deletes_and_reports(work, monkeypatch):
    copies, reports = fakes.MemoryCopies(), fakes.MemoryReports()
    stop, t = _serve(monkeypatch, {"REHEARSAL_SOURCE": "tool-db", "REHEARSAL_BUCKET": "reports"}, copies, reports)
    try:
        assert R.copy() == 0
        code, got = R._sidecar("/database")
        made = copies.copies["valxel-rehearsal-sdlc"]
        assert code == 200 and got["url"].startswith(f"postgresql://postgres:{made['password']}@{made['host']}:5432/")
        _, status = R._sidecar("/status")
        assert "url" not in status and made["password"] not in json.dumps(status)  # status carries no secret
        assert made["password"] not in (work / "steps.jsonl").read_text()  # nor does the record
    finally:
        stop.set()
        t.join(10)
    assert copies.deleted == ["valxel-rehearsal-sdlc"] and not copies.copies
    [(key, (body, _))] = reports.objects.items()
    report = json.loads(body)
    assert key == f"rehearsals/{R.key(CURRENT, CANDIDATE)}.json" and report["copy_deleted"] is True
    assert not report["passed"] and report["steps"][0]["step"] == "copy"


def test_a_failed_copy_fails_the_copy_step_and_is_still_reported(work, monkeypatch):
    copies, reports = fakes.MemoryCopies(sources={}), fakes.MemoryReports()
    stop, t = _serve(monkeypatch, {"REHEARSAL_SOURCE": "missing", "REHEARSAL_BUCKET": "reports"}, copies, reports)
    try:
        assert R.copy() == 1
    finally:
        stop.set()
        t.join(10)
    report = json.loads(next(iter(reports.objects.values()))[0])
    assert report["steps"][0]["ok"] is False and "missing" in report["steps"][0]["error"]
    assert "copy_deleted" not in report  # nothing was made


@pytest.mark.skipif(not DB, reason="needs VALXEL_TEST_DATABASE_URL")
def test_the_steps_against_postgres(work, monkeypatch):
    # current and candidate are this tree here; the rehearsal's point is that they're two images.
    import psycopg

    name = f"rehearsal_{os.getpid()}"
    with psycopg.connect(DB, autocommit=True) as conn:
        conn.execute(f"DROP DATABASE IF EXISTS {name}")
        conn.execute(f"CREATE DATABASE {name}")
    from psycopg.conninfo import make_conninfo

    url = make_conninfo(DB, dbname=name)  # under xdist, DB is key=value conninfo, not a URL
    stop, t = _serve(monkeypatch, {"REHEARSAL_DATABASE_URL": url, "REHEARSAL_WORKER_S": "2"}, None, None)
    try:
        assert R.sql("current") == 0 and R.sql("candidate") == 0
        assert R.copy() == 0
        assert R.smoke("current") == 0 and R.smoke("candidate") == 0 and R.smoke("current-again") == 0
        assert R.done() == 0
    finally:
        stop.set()
        t.join(10)
        with psycopg.connect(DB, autocommit=True) as conn:
            conn.execute(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")
    assert json.loads((work / "report.json").read_text())["passed"]

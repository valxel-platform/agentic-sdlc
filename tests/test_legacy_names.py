"""Deployments set up before the rename keep working: old settings, old tables, old alert labels."""

from __future__ import annotations

import os

import pytest

from valxel_sdlc import legacy
from valxel_sdlc.ops.signals import from_alertmanager


def test_old_settings_still_work_and_new_ones_win():
    env = {"VRIXEL_MODE": "live", "VRIXEL_AUTH": "github", "VALXEL_AUTH": "dev", "PATH": "/bin"}
    assert legacy.environment(env) == ["VALXEL_MODE"]
    assert env["VALXEL_MODE"] == "live" and env["VALXEL_AUTH"] == "dev"


def test_alerts_labelled_with_the_old_name_find_their_product():
    payload = {"alerts": [{"status": "firing", "labels": {"alertname": "HighErrors", "vrixel_product": "CLN",
                                                          "severity": "critical"},
                           "annotations": {}, "startsAt": "2026-11-12T00:00:00Z", "fingerprint": "f1"}]}
    assert from_alertmanager(payload)[0]["product_id"] == "CLN"


@pytest.mark.skipif(not os.environ.get("VALXEL_TEST_DATABASE_URL"), reason="needs Postgres")
def test_old_tables_are_renamed_once_and_keep_their_rows():
    import psycopg

    with psycopg.connect(os.environ["VALXEL_TEST_DATABASE_URL"], autocommit=True) as conn:
        for t in ("valxel_legacy_probe", "vrixel_legacy_probe"):
            conn.execute(f"DROP TABLE IF EXISTS {t}")
        conn.execute("CREATE TABLE vrixel_legacy_probe (id serial primary key, v text)")
        conn.execute("CREATE INDEX vrixel_legacy_probe_v ON vrixel_legacy_probe (v)")
        conn.execute("INSERT INTO vrixel_legacy_probe (v) VALUES ('kept')")
        done = legacy.rename_tables(conn)
        assert "vrixel_legacy_probe -> valxel_legacy_probe" in done and "vrixel_legacy_probe_v -> valxel_legacy_probe_v" in done
        assert conn.execute("SELECT v FROM valxel_legacy_probe").fetchone()[0] == "kept"
        assert legacy.rename_tables(conn) == []  # nothing left to do
        conn.execute("DROP TABLE valxel_legacy_probe")

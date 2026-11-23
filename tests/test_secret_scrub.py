"""Secrets never reach a feature's stored history (T-121): the checkpointer's serializer scrubs them,
the same length, so what's stored stays valid and the archive (checkpoint_archive.py) never has one."""

from __future__ import annotations

import pickle
import time
from datetime import UTC

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

from valxel_sdlc import sandbox
from valxel_sdlc.engine import FeatureEngine
from valxel_sdlc.secret_scrub import ScrubbingSerde, found, scrub, scrub_bytes

# Made-up values in the real shapes (as ss-llm-gateway's redaction corpus).
GH = "ghs_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"
PAT = "github_pat_" + "11ABCDEFG0123456789_abcdefghijklmnopqrstuvwxyz"
AWS = "AKIA" + "IOSFODNN7EXAMPLE"
ANT = "sk-ant-" + "api03-abcdefghijklmnopqrstuvwxyz012345"
JWT = "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJhbm5hIiwiZXhwIjoxfQ.c2lnbmF0dXJlLXZhbHVlLWhlcmU"
KEY = ("-----BEGIN RSA PRIVATE KEY-----\n"
       "MIIEpAIBAAKCAQEAu1SU1LfVLPHCozMxH2Mo4lgOEePzNm0tRgeLezV6ffAt0gun\n-----END RSA PRIVATE KEY-----")
ALL = (GH, PAT, AWS, ANT, JWT, KEY)


def test_every_shape_is_found_and_scrubbed_to_the_same_length():
    for secret in ALL:
        text = f"before {secret} after"
        assert found(text), secret
        out = scrub(text)
        assert len(out) == len(text) and secret not in out and out.startswith("before ") and out.endswith(" after")
        assert out[7:11] == secret[:4]  # what kind of thing it was stays readable
    assert found("no secrets in a sentence about ghs tokens or sk- prefixes") == []


def test_serialized_state_stays_valid_without_the_secrets():
    serde = ScrubbingSerde(JsonPlusSerializer())
    state = {"summary": f"used {GH} to push", "notes": [f"key: {KEY}", {"ci": f"AWS {AWS} failed"}],
             "count": 3, "unicode": "café ✓"}
    kind, data = serde.dumps_typed(state)
    assert not any(s.encode() in data for s in ALL)
    back = serde.loads_typed((kind, data))
    assert back["count"] == 3 and back["unicode"] == "café ✓"
    assert back["summary"].startswith("used ghs_xxxx") and len(back["summary"]) == len(state["summary"])
    assert back["notes"][1]["ci"].startswith("AWS AKIAxxxx")
    assert scrub_bytes(b"plain bytes") == b"plain bytes"


def test_a_features_stored_history_holds_no_secret(services):
    saver = InMemorySaver(serde=ScrubbingSerde())
    engine = FeatureEngine(services, saver)
    fid = engine.create(product_id="FARM", title="Harvest log",
                        description=f"The CI log showed {GH} and {AWS}; the fixture had {KEY}.")
    stored = pickle.dumps((dict(saver.storage), dict(saver.blobs), dict(saver.writes)))
    assert not any(s.encode() in stored for s in ALL)
    assert "ghs_xxxx" in engine.view(fid)["description"]  # the record says what was there, not what it was


def test_leftover_session_folders_and_checkouts_are_swept(tmp_path, monkeypatch):
    monkeypatch.setenv("VALXEL_SANDBOX_DIR", str(tmp_path))
    old, fresh = tmp_path / "abc123", tmp_path / "def456"
    co = tmp_path / "checkouts" / "FARM-FEAT-1-architecture"
    for d in (old, fresh, co):
        (d / "work").mkdir(parents=True)
        (d / "work" / "main.py").write_text("print('customer code')")
    (old / ".token").write_text(GH)
    (co / "work" / "main.py").chmod(0o444)  # checkouts are read-only
    t = time.time()
    for d in (old, co):
        import os

        os.utime(d, (t - 7 * 3600, t - 7 * 3600))
    assert sandbox.sweep(now=t) == 2
    assert not old.exists() and not co.exists() and fresh.exists()
    assert sandbox.sweep(now=t) == 0


def test_notes_and_eval_cases_in_postgres_hold_no_secret():
    import json
    import os
    import uuid

    import pytest

    url = os.environ.get("VALXEL_TEST_DATABASE_URL")
    if not url:
        pytest.skip("set VALXEL_TEST_DATABASE_URL")
    from psycopg.rows import dict_row

    from valxel_sdlc import tenancy
    from valxel_sdlc.cluster import PostgresCluster
    from valxel_sdlc.eval_case import EvalCase
    from valxel_sdlc.evals import PostgresEvalStore

    with tenancy.make_pool(url, min_size=1, max_size=2, kwargs={"autocommit": True, "prepare_threshold": 0,
                                                    "row_factory": dict_row}) as pool, tenancy.bound(tenancy.PLATFORM):
        key = f"t121:{uuid.uuid4().hex}"
        PostgresCluster(pool, url).put(key, {"step": f"git push https://x:{GH}@github.com/acme/api", "key": KEY})
        store = PostgresEvalStore(pool)
        store.capture(EvalCase(id=key, product_id="FARM", feature_id=key, phase="spec", data_class="general",
                               at="2026-11-22T00:00:00+00:00", config_version="t", ctx={}, status="ok",
                               output=f"use {ANT}"))
        with pool.connection() as conn:
            note = conn.execute("select value from valxel_notes where key = %s", (key,)).fetchone()["value"]
            case = conn.execute("select data from valxel_eval_cases where id = %s", (key,)).fetchone()["data"]
            conn.execute("delete from valxel_notes where key = %s", (key,))
            conn.execute("delete from valxel_eval_cases where id = %s", (key,))
        stored = json.dumps(note) + json.dumps(case)
        assert not any(s in stored for s in (GH, ANT)) and "MIIEpAIBAAKCAQEA" not in stored
        assert "ghs_xxxx" in stored


def _case(id_, at, label=None, product="FARM"):
    from valxel_sdlc.eval_case import EvalCase

    return EvalCase(id=id_, product_id=product, feature_id=id_, phase="spec", data_class="general", at=at,
                    config_version="t", ctx={}, status="ok", output="doc", label=label)


def test_eval_cases_are_deleted_after_their_window():
    from datetime import datetime

    from valxel_sdlc.evals import InMemoryEvalStore, retention_days

    assert retention_days({}) == (90, 730)
    now = datetime(2026, 11, 22, tzinfo=UTC)
    store = InMemoryEvalStore()
    store.capture(_case("new", "2026-11-01T00:00:00+00:00"))
    store.capture(_case("old-open", "2026-06-01T00:00:00+00:00"))
    store.capture(_case("old-labeled", "2026-06-01T00:00:00+00:00", {"decision": "approve"}))
    store.capture(_case("ancient-labeled", "2024-06-01T00:00:00+00:00", {"decision": "approve"}))
    assert store.prune(now=now) == 2
    assert sorted(c.id for c in store.cases(labeled=False)) == ["new", "old-labeled"]


def test_eval_cases_in_postgres_are_deleted_after_their_window():
    import os
    import uuid
    from datetime import datetime

    import pytest

    url = os.environ.get("VALXEL_TEST_DATABASE_URL")
    if not url:
        pytest.skip("set VALXEL_TEST_DATABASE_URL")
    from psycopg.rows import dict_row

    from valxel_sdlc import tenancy
    from valxel_sdlc.evals import PostgresEvalStore

    p = uuid.uuid4().hex
    now = datetime(2126, 11, 22, tzinfo=UTC)  # pruned for this test's own product only
    with tenancy.make_pool(url, min_size=1, max_size=2, kwargs={"autocommit": True, "prepare_threshold": 0,
                                                                "row_factory": dict_row}) as pool, \
            tenancy.bound(tenancy.PLATFORM):
        store = PostgresEvalStore(pool)
        store.capture(_case(f"{p}-new", "2126-11-01T00:00:00+00:00", product=p))
        store.capture(_case(f"{p}-open", "2126-06-01T00:00:00+00:00", product=p))
        store.capture(_case(f"{p}-labeled", "2126-06-01T00:00:00+00:00", {"decision": "approve"}, product=p))
        assert store.prune(now=now, product_id=p) == 1
        with pool.connection() as conn:
            left = {r["id"] for r in conn.execute("select id from valxel_eval_cases where id like %s", (f"{p}%",))}
            conn.execute("delete from valxel_eval_cases where id like %s", (f"{p}%",))
        assert left == {f"{p}-new", f"{p}-labeled"}


def test_notes_keep_an_excerpt_of_code_never_the_whole_of_it():
    from valxel_sdlc.secret_scrub import EXCERPT_LINES, NOTE_CHARS, excerpt

    code = "\n".join(f"line_{i} = {i}" for i in range(100))
    note = f"test_harvest failed:\n```python\n{code}\n```\nsee above"
    short = excerpt(note)
    assert "line_19 = 19" in short and "line_20 = 20" not in short
    assert f"({100 - EXCERPT_LINES} more lines not kept)" in short and short.endswith("```\nsee above")
    assert excerpt("```\na = 1\n```") == "```\na = 1\n```"  # a short block is kept as it is
    assert len(excerpt("x" * 10_000)) < NOTE_CHARS + 60


def test_a_features_history_keeps_excerpts_in_notes_and_documents_whole():
    from langgraph.checkpoint.base import empty_checkpoint

    from valxel_sdlc.secret_scrub import keep_excerpts

    code = "```python\n" + "\n".join(f"line_{i} = {i}" for i in range(100)) + "\n```"
    saver = keep_excerpts(InMemorySaver(serde=ScrubbingSerde()))
    config = {"configurable": {"thread_id": "t121", "checkpoint_ns": ""}}
    cp = empty_checkpoint()
    cp["channel_values"] = {"findings": {"build": [code]}, "draft": {"content": code}}
    cp["channel_versions"] = {"findings": 1, "draft": 1}
    config = saver.put(config, cp, {}, {"findings": 1, "draft": 1})
    saver.put_writes(config, [("ci_failures", [[code]]), ("draft", {"content": code})], "task-1")
    got = saver.get_tuple(config)
    assert "line_99" not in got.checkpoint["channel_values"]["findings"]["build"][0]
    assert "line_99" in got.checkpoint["channel_values"]["draft"]["content"]
    writes = {c: v for _, c, v in got.pending_writes}
    assert "line_99" not in writes["ci_failures"][0][0] and "line_99" in writes["draft"]["content"]

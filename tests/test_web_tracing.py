"""The browser's trace continues through the API and the runs it queues (web/src/telemetry.ts,
api/app.py, run_queue.py), and the browser's spans reach the trace store through the API."""

import io
import urllib.error

import pytest
from fastapi.testclient import TestClient
from test_api import SECRET, sign_in
from test_telemetry import traced

from valxel_sdlc.api.app import create_app, forward_spans
from valxel_sdlc.run_queue import Job, MemoryRunQueue, Worker
from valxel_sdlc.telemetry import (
    TelemetryConfig,
    current_ids,
    current_traceparent,
    joined,
)
from valxel_sdlc.web.auth import AuthConfig

BROWSER_TRACE = "4bf92f3577b34da6a3ce929d0e0e4736"
BROWSER = f"00-{BROWSER_TRACE}-00f067aa0ba902b7-01"


@pytest.fixture
def tracing(engine):
    tel, spans = traced()
    engine.services.telemetry = tel
    with TestClient(create_app(engine, auth=AuthConfig(mode="dev", secret=SECRET), web_dist=None)) as c:
        sign_in(c, "maya")
        yield c, spans


def test_an_api_request_continues_the_browser_s_trace(tracing):
    c, spans = tracing
    assert c.get("/api/me", headers={"traceparent": BROWSER}).json()["tracing"] is True
    [span] = [s for s in spans.get_finished_spans() if s.name == "GET /api/me"]
    assert format(span.context.trace_id, "032x") == BROWSER_TRACE
    assert format(span.parent.span_id, "016x") == "00f067aa0ba902b7"
    assert span.attributes["http.response.status_code"] == 200


def test_without_a_browser_trace_the_request_starts_one(tracing):
    c, spans = tracing
    c.get("/api/me")
    [span] = [s for s in spans.get_finished_spans() if s.name == "GET /api/me"]
    assert span.parent is None


def test_a_queued_run_continues_the_trace_that_queued_it():
    tel, _ = traced()
    q = MemoryRunQueue()
    with tel._tracer.start_as_current_span("POST /api/features"):
        queued_in = current_ids()[0]
        q.enqueue("FARM-1", {"go": 1})
    seen = []

    class Engine:
        def __init__(self):
            self.errors = {}

        def run_job(self, thread, payload, claim=None):
            seen.append(current_ids()[0])

    w = Worker(Engine(), q, concurrency=1)
    w.run_once()
    assert seen == [queued_in]
    assert current_traceparent() == ""  # nothing leaks past the run


def test_joined_is_a_no_op_without_a_traceparent():
    with joined(""):
        assert current_ids() == ("", "")
    assert Job(1, "t", "w", {}, 1).traceparent == ""


def test_the_browser_s_spans_go_through_the_api_for_signed_in_people_only(tracing, monkeypatch):
    c, _ = tracing
    sent = []
    monkeypatch.setattr("valxel_sdlc.api.app.forward_spans", lambda cfg, body, kind: sent.append((body, kind)) or 200)
    assert c.post("/otlp/v1/traces", content=b"{}", headers={"content-type": "application/json"}).status_code == 200
    assert sent == [(b"{}", "application/json")]
    assert c.post("/otlp/v1/traces", content=b"x", headers={"content-type": "text/plain"}).status_code == 415
    assert c.post("/otlp/v1/traces", content=b"x" * 1_000_001,
                  headers={"content-type": "application/json"}).status_code == 413
    c.cookies.clear()
    assert c.post("/otlp/v1/traces", content=b"{}", headers={"content-type": "application/json"}).status_code == 401


def test_no_trace_store_no_browser_spans(engine):
    with TestClient(create_app(engine, auth=AuthConfig(mode="dev", secret=SECRET), web_dist=None)) as c:
        sign_in(c, "maya")
        assert c.get("/api/me").json()["tracing"] is False
        assert c.post("/otlp/v1/traces", content=b"{}", headers={"content-type": "application/json"}).status_code == 404


def test_forward_spans_posts_to_the_trace_store_with_its_headers():
    got = {}

    class Resp(io.BytesIO):
        status = 200

    def opener(req, timeout=None):
        got.update(url=req.full_url, auth=req.headers.get("Authorization"), body=req.data)
        return Resp(b"")

    cfg = TelemetryConfig(endpoint="http://collector:4318", headers={"Authorization": "Bearer t"})
    assert forward_spans(cfg, b"spans", "application/x-protobuf", opener=opener) == 200
    assert got == {"url": "http://collector:4318/v1/traces", "auth": "Bearer t", "body": b"spans"}

    def down(req, timeout=None):
        raise urllib.error.URLError("refused")

    assert forward_spans(cfg, b"spans", "application/json", opener=down) == 502

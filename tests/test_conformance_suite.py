"""The template conformance suite (standard_files/conformance.py, T-14), run against a tiny app."""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

pytest.importorskip("jwt")
SUITE = Path(__file__).resolve().parents[1] / "src" / "valxel_sdlc" / "standard_files" / "conformance.py"
spec = importlib.util.spec_from_file_location("conformance", SUITE)
conformance = importlib.util.module_from_spec(spec)
spec.loader.exec_module(conformance)

APP = r'''
import json, os, sys, urllib.request, uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
GOOD = os.environ["GOOD"] == "1"
def post(url, body):
    urllib.request.urlopen(urllib.request.Request(url, data=body, method="POST",
                           headers={"content-type": "application/json"}), timeout=5).read()
class H(BaseHTTPRequestHandler):
    def do_GET(self):
        out = b"ok" if self.path == "/healthz" else (b"# TYPE valxel_events_total counter\n" if GOOD else b"")
        self.send_response(200); self.end_headers(); self.wfile.write(out)
    def do_POST(self):
        auth = self.headers.get("authorization", "")
        event = {"specversion": "1.0", "id": str(uuid.uuid4()), "source": "c/svc", "type": "valxel.audit.v1",
                 "time": "2026-11-18T00:00:00+00:00", "subject": "x/1", "data": {"service": "svc",
                 "action": "x.read", "actor": {"id": "u", "type": "user" if GOOD else "robot"},
                 "resource": {"type": "x", "ids": ["1"]}, "outcome": "success"}}
        print(json.dumps({"log.logger": "audit", "message": json.dumps(event)}), flush=True)
        post(os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"] + "/v1/traces", b"\x00")
        if GOOD:
            post(os.environ["FLAGS"] + "/ofrep/v1/evaluate/flags/demo", b"{}")
        self.send_response(200 if auth.startswith("Bearer ") else 401); self.end_headers(); self.wfile.write(b"{}")
    def log_message(self, *a): pass
HTTPServer(("127.0.0.1", int(os.environ["PORT"])), H).serve_forever()
'''


@pytest.fixture
def repo(tmp_path, monkeypatch):
    (tmp_path / "contracts" / "audit").mkdir(parents=True)
    # Enough of ss-audit-sink's schema to tell a good record from a bad one.
    schema = {"$schema": "https://json-schema.org/draft/2020-12/schema", "type": "object",
              "required": ["type", "data"], "properties": {"type": {"const": "valxel.audit.v1"}, "data": {
                  "type": "object", "properties": {"actor": {"type": "object", "properties": {
                      "type": {"enum": ["user", "service", "agent"]}}}}}}}
    (tmp_path / "contracts" / "audit" / "valxel.audit.v1.schema.json").write_text(json.dumps(schema))
    (tmp_path / "app.py").write_text(APP)
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _spec(port, good):
    return {"port": port, "env": {"PORT": str(port), "FLAGS": "{ofrep}", "GOOD": "1" if good else "0"},
            "steps": [{"method": "POST", "path": "/act", "token": {"scope": "x"}}], "settle_s": 0.5,
            "checks": ["audit", "tracing", "events", "flags"]}


def test_an_app_that_meets_the_contract_passes(repo):
    assert conformance.run(_spec(18731, True), cmd=f"{sys.executable} app.py") == []


def test_each_gap_is_named(repo):
    problems = conformance.run(_spec(18732, False), cmd=f"{sys.executable} app.py")
    assert any(p.startswith("audit: a record breaks the schema") and "actor/type" in p for p in problems)
    assert any(p.startswith("events:") for p in problems) and any(p.startswith("flags:") for p in problems)
    assert not any(p.startswith("tracing:") for p in problems)


def test_audit_records_are_found_in_json_log_lines():
    line = json.dumps({"message": json.dumps({"type": "valxel.audit.v1", "id": "1"})})
    assert conformance.audit_records("noise\n" + line + "\n{\"type\": \"valxel.audit.v1\"}") == [
        {"type": "valxel.audit.v1", "id": "1"}, {"type": "valxel.audit.v1"}]

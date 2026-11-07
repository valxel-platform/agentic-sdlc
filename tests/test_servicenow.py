"""ServiceNow change receipts: each production change becomes a closed change_request in the
organization's ServiceNow, found again by correlation id on a retry, never holding a release up."""

import io
import json
import shutil
from pathlib import Path
from urllib.error import HTTPError

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from vrixel_sdlc.config import load_settings
from vrixel_sdlc.engine import FeatureEngine
from vrixel_sdlc.governance.loader import GovernanceError
from vrixel_sdlc.ops import changes
from vrixel_sdlc.ops.servicenow import InMemoryReceipts, ServiceNowReceipts, WithReceipts, body_for
from vrixel_sdlc.services import build_services

EXAMPLES = Path(__file__).resolve().parents[1] / "examples" / "governance"
SPEC = {"provider": "servicenow", "instance": "https://acme.service-now.com", "assignment_group": "Platform Engineering",
        "types": {}, "close": {}}


class FakeSN:
    def __init__(self, existing=None, close_fails=False, post_fails=0):
        self.existing, self.close_fails, self.post_fails = existing or [], close_fails, post_fails
        self.calls = []

    def open(self, req, timeout=None):
        body = json.loads(req.data) if req.data else None
        self.calls.append((req.get_method(), req.full_url, body, dict(req.header_items())))
        m = req.get_method()
        if m == "GET":
            out = {"result": self.existing}
        elif m == "POST":
            if self.post_fails:
                self.post_fails -= 1
                raise HTTPError(req.full_url, 503, "busy", {}, io.BytesIO(b"{}"))
            out = {"result": {"sys_id": "abc123", "number": "CHG0030001"}}
        else:
            if self.close_fails:
                raise HTTPError(req.full_url, 403, "state model", {}, io.BytesIO(b"{}"))
            out = {"result": {}}
        data = io.BytesIO(json.dumps(out).encode())
        return type("R", (), {"__enter__": lambda s: data, "__exit__": lambda s, *a: None})()


def record(log=None, **kw):
    log = log or changes.InMemoryChangeLog()
    return log.record(changes.new(key=kw.get("key", "k1"), product_id="EMR", kind="release", type=kw.get("type", "standard"),
                                  action="deploy", summary="EMR-FEAT-1 Consent capture at intake",
                                  targets=[{"kind": "component", "name": "emr-intake-api", "release": "api-7"},
                                           {"kind": "flag", "name": "emr.f", "on": True, "percent": 5.0}],
                                  feature_id="EMR-FEAT-1", requested_by="dev",
                                  approved_by=["rm-1"], approvals=[{"gate": "g6", "approver": "rm-1", "at": "t"}],
                                  evidence={"pull_requests": ["https://github.com/vrixel-emr/emr-intake-api/pull/7"]},
                                  outcome=kw.get("outcome", "done")))


def receipts(http, store=None, spec=SPEC, auth="Basic dTpw"):
    return ServiceNowReceipts(lambda pid: spec, store or InMemoryReceipts(),
                              env={"VRIXEL_SERVICENOW_AUTH_EMR": auth} if auth else {}, opener=http, delays=(0, 0))


def test_the_receipt_says_what_changed_and_who_approved_it():
    b = body_for(record(), SPEC)
    assert b["type"] == "normal" and b["correlation_id"] == "vrixel:CHG-1" and b["correlation_display"] == "vrixel-sdlc"
    assert b["short_description"] == "[standard] EMR-FEAT-1 Consent capture at intake"
    assert "component emr-intake-api release api-7" in b["description"] and "for 5% of users" in b["description"]
    assert "G6 rm-1" in b["description"] and "pull/7" in b["description"] and b["assignment_group"] == "Platform Engineering"
    assert body_for(record(type="automatic"), SPEC)["type"] == "emergency"
    assert body_for(record(type="automatic"), {**SPEC, "types": {"automatic": "standard"}})["type"] == "standard"


def test_a_change_becomes_a_closed_change_request():
    http = FakeSN()
    r = receipts(http)
    rec = record()
    r.send(rec)
    r.flush()
    (get, url0, _, h), (post, url1, body, _), (patch, url2, closing, _) = http.calls
    assert (get, post, patch) == ("GET", "POST", "PATCH") and h["Authorization"] == "Basic dTpw"
    assert "correlation_id%3Dvrixel%3ACHG-1" in url0 and url1.endswith("/api/now/table/change_request?sysparm_input_display_value=true")
    assert url2.endswith("/change_request/abc123?sysparm_input_display_value=false")
    assert closing == {"state": "3", "close_code": "successful", "close_notes": f"Recorded by the Vrixel SDLC tool: {rec['summary']}"}
    assert r.store.get(["CHG-1"])["CHG-1"] | {"at": ""} == {"status": "closed", "number": "CHG0030001", "sys_id": "abc123",
                                                           "instance": SPEC["instance"], "change_id": "CHG-1", "at": ""}


def test_a_retry_finds_the_one_it_made_and_a_refused_close_stays_open():
    http = FakeSN(existing=[{"sys_id": "abc123", "number": "CHG0030001"}], close_fails=True)
    r = receipts(http)
    r.send(record(outcome="failed"))
    r.flush()
    assert [c[0] for c in http.calls] == ["GET", "PATCH"]  # no second change_request
    got = r.store.get(["CHG-1"])["CHG-1"]
    assert got["status"] == "open" and "created, not closed" in got["detail"]


def test_busy_servicenow_is_retried_and_missing_credentials_said_plainly():
    http = FakeSN(post_fails=1)
    r = receipts(http)
    r.send(record())
    r.flush()
    assert r.store.get(["CHG-1"])["CHG-1"]["status"] == "closed"
    nocred = receipts(FakeSN(), auth="")
    nocred.send(record())
    assert nocred.store.get(["CHG-1"])["CHG-1"] | {"at": ""} == {
        "status": "not_sent", "detail": "set VRIXEL_SERVICENOW_AUTH_EMR", "change_id": "CHG-1", "at": ""}


def test_only_new_records_are_sent_and_only_for_products_that_want_it():
    sent = []

    class Spy(ServiceNowReceipts):
        def send(self, rec):
            sent.append(rec["id"])
            self.store.put(rec["id"], {"status": "queued"})

    log = WithReceipts(changes.InMemoryChangeLog(), Spy(lambda pid: SPEC if pid == "EMR" else None, InMemoryReceipts()))
    record(log)
    record(log)  # the same change again
    log.record(changes.new(key="ag", product_id="AG", kind="release", type="standard", action="deploy", summary="s", targets=[]))
    assert sent == ["CHG-1"] and len(log.list()) == 2  # the log still has everything


def test_product_yaml_turns_it_on_and_refuses_what_it_cant_do(tmp_path):
    root = tmp_path / "g"
    shutil.copytree(EXAMPLES, root)
    gov = root / "vrixel-emr" / "emr-governance" / "product.yaml"
    base = gov.read_text()
    gov.write_text(base.replace("tools:\n", "tools:\n  change_management:\n    provider: servicenow\n"
                                           "    instance: https://acme.service-now.com\n"
                                           "    assignment_group: Platform Engineering\n", 1))
    s = load_settings(mode="stub", database_url="", artifact_dir=tmp_path / "a", governance_source="local", governance_root=root)
    eng = FeatureEngine(build_services(s), InMemorySaver())
    spec = eng.services.snapshot("EMR").product.tools["change_management"]
    assert spec["instance"] == "https://acme.service-now.com" and spec["mode"] == "receipt"
    eng.auto_drive(eng.create(product_id="EMR", title="Consent capture at intake"))
    got = eng.services.change_receipts.get(["CHG-1"])["CHG-1"]
    assert got["status"] == "not_sent"  # no credentials in this test; the release went out anyway
    for bad in ("    mode: gate\n", "    types: {standard: major}\n"):
        gov.write_text(base.replace("tools:\n", "tools:\n  change_management:\n    provider: servicenow\n"
                                               "    instance: https://acme.service-now.com\n" + bad, 1))
        eng2 = FeatureEngine(build_services(s), InMemorySaver())
        with pytest.raises(GovernanceError, match="change_management"):
            eng2.services.snapshot("EMR")

"""Where error reports may go (platform/error-tracking.yaml), and GlitchTip alerts as signals."""

import io
import json
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import InMemorySaver

from vrixel_sdlc.api.app import create_app
from vrixel_sdlc.cli import main as cli
from vrixel_sdlc.config import load_settings
from vrixel_sdlc.engine import FeatureEngine
from vrixel_sdlc.governance.loader import GovernanceError
from vrixel_sdlc.ops import error_tracking as et
from vrixel_sdlc.services import build_services
from vrixel_sdlc.web.auth import AuthConfig

EXAMPLES = Path(__file__).resolve().parents[1] / "examples" / "governance"
POLICY = et.load_policy((EXAMPLES / "vrixel-tools" / "sdlc-governance" / "platform" / "error-tracking.yaml").read_text())
TRACE = "4bf92f3577b34da6a3ce929d0e0e4736"


def rt(provider, region="us-east-2", **extra):
    return {"aws": {"region": region}, "error_tracking": {"provider": provider, **extra}}


@pytest.mark.parametrize("runtime, data_class, problem", [
    (rt("glitchtip", url="https://errors.pay.example.dev"), "payments", None),
    (rt("sentry"), "payments", "must be glitchtip (payments)"),
    (rt("sentry", region="ap-south-1"), "general", "must be glitchtip (india)"),
    (rt("glitchtip", region="ap-south-1", url="https://errors.cln.vrixel.in"), "phi", None),
    (rt("sentry"), "phi", "needs a signed BAA"),
    (rt("sentry", baa=True), "phi", None),
    (rt("sentry"), "general", None),
    (rt("glitchtip"), "general", "url must be GlitchTip's https address"),
    (rt("datadog"), "general", "provider must be one of"),
    ({"aws": {"region": "us-east-2"}}, "payments", None),  # no error tracking: nothing is sent
])
def test_the_platform_decides_where_error_reports_may_go(runtime, data_class, problem):
    problems = et.check(runtime, data_class, POLICY)
    assert (problem is None and problems == []) or (problem and any(problem in p for p in problems)), problems


def test_a_bad_policy_is_refused():
    with pytest.raises(et.PolicyError):
        et.load_policy("rules: [{name: x, when: {}, require: rollbar}]")
    assert et.load_policy("") == {}


@pytest.fixture
def engine(tmp_path):
    root = tmp_path / "governance"
    shutil.copytree(EXAMPLES, root)
    s = load_settings(mode="stub", database_url="", artifact_dir=tmp_path / "a", governance_source="local",
                      governance_root=root)
    return FeatureEngine(build_services(s), InMemorySaver()), root


def test_governance_refuses_a_product_that_breaks_the_rules(engine):
    eng, root = engine
    p = root / "acme-pay" / "pay-governance" / "product.yaml"
    p.write_text(p.read_text().replace("provider: glitchtip\n    url: https://errors.pay.example.dev", "provider: sentry"))
    eng.services.governance._cache.clear() if hasattr(eng.services.governance, "_cache") else None
    with pytest.raises(GovernanceError, match="must be glitchtip"):
        eng.services.snapshot("PAY")


ALERT = {"alias": "GlitchTip", "text": "GlitchTip Alert",
         "attachments": [{"title": "PSQLException: connection refused", "text": "LedgerRepository.post",
                          "title_link": "https://errors.pay.example.dev/acme-pay/issues/412",
                          "fields": [{"title": "Project", "value": "pay-ledger", "short": True},
                                     {"title": "Environment", "value": "production", "short": True},
                                     {"title": "Release", "value": "1.4.0", "short": True}]}]}


def test_a_glitchtip_alert_is_a_signal_and_learns_its_trace():
    [s] = et.from_glitchtip(ALERT, "PAY")
    assert (s["component"], s["env"], s["fingerprint"], s["resolves"]) == ("pay-ledger", "production", "glitchtip:PAY:412", False)
    assert et.from_glitchtip({"text": "GlitchTip Alert"}, "PAY") == []

    seen = []

    class Opener:
        def open(self, req, timeout=0):
            seen.append(req)
            return io.BytesIO(json.dumps({"tags": [{"key": "trace_id", "value": TRACE}, ["environment", "staging"],
                                                   {"key": "endpoint", "value": "POST /ledger/entries"}]}).encode())

    out = et.enrich(s, "https://errors.pay.example.dev/", "tok", Opener())
    assert out["labels"]["trace_id"] == TRACE and out["labels"]["endpoint"] == "POST /ledger/entries" and out["env"] == "staging"
    assert seen[0].full_url == "https://errors.pay.example.dev/api/0/issues/412/events/latest/"
    assert seen[0].headers["Authorization"] == "Bearer tok"
    assert et.enrich(s, "https://errors.pay.example.dev", "", Opener()) is s  # no token: as it came
    assert et.enrich({**s, "labels": {"issue": "../x"}}, "https://e.dev", "tok", Opener())["labels"] == {"issue": "../x"}


def test_the_webhook_takes_the_products_own_token(engine, monkeypatch, capsys):
    eng, _ = engine
    monkeypatch.setenv("VRIXEL_SIGNALS_TOKEN", "s3cret")
    client = TestClient(create_app(eng, auth=AuthConfig(mode="dev", secret="x" * 40), web_dist=None))
    good = et.token_for("PAY")
    assert good and good != et.token_for("CLN")
    assert client.post("/api/signals/glitchtip/PAY?token=nope", json=ALERT).status_code == 401
    assert client.post(f"/api/signals/glitchtip/PAY?token={et.token_for('CLN')}", json=ALERT).status_code == 401
    r = client.post(f"/api/signals/glitchtip/PAY?token={good}", json=ALERT)
    assert r.status_code == 202 and len(r.json()["incidents"]) == 1
    assert client.post(f"/api/signals/glitchtip/CLN?token={et.token_for('CLN')}", json=ALERT).status_code == 404

    assert cli(["glitchtip-token", "PAY"]) == 0
    assert capsys.readouterr().out.strip() == f"/api/signals/glitchtip/PAY?token={good}"
    monkeypatch.delenv("VRIXEL_SIGNALS_TOKEN")
    assert cli(["glitchtip-token", "PAY"]) == 2


def test_runtime_calls_reach_the_call_graph(tmp_path):
    from vrixel_sdlc.ops.correlate import graph_for

    root = tmp_path / "governance"
    shutil.copytree(EXAMPLES, root)
    p = root / "acme-pay" / "pay-governance" / "product.yaml"
    p.write_text(p.read_text().replace("runtime:\n", "runtime:\n  calls: {pay-web: [pay-ledger]}\n", 1))
    s = load_settings(mode="stub", database_url="", artifact_dir=tmp_path / "a", governance_source="local",
                      governance_root=root)
    snap = build_services(s).snapshot("PAY")
    assert snap.product.runtime["calls"] == {"pay-web": ["pay-ledger"]}
    assert graph_for(snap).related("pay-web", "pay-ledger")

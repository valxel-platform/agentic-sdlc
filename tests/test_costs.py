"""FinOps: a released feature's cost against what G4 approved, and budget alerts from OpenChoreo and
AWS Budgets, all as cost signals in the incident flow."""

import base64
import json
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import InMemorySaver

from vrixel_sdlc.api.app import create_app
from vrixel_sdlc.config import load_settings
from vrixel_sdlc.engine import FeatureEngine
from vrixel_sdlc.ops import cost
from vrixel_sdlc.ops.incident import recovery
from vrixel_sdlc.ops.signals import from_aws_budget, from_openchoreo, still_firing
from vrixel_sdlc.services import build_services
from vrixel_sdlc.web.auth import AuthConfig

EXAMPLES = Path(__file__).resolve().parents[1] / "examples" / "governance"
TOPIC = "arn:aws:sns:us-east-2:333333333333:vrixel-budgets"

BUDGET_MAIL = """AWS Budget Notification November 05, 2026
AWS Account 333333333333

Dear AWS Customer,

You requested that we alert you when the ACTUAL Cost associated with your vrixel-FARM-production budget is greater than $800.00 for the current month. The ACTUAL Cost associated with this budget is $1,042.17.

Budget Name: vrixel-FARM-production
Budget Type: Cost
Budgeted Amount: $1,000.00
Alert Type: ACTUAL
Alert Threshold: > $800.00
ACTUAL Amount: $1,042.17
"""


def oc_budget(**over):
    return {"alertName": "farm-field-api-monthly-budget", "alertTimestamp": "2026-11-05T10:00:00Z",
            "alertSeverity": "critical", "alertDescription": "Monthly spend over budget", "alertThreshold": 150,
            "alertValue": 212.4, "alertType": "budget", "namespace": "default", "project": "farm",
            "component": "farm-field-api", "environment": "production", "releaseLine": "", **over}


@pytest.fixture
def engine(tmp_path):
    root = tmp_path / "governance"
    shutil.copytree(EXAMPLES, root)
    s = load_settings(mode="stub", database_url="", artifact_dir=tmp_path / "artifacts",
                      governance_source="local", governance_root=root)
    svc = build_services(s)
    svc.costs = cost.StubCostSource()
    return FeatureEngine(svc, InMemorySaver())


def released(engine):
    fid = engine.start(product_id="FARM", title="Field log",
                       description="Keep each field's latest state in DynamoDB")["feature_id"]
    v = engine.auto_drive(fid)
    assert v["status"] == "done"
    return fid, engine._state(fid)


# --------------------------------------------------------------------------- signals
def test_an_openchoreo_budget_alert_is_a_cost_signal_that_never_waits_to_clear():
    [s] = from_openchoreo(oc_budget())
    assert s["category"] == "cost" and s["source"] == "openchoreo" and s["resolves"] is False
    assert s["severity"] == "sev2"  # critical, but spend is never an outage
    assert "$212.40 over the $150.00 budget" in s["detail"]
    assert s["labels"]["project"] == "farm" and s["env"] == "production"
    [m] = from_openchoreo(oc_budget(alertType="metric", alertSeverity="critical"))
    assert "category" not in m and m["severity"] == "sev1"
    assert from_openchoreo({}) == []
    assert still_firing([s]) == [] and recovery([s], "2026-11-05T10:00:00+00:00", 30)["status"] == "success"


def test_an_aws_budgets_notification_names_its_product_and_environment():
    [s] = from_aws_budget(BUDGET_MAIL, ["FARM", "CLN", "AGX"])
    assert (s["product_id"], s["env"], s["severity"]) == ("FARM", "production", "sev2")  # actual over budget
    assert s["category"] == "cost" and s["resolves"] is False
    assert "ACTUAL $1,042.17 against a budget of $1,000.00" in s["detail"] and "333333333333" in s["detail"]
    forecast = BUDGET_MAIL.replace("Alert Type: ACTUAL", "Alert Type: FORECASTED").replace(
        "ACTUAL Amount", "FORECASTED Amount").replace("vrixel-FARM-production", "vrixel-FARM-staging")
    [f] = from_aws_budget(forecast, ["FARM"])
    assert (f["env"], f["severity"]) == ("staging", "sev4")
    [other] = from_aws_budget(BUDGET_MAIL.replace("vrixel-FARM-production", "team-budget"), ["FARM"])
    assert other["product_id"] == ""  # not ours: ingest drops it
    assert from_aws_budget("hello", ["FARM"]) == []


# --------------------------------------------------------------------------- the review
PLAN = {"aws_monthly_usd": {"dev": 10.0, "staging": 10.0, "production": 60.0}}
T0 = datetime(2026, 11, 1, tzinfo=timezone.utc)


def test_spend_within_tolerance_is_fine_and_above_it_is_a_finding():
    ok = cost.review(PLAN, feature_id="FARM-FEAT-1", product_id="FARM", released=T0, at=T0 + timedelta(days=7),
                     aws_actual={"production": {"Amazon DynamoDB": 15.0}}, window_days=7,
                     compute_before={}, compute_after={})
    assert ok["aws"]["production"]["projected_monthly_usd"] == pytest.approx(64.29) and ok["findings"] == []

    bad = cost.review(PLAN, feature_id="FARM-FEAT-1", product_id="FARM", released=T0, at=T0 + timedelta(days=7),
                      aws_actual={"production": {"Amazon DynamoDB": 35.0}, "dev": {"Amazon DynamoDB": 1.0}},
                      window_days=7, compute_before={"farm-field-api": 7.0}, compute_after={"farm-field-api": 21.0})
    kinds = {(f["kind"], f["env"], f["severity"]) for f in bad["findings"]}
    assert kinds == {("aws", "production", "sev2"), ("compute", "production", "sev3")}  # 150 vs 60: 2.5x
    sigs = cost.signals(bad, title="Field log", repo_of=lambda c: f"acme-farm/{c}")
    assert {s["fingerprint"] for s in sigs} == {"cost:FARM-FEAT-1:aws-production", "cost:FARM-FEAT-1:farm-field-api"}
    assert next(s for s in sigs if s["labels"]["kind"] == "compute")["component"] == "acme-farm/farm-field-api"

    small = cost.review({"aws_monthly_usd": {"production": 0.0}}, feature_id="F", product_id="FARM", released=T0,
                        at=T0 + timedelta(days=7), aws_actual={"production": {"x": 0.7}}, window_days=7,
                        compute_before={}, compute_after={})
    assert small["findings"] == []  # $3/month unplanned: below the floor


def test_g4_records_the_plan_and_a_released_feature_over_it_opens_a_cost_incident(engine):
    fid, s = released(engine)
    plan = s["cost_plan"]
    assert plan["aws_monthly_usd"]["production"] == 60.0 and plan["accounts"]["production"] == "333333333333"
    assert [r["type"] for r in plan["resources"]] == ["dynamodb-table"]
    at = cost.released_at(s)

    assert engine.review_costs(at=at + timedelta(days=3)) == []  # not due yet
    engine.services.costs.set_aws(fid, "333333333333", 5.0)  # $150/month against $60
    [rv] = engine.review_costs(at=at + timedelta(days=8))
    assert rv["review_day"] == 7 and rv["aws"]["production"]["projected_monthly_usd"] == 150.0
    [iid] = rv["incidents"]
    inc = engine._state(iid)
    assert inc["severity"] == "sev2" and "costs more than approved" in inc["title"]
    assert "intent_change" in {a["type"] for a in inc["actions"]}
    assert engine.review_costs(at=at + timedelta(days=9)) == []  # day 7 is done

    # Thirty days on it is still over: the finding joins the open cost incident.
    [rv30] = engine.review_costs(at=at + timedelta(days=31))
    assert rv30["review_day"] == 30 and rv30["incidents"] == [iid]

    view = engine.feature_cost(fid)
    llm = next(c for c in view["build"]["by_category"] if c["category"] == "llm")
    assert llm["usd"] == pytest.approx(s["cost_usd"], abs=1e-3) and view["plan"]["aws_monthly_usd"]["production"] == 60.0
    assert view["latest_review"]["aws"]["production"]["projected_monthly_usd"] == 150.0 and len(view["reviews"]) == 2
    row = engine.product_cost("FARM")["features"][0]
    assert row["feature_id"] == fid and row["approved_monthly_usd"] == 60.0


def test_a_cost_finding_never_joins_a_components_outage(engine):
    from vrixel_sdlc.ops.signals import from_alertmanager

    outage = engine.ingest(from_alertmanager({"alerts": [{"status": "firing", "fingerprint": "x", "labels": {
        "alertname": "Errors", "severity": "critical", "component": "farm-field-api", "vrixel_product": "FARM"}}]}))
    budget = engine.ingest(from_openchoreo(oc_budget()))
    assert budget and budget != outage
    assert engine._state(budget[0])["product_id"] == "FARM"  # found through the OpenChoreo project


# --------------------------------------------------------------------------- API
def test_budget_webhooks(engine, monkeypatch):
    client = TestClient(create_app(engine, auth=AuthConfig(mode="dev", secret="x" * 40), web_dist=None))
    assert client.post("/api/signals/openchoreo", json=oc_budget()).status_code == 404  # not configured
    monkeypatch.setenv("VRIXEL_SIGNALS_TOKEN", "t0k")
    r = client.post("/api/signals/openchoreo", json=oc_budget(), headers={"authorization": "Bearer t0k"})
    assert r.status_code == 202 and r.json()["incidents"] == ["FARM-INC-1"]

    note = {"Type": "Notification", "TopicArn": TOPIC, "Message": BUDGET_MAIL, "Timestamp": "2026-11-05T10:00:00Z"}
    assert client.post("/api/signals/aws-budgets", json=note).status_code == 404  # no topics allowed yet
    monkeypatch.setenv("VRIXEL_AWS_BUDGET_TOPICS", TOPIC)
    r = client.post("/api/signals/aws-budgets", json=note)
    assert r.status_code == 401 and r.headers["www-authenticate"].startswith("Basic")  # SNS's challenge
    basic = {"authorization": "Basic " + base64.b64encode(b"vrixel:t0k").decode()}
    r = client.post("/api/signals/aws-budgets", json=note, headers=basic)
    assert r.status_code == 202 and r.json()["incidents"] == ["FARM-INC-2"]
    assert client.post("/api/signals/aws-budgets", json={**note, "TopicArn": TOPIC + "x"}, headers=basic).status_code == 403

    visited = []

    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b"ok"

    monkeypatch.setattr("urllib.request.urlopen", lambda url, timeout=0: visited.append(url) or Resp())
    token = "2336412f37fb687f5d51e6e2425c464de" * 2
    confirm = {"Type": "SubscriptionConfirmation", "TopicArn": TOPIC, "Token": token,
               "SubscribeURL": "https://evil.example/?Action=ConfirmSubscription"}  # never visited
    assert client.post("/api/signals/aws-budgets", json=confirm, headers=basic).status_code == 202
    [url] = visited
    assert url.startswith("https://sns.us-east-2.amazonaws.com/?Action=ConfirmSubscription&TopicArn=")
    assert url.endswith(f"&Token={token}")
    bad = {**confirm, "Token": "a&b=c"}
    assert client.post("/api/signals/aws-budgets", json=bad, headers=basic).status_code == 400


def test_cost_views_are_for_the_products_members(engine):
    fid, _ = released(engine)
    client = TestClient(create_app(engine, auth=AuthConfig(mode="dev", secret="x" * 40), web_dist=None))
    client.post("/api/auth/dev-login", json={"login": "someone", "orgs": ["acme-clinic"]})
    assert client.get(f"/api/features/{fid}/cost").status_code == 403
    client.post("/api/auth/dev-login", json={"login": "farmer", "orgs": ["acme-farm"]})
    c = client.get(f"/api/features/{fid}/cost").json()
    assert c["plan"]["aws_monthly_usd"]["production"] == 60.0 and c["latest_review"] is None
    p = client.get("/api/costs/FARM").json()
    assert p["features"][0]["feature_id"] == fid
    assert client.get("/api/features/FARM-FEAT-99/cost").status_code == 404


def test_observer_and_cost_explorer_sources():
    calls = []

    class Opener:
        def open(self, req, timeout=0):
            calls.append(req)

            class R:
                def __enter__(self):
                    return self

                def __exit__(self, *a):
                    return False

                def read(self):
                    return json.dumps({"items": [
                        {"component": "farm-field-api", "cpuCost": 1.5, "memoryCost": 0.5},
                        {"component": "other", "cpuCost": 9, "memoryCost": 9}]}).encode()
            return R()

    src = cost.ObserverCostSource("https://observer.example/", auth=lambda: {"Authorization": "Bearer x"}, opener=Opener())
    got = src.component_costs(namespace="default", project="farm", env="production",
                              components=["farm-field-api", "farm-web"], start=T0, end=T0 + timedelta(days=7))
    assert got == {"farm-field-api": 2.0, "farm-web": 0.0}
    assert calls[0].full_url.startswith("https://observer.example/api/v1alpha1/costs/namespaces/default/environments/production?")
    assert "project=farm" in calls[0].full_url and calls[0].headers["Authorization"] == "Bearer x"

    class CE:
        def __init__(self):
            self.kw = []

        def get_cost_and_usage(self, **kw):
            self.kw.append(kw)
            page = {"ResultsByTime": [{"Groups": [{"Keys": ["Amazon DynamoDB"],
                                                   "Metrics": {"UnblendedCost": {"Amount": "1.25"}}}]}]}
            return {**page, "NextPageToken": "n"} if len(self.kw) == 1 else page

    ce = CE()
    out = cost.CostExplorerSource(ce).aws_costs(product_id="FARM", feature_id="FARM-FEAT-1", account="3" * 12,
                                                 start=T0, end=T0 + timedelta(days=7))
    assert out == {"Amazon DynamoDB": 2.5} and ce.kw[1]["NextPageToken"] == "n"
    tags = ce.kw[0]["Filter"]["And"][0]["Tags"]
    assert tags["Key"] == "vrixel:feature" and tags["Values"] == ["FARM-FEAT-1"]

"""The cost ledger (finops): every place a feature or product spends money, collected into FOCUS
records, and the feature card and product rollup built from them."""

import csv
import io
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
from vrixel_sdlc.finops import collect as fc
from vrixel_sdlc.finops.ledger import FOCUS_COLUMNS, CostRecord, InMemoryCostLedger, focus_csv
from vrixel_sdlc.finops.views import product_rollup
from vrixel_sdlc.ops import cost
from vrixel_sdlc.services import build_services
from vrixel_sdlc.web.auth import AuthConfig

EXAMPLES = Path(__file__).resolve().parents[1] / "examples" / "governance"
D0 = datetime(2026, 11, 2, tzinfo=timezone.utc)


def rec(key, **over):
    base = dict(key=key, product_id="FARM", stage="build", category="ci", usd=1.0,
                period_start=D0.isoformat(), period_end=(D0 + timedelta(hours=1)).isoformat())
    return CostRecord(**{**base, **over})


def test_records_are_upserted_by_key_and_export_as_focus():
    ledger = InMemoryCostLedger()
    ledger.upsert([rec("ci:a:1"), rec("ci:a:2", feature_id="FARM-FEAT-1")])
    ledger.upsert([rec("ci:a:1", usd=2.5)])  # a collector ran again
    assert sorted(r.usd for r in ledger.query(product_id="FARM")) == [1.0, 2.5]
    assert [r.key for r in ledger.query(feature_id="FARM-FEAT-1")] == ["ci:a:2"]
    assert ledger.query(start=(D0 + timedelta(days=1)).isoformat()) == []
    with pytest.raises(ValueError):
        rec("x", category="snacks")

    rows = list(csv.DictReader(io.StringIO(focus_csv([rec("e", category="environment", basis="allocated", usd=3)]))))
    assert tuple(rows[0]) == FOCUS_COLUMNS
    assert rows[0]["BilledCost"] == "0.0" and rows[0]["EffectiveCost"] == "3.0"  # an allocation isn't billed
    assert rows[0]["ProviderName"] == "OpenChoreo" and json.loads(rows[0]["Tags"]) == {"vrixel:product": "FARM"}


def test_ci_minutes_are_billed_per_job_rounded_up_by_runner():
    from vrixel_sdlc.integrations.github import StubGitHub

    gh = StubGitHub()
    at = "2026-11-02T10:00:00+00:00"
    gh.actions[("o/app", "o/app@merge")] = []
    gh.actions[("o/app", "abc")] = [{"id": 7, "name": "release", "status": "completed", "created_at": at, "jobs": [
        {"started_at": at, "completed_at": "2026-11-02T10:10:01+00:00", "labels": ["macos-14"]}]}]
    gh.actions[("o/acc", "repository_dispatch")] = [
        {"id": 9, "name": "acceptance", "display_title": "acceptance FARM-FEAT-1 staging", "status": "completed",
         "created_at": at, "jobs": [{"started_at": at, "completed_at": "2026-11-02T10:02:00+00:00", "labels": ["ubuntu-24.04"]}]},
        {"id": 10, "display_title": "acceptance FARM-FEAT-2", "status": "completed", "created_at": at,
         "jobs": [{"started_at": at, "completed_at": "2026-11-02T11:00:00+00:00", "labels": ["ubuntu-24.04"]}]}]
    state = {"product_id": "FARM", "feature_id": "FARM-FEAT-1",
             "pull_requests": {"o/app#1": {"repo": "o/app", "number": 1, "branch": "feat", "merge_sha": "abc"}},
             "change_set": {"acceptance": {"repo": "o/acc", "sha": "s"}}}
    out = {r.key: r for r in fc.collect_ci(gh, state, fc.rates())}
    pr_run = next(r for k, r in out.items() if k.startswith("ci:o/app:") and r.detail["workflow"] == "ci")
    assert pr_run.quantity == 5 and pr_run.usd == pytest.approx(0.04)  # 4m30s -> 5 Linux minutes
    assert out["ci:o/app:7"].quantity == 11 and out["ci:o/app:7"].usd == pytest.approx(0.88)  # macOS
    assert out["test:o/acc:9"].category == "test" and "test:o/acc:10" not in out  # another feature's run


def test_an_environment_day_is_shared_by_the_features_in_it_and_the_rest_is_idle():
    src = cost.StubCostSource()
    src.set_component("dev", "api", 24.0)
    day = D0 + timedelta(days=1)
    windows = {("p", "dev", "api"): [("FARM-FEAT-1", day, day + timedelta(hours=6)),
                                     ("FARM-FEAT-2", day + timedelta(hours=3), day + timedelta(hours=9))]}
    out = fc.collect_runtime(src, "FARM", [("default", "p", "dev", False), ("default", "p", "production", True)],
                             windows, D0 + timedelta(days=3), fc.rates())
    mine = {r.feature_id: r.usd for r in out if r.period_start.startswith("2026-11-03") and r.env == "dev"}
    assert mine == {"FARM-FEAT-1": pytest.approx(6.0), "FARM-FEAT-2": pytest.approx(6.0), "": pytest.approx(12.0)}
    assert {r.stage for r in out if r.feature_id} == {"build"}
    crowded = fc._share([("a", day, day + timedelta(days=1)), ("b", day, day + timedelta(days=1))], day.date())
    assert crowded == {"a": 0.5, "b": 0.5}


def test_aws_spend_follows_the_feature_tag_and_the_release():
    src = cost.StubCostSource()
    src.set_aws("FARM-FEAT-1", "111", 2.0)
    src.aws_product_daily[("FARM", "111")] = 1.0
    rel = D0 + timedelta(days=1)
    out = fc.collect_aws(src, "FARM", {"dev": "111"}, {"FARM-FEAT-1": rel}, D0 + timedelta(days=3), fc.rates())
    feature = [r for r in out if r.feature_id == "FARM-FEAT-1"]
    assert {r.stage for r in feature if r.period_end <= rel.isoformat()} == {"build"}
    assert {r.stage for r in feature if r.period_start >= rel.isoformat()} == {"run"}
    assert {(r.stage, r.service) for r in out if not r.feature_id} == {("run", "AWS Key Management Service")}
    assert any(r.status == "provisional" for r in out) and any(r.status == "final" for r in out)


@pytest.fixture
def engine(tmp_path, monkeypatch):
    monkeypatch.setenv("VRIXEL_COST_RATES", json.dumps({"people_hour_usd": 120, "platform_monthly_usd": 3000}))
    root = tmp_path / "governance"
    shutil.copytree(EXAMPLES, root)
    s = load_settings(mode="stub", database_url="", artifact_dir=tmp_path / "artifacts",
                      governance_source="local", governance_root=root)
    svc = build_services(s)
    svc.costs = cost.StubCostSource()
    return FeatureEngine(svc, InMemorySaver())


def test_a_features_card_and_its_products_month(engine):
    seen = {}
    real = engine.decide

    def capture(fid, iid, decision):
        for p in engine.pending(fid):
            if p.get("interrupt_id") == iid and p.get("gate"):
                seen[p["gate"]] = p.get("build_cost")
        return real(fid, iid, decision)

    engine.decide = capture
    fid = engine.start(product_id="FARM", title="Field log",
                       description="Keep each field's latest state in DynamoDB")["feature_id"]
    s = engine.auto_drive(fid)
    assert s["status"] == "done"
    assert seen["g6"]["by_category"][0]["category"] == "llm" and seen["g6"]["total_usd"] > 0

    comps = sorted({d["component"] for d in s["deployments"].values() if d.get("kind") not in ("resource", "store")})
    src = engine.services.costs
    for c in comps:
        src.set_component("dev", c, 4.8, project="farm")
        src.set_component("production", c, 12.0, project="farm")
    src.set_aws(fid, "111111111111", 1.0)
    src.aws_product_daily[("FARM", "333333333333")] = 0.5
    released = cost.released_at(s)
    at = released + timedelta(days=3)
    n = engine.collect_costs(at=at)
    assert n > 0 and engine.collect_costs(at=at) == n  # re-running replaces, never adds

    card = engine.feature_cost(fid)
    cats = {c["category"]: c for c in card["build"]["by_category"]}
    assert {"llm", "ci", "build", "environment", "aws", "people"} <= set(cats)
    assert cats["ci"]["basis"] == ["metered"] and cats["environment"]["basis"] == ["allocated"]
    assert cats["aws"]["basis"] == ["billed"] and card["build"]["provisional"] is False
    assert card["build"]["total_usd"] == pytest.approx(sum(c["usd"] for c in cats.values()), abs=1e-3)
    assert [c["category"] for c in card["run_30d"]["by_category"]] == ["aws"]  # its table, since release

    month = f"{at:%Y-%m}"
    roll = engine.product_cost("FARM", month)
    run = {c["category"]: c["usd"] for c in roll["run"]["by_category"]}
    assert run["compute"] > 0 and run["environment"] > 0 and run["platform"] > 0 and run["aws"] > 0
    assert roll["totals"]["total_usd"] == pytest.approx(roll["totals"]["run_usd"] + roll["totals"]["build_usd"])
    assert roll["features"][0]["feature_id"] == fid and roll["features"][0]["build_to_date_usd"] == pytest.approx(
        card["build"]["total_usd"], abs=1e-3)
    assert set(roll["run"]["by_env"]) >= {"dev", "production"}
    # The platform is shared by products with production compute: FARM is the only one here.
    platform = [r for r in engine.services.cost_ledger.query(stage="run") if r.category == "platform"]
    assert [r.product_id for r in platform] == ["FARM"] and platform[0].detail["share"] == 1.0

    client = TestClient(create_app(engine, auth=AuthConfig(mode="dev", secret="x" * 40), web_dist=None))
    client.post("/api/auth/dev-login", json={"login": "farmer", "orgs": ["acme-farm"]})
    assert client.get(f"/api/costs/FARM?month={month}").json()["totals"] == roll["totals"]
    assert client.get("/api/costs/FARM?month=2026-13").status_code == 400
    r = client.get(f"/api/costs/FARM/focus.csv?month={month}")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    rows = list(csv.DictReader(io.StringIO(r.text)))
    assert {x["x_CostCategory"] for x in rows} >= {"llm", "ci", "compute", "aws", "platform"}
    assert sum(float(x["EffectiveCost"]) for x in rows) == pytest.approx(roll["totals"]["total_usd"], abs=0.01)


def test_rollup_counts_each_dollar_once():
    ledger = InMemoryCostLedger()
    ledger.upsert([rec("a", feature_id="F1", category="aws", basis="billed", usd=10, env="dev"),
                   rec("b", stage="run", category="aws", basis="billed", usd=5, env="production"),
                   rec("c", stage="run", feature_id="F1", category="aws", basis="billed", usd=2, env="production")])
    roll = product_rollup(ledger, "FARM", [{"feature_id": "F1", "title": "x", "status": "released"}], month="2026-11")
    assert roll["totals"] == {"run_usd": 7.0, "build_usd": 10.0, "total_usd": 17.0, "build_to_date_usd": 10.0}
    assert roll["features"][0]["run_month_usd"] == 2.0 and roll["run"]["by_env"] == {"production": 7.0}

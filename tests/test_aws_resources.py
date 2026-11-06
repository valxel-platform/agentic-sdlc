"""VX-19: AWS services chosen at G4 from the platform catalog, created and bound per environment
through OpenChoreo, each component with only the access its design declares."""

import shutil
from pathlib import Path

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from vrixel_sdlc import resources
from vrixel_sdlc.config import load_settings
from vrixel_sdlc.engine import FeatureEngine
from vrixel_sdlc.services import build_services

EXAMPLES = Path(__file__).resolve().parents[1] / "examples" / "governance"
CATALOG = resources.load_catalog((EXAMPLES / "vrixel-tools/sdlc-governance/platform/aws-resources.yaml").read_text())
REPOS = [{"name": "o/ag-api", "role": "provider"}, {"name": "o/ag-worker", "role": "event-worker"},
         {"name": "o/ag-web", "role": "consumer"}]
AWS = {"region": "us-east-2", "accounts": {"dev": "1" * 12, "staging": "2" * 12, "production": "3" * 12}}


def ctx(**over):
    return {"aws_catalog": CATALOG, "aws": AWS, "data_class": "general", "pipeline": ["dev", "staging", "production"],
            "repos": REPOS, **over}


def hld(resources_body: str, adrs: str = "- ADR-1: Use DynamoDB for field-state and Kinesis for readings") -> str:
    return f"# HLD\n\n## ADRs\n{adrs}\n\n## Resources\n{resources_body}\n\n## Endpoints\n- o/ag-api: project\n"


GOOD = """- field-state: dynamodb-table; partition_key=field_id; size production=large
  reads, writes: ag-api
- readings: kinesis-stream; retention_hours=168
  writes: ag-api; reads: ag-worker"""


def test_the_catalog_and_the_products_accounts_are_checked():
    assert "workload-identity" in CATALOG["types"] and CATALOG["types"]["workload-identity"]["internal"]
    assert resources.check_aws(AWS, "general", ["dev", "staging", "production"], CATALOG) == []
    same = {"region": "eu-west-1", "accounts": {"dev": "1" * 12, "staging": "1" * 12, "production": "x"}}
    problems = resources.check_aws(same, "payments", ["dev", "staging", "production"], CATALOG)
    assert any("not allowed for payments" in p for p in problems)
    assert any("accounts.production" in p for p in problems) and any("its own AWS account" in p for p in problems)
    with pytest.raises(resources.CatalogError):
        resources.load_catalog("types:\n  x: {service: X, use_for: y, data_classes: [general], sizes: {small: {}}}\n")


def test_a_design_declares_its_services_and_who_uses_them():
    entries = {e["name"]: e for e in resources.parse_resources(hld(GOOD))}
    assert entries["field-state"]["params"] == {"partition_key": "field_id"}
    assert entries["field-state"]["sizes"] == {"production": "large"}
    assert entries["readings"]["access"] == {"ag-api": {"write"}, "ag-worker": {"read"}}
    assert resources.check(hld(GOOD), REPOS, ctx()) == []


@pytest.mark.parametrize(("body", "adrs", "context", "finding"), [
    ("- cache: memcached\n  reads: ag-api", None, {}, "isn't in the platform catalog"),
    ("- ids: workload-identity; component=ag-api\n  reads: ag-api", None, {}, "isn't in the platform catalog"),
    ("- field-state: dynamodb-table\n  reads: ag-api", None, {}, "needs partition_key"),
    ("- readings: kinesis-stream; retention_hours=2\n  writes: ag-api", None, {}, "between 24 and 8760"),
    ("- readings: kinesis-stream; shards=4\n  writes: ag-api", None, {}, "has no parameter shards"),
    ("- readings: kinesis-stream", None, {}, "no component reads or writes it"),
    ("- readings: kinesis-stream\n  writes: ag-web", None, {}, "runs outside the platform"),
    ("- readings: kinesis-stream\n  writes: ag-ghost", None, {}, "not a component"),
    ("- alerts: sns-topic\n  reads: ag-api", "- ADR-1: SNS for alerts", {}, "offers ['write'], not read"),
    ("- readings: kinesis-stream\n  watches: ag-api", None, {}, "'watches' is not an access"),
    ("- readings: kinesis-stream; size qa=small\n  writes: ag-api", None, {}, "not one of the product's environments"),
    ("- readings: kinesis-stream; size dev=huge\n  writes: ag-api", None, {}, "has no size huge"),
    ("- readings: kinesis-stream\n  writes: ag-api", "- ADR-1: Extend the API", {}, "not a platform default"),
    ("- readings: kinesis-stream\n  writes: ag-api", None, {"aws": None}, "has no AWS accounts"),
    ("- field-state: dynamodb-table; partition_key=id\n  writes: ag-api", None,
     {"aws_existing": [{"name": "field-state", "type": "dynamodb-table", "parameters": {"partition_key": "field_id"}}]},
     "replaces it and its data"),
    ("- field-state: s3-bucket\n  writes: ag-api", "- ADR-1: S3 for field-state", {"aws_existing": [{"name": "field-state", "type": "dynamodb-table"}]},
     "changing its type replaces it"),
    ("- Bad_Name: s3-bucket\n  writes: ag-api", "- ADR-1: S3 for Bad_Name", {}, "lowercase letters"),
    ("- readings kinesis", None, {}, "Can't read resource line"),
])
def test_resource_findings(body, adrs, context, finding):
    text = hld(body, adrs) if adrs else hld(body)
    findings = resources.check(text, REPOS, ctx(**context))
    assert any(finding in f for f in findings), findings


def test_naming_a_service_needs_a_resources_section():
    text = "# HLD\n\n## ADRs\n- ADR-1: Keep field state in DynamoDB\n\n## Endpoints\n- o/ag-api: project\n"
    assert "no Resources section" in resources.check(text, REPOS, ctx())[0]
    assert resources.check(text.replace("DynamoDB", "Postgres"), REPOS, ctx()) == []


def test_restricted_data_classes_are_enforced():
    catalog = {**CATALOG, "types": {**CATALOG["types"], "kinesis-stream": {**CATALOG["types"]["kinesis-stream"],
                                                                          "data_classes": ["general"]}}}
    findings = resources.check(hld(GOOD), REPOS, ctx(aws_catalog=catalog, data_class="phi"))
    assert any("not approved for phi" in f for f in findings)


def test_g4_sees_the_cost_and_the_plan_gives_least_privilege():
    s = resources.summary(hld(GOOD), CATALOG, ["dev", "staging", "production"], AWS)
    rows = {r["name"]: r for r in s["resources"]}
    assert rows["field-state"]["sizes"] == {"dev": "small", "staging": "small", "production": "large"}
    assert s["monthly_usd"] == {"dev": 20.0, "staging": 20.0, "production": 660.0}
    assert rows["readings"]["access"] == {"ag-api": ["write"], "ag-worker": ["read"]}

    plan = resources.plan(hld(GOOD), CATALOG, lambda c: ("agtech", None), product_id="AG", feature_id="AG-1",
                          data_class="general")
    assert [p["name"] for p in plan[:2]] == ["ag-api-identity", "ag-worker-identity"]
    readings = next(p for p in plan if p["name"] == "readings")
    writer = next(c for c in readings["parameters"]["consumers"] if c["component"] == "ag-api")
    assert "kinesis:PutRecord" in writer["actions"] and "kinesis:GetRecords" not in writer["actions"]
    assert readings["owner"] == "ag-api" and readings["parameters"]["tags"]["vrixel:feature"] == "AG-1"


def test_coders_get_the_workload_bindings():
    snippet = resources.workload_snippet(hld(GOOD), CATALOG, "ag-worker")
    assert "- ref: readings" in snippet and "stream: READINGS_STREAM" in snippet and "field-state" not in snippet
    text = resources.instruction({"hld_text": hld(GOOD), "aws_catalog": CATALOG}, {"repo": "o/ag-api"})
    assert "field-state (dynamodb-table, read/write)" in text and "FIELD_STATE_TABLE" in text
    assert "DynamoDB Local" in text and "never keys" in text
    assert resources.instruction({"hld_text": hld(GOOD), "aws_catalog": CATALOG}, {"repo": "o/ag-web"}) == ""
    guide = resources.catalog_guide(CATALOG, "general")
    assert "dynamodb-table (Amazon DynamoDB)" in guide and "workload-identity" not in guide


@pytest.fixture
def engine(tmp_path):
    root = tmp_path / "governance"
    shutil.copytree(EXAMPLES, root)
    s = load_settings(mode="stub", database_url="", artifact_dir=tmp_path / "artifacts",
                      governance_source="local", governance_root=root)
    return FeatureEngine(build_services(s), InMemorySaver())


def test_g4_approves_the_services_and_the_pipeline_creates_them_in_every_environment(engine):
    seen = {}
    real = engine.decide

    def capture(fid, iid, decision):
        for p in engine.pending(fid):
            if p.get("interrupt_id") == iid and p.get("gate") in ("g4", "g6"):
                seen[p["gate"]] = p.get("aws_resources")
        return real(fid, iid, decision)

    engine.decide = capture
    oc = engine.services.openchoreo
    order = []
    for method in ("bind_resource", "promote"):
        real_m = getattr(oc, method)

        def logged(*a, _m=real_m, _n=method, **kw):
            order.append((_n, kw.get("name") or kw.get("component"), kw["env"]))
            return _m(*a, **kw)

        setattr(oc, method, logged)
    v = engine.auto_drive(engine.start(product_id="AG", title="Field log",
                                       description="Keep each field's latest state in DynamoDB")["feature_id"])
    assert v["status"] == "done"
    g4 = seen["g4"]
    assert [r["type"] for r in g4["resources"]] == ["dynamodb-table"] and g4["region"] == "us-east-2"
    assert g4["monthly_usd"]["production"] == 60.0

    assert set(oc.resources) == {"agtech-field-api-identity", "field-log-dynamodb"}
    assert oc.resources["field-log-dynamodb"]["parameters"]["consumers"][0]["component"] == "agtech-field-api"
    binds = [(b["name"], b["env"], b["size"], b["actor_type"]) for b in oc.resource_bindings]
    assert ("field-log-dynamodb", "dev", "small", "agent") in binds
    assert ("field-log-dynamodb", "production", "medium", "human") in binds
    # In each environment the resources are bound before any component is deployed.
    for env in ("dev", "staging", "production"):
        steps = [kind for kind, _, e in order if e == env]
        assert steps and steps.index("promote") > max(i for i, k in enumerate(steps) if k == "bind_resource")


def test_a_resource_that_fails_to_come_up_goes_to_people(engine):
    engine.services.openchoreo.resource_results[("field-log-dynamodb", "dev")] = "failed"
    v = engine.auto_drive(engine.start(product_id="AG", title="Field log",
                                       description="Keep each field's latest state in DynamoDB")["feature_id"],
                          max_steps=14, decisions={"escalation:delivery": {"decision": "abort", "feedback": "no table"}})
    assert v["status"] == "aborted"
    assert not any(p["env"] == "production" for p in engine.services.openchoreo.promotions)

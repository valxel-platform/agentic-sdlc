"""VX-19: AWS services chosen at G4 from the platform catalog, created and bound per environment
through OpenChoreo, each component with only the access its design declares."""

import shutil
from pathlib import Path

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from valxel_sdlc import resources
from valxel_sdlc.config import load_settings
from valxel_sdlc.engine import FeatureEngine
from valxel_sdlc.services import build_services

EXAMPLES = Path(__file__).resolve().parents[1] / "examples" / "governance"
CATALOG = resources.load_catalog((EXAMPLES / "valxel-platform/sdlc-governance/platform/aws-resources.yaml").read_text())
REPOS = [{"name": "o/farm-api", "role": "provider"}, {"name": "o/farm-worker", "role": "event-worker"},
         {"name": "o/farm-web", "role": "consumer"}]
AWS = {"region": "us-east-2", "accounts": {"dev": "1" * 12, "staging": "2" * 12, "production": "3" * 12}}


def ctx(**over):
    return {"aws_catalog": CATALOG, "aws": AWS, "data_class": "general", "pipeline": ["dev", "staging", "production"],
            "repos": REPOS, **over}


def hld(resources_body: str, adrs: str = "- ADR-1: Use DynamoDB for field-state and Kinesis for readings") -> str:
    return f"# HLD\n\n## ADRs\n{adrs}\n\n## Resources\n{resources_body}\n\n## Endpoints\n- o/farm-api: project\n"


GOOD = """- field-state: dynamodb-table; partition_key=field_id, indexes=by-farm:farm_id+by-crop:crop_id; size production=large
  reads, writes: farm-api
- readings: kinesis-stream; retention_hours=168
  writes: farm-api; reads: farm-worker"""


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
    assert entries["field-state"]["params"] == {"partition_key": "field_id", "indexes": "by-farm:farm_id+by-crop:crop_id"}
    assert entries["field-state"]["sizes"] == {"production": "large"}
    assert entries["readings"]["access"] == {"farm-api": {"write"}, "farm-worker": {"read"}}
    assert resources.check(hld(GOOD), REPOS, ctx()) == []


@pytest.mark.parametrize(("body", "adrs", "context", "finding"), [
    ("- cache: memcached\n  reads: farm-api", None, {}, "isn't in the platform catalog"),
    ("- ids: workload-identity; component=farm-api\n  reads: farm-api", None, {}, "isn't in the platform catalog"),
    ("- field-state: dynamodb-table\n  reads: farm-api", None, {}, "needs partition_key"),
    ("- readings: kinesis-stream; retention_hours=2\n  writes: farm-api", None, {}, "between 24 and 8760"),
    ("- readings: kinesis-stream; shards=4\n  writes: farm-api", None, {}, "has no parameter shards"),
    ("- readings: kinesis-stream", None, {}, "no component reads or writes it"),
    ("- readings: kinesis-stream\n  writes: farm-web", None, {}, "runs outside the platform"),
    ("- readings: kinesis-stream\n  writes: farm-ghost", None, {}, "not a component"),
    ("- alerts: sns-topic\n  reads: farm-api", "- ADR-1: SNS for alerts", {}, "offers ['write'], not read"),
    ("- readings: kinesis-stream\n  watches: farm-api", None, {}, "'watches' is not an access"),
    ("- readings: kinesis-stream; size qa=small\n  writes: farm-api", None, {}, "not one of the product's environments"),
    ("- readings: kinesis-stream; size dev=huge\n  writes: farm-api", None, {}, "has no size huge"),
    ("- readings: kinesis-stream\n  writes: farm-api", "- ADR-1: Extend the API", {}, "not a platform default"),
    ("- readings: kinesis-stream\n  writes: farm-api", None, {"aws": None}, "has no AWS accounts"),
    ("- field-state: dynamodb-table; partition_key=id\n  writes: farm-api", None,
     {"aws_existing": [{"name": "field-state", "type": "dynamodb-table", "parameters": {"partition_key": "field_id"}}]},
     "replaces it and its data"),
    ("- field-state: s3-bucket\n  writes: farm-api", "- ADR-1: S3 for field-state", {"aws_existing": [{"name": "field-state", "type": "dynamodb-table"}]},
     "changing its type replaces it"),
    ("- Bad_Name: s3-bucket\n  writes: farm-api", "- ADR-1: S3 for Bad_Name", {}, "lowercase letters"),
    ("- readings kinesis", None, {}, "Can't read resource line"),
    ("- field-state: dynamodb-table; partition_key=id, indexes=a:b+\n  reads: farm-api", None, {}, "separate items with +"),
])
def test_resource_findings(body, adrs, context, finding):
    text = hld(body, adrs) if adrs else hld(body)
    findings = resources.check(text, REPOS, ctx(**context))
    assert any(finding in f for f in findings), findings


def test_naming_a_service_needs_a_resources_section():
    text = "# HLD\n\n## ADRs\n- ADR-1: Keep field state in DynamoDB\n\n## Endpoints\n- o/farm-api: project\n"
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
    assert rows["readings"]["access"] == {"farm-api": ["write"], "farm-worker": ["read"]}

    plan = resources.plan(hld(GOOD), CATALOG, lambda c: ("farm", None), product_id="FARM", feature_id="FARM-1",
                          data_class="general")
    assert [p["name"] for p in plan[:2]] == ["farm-api-identity", "farm-worker-identity"]
    readings = next(p for p in plan if p["name"] == "readings")
    writer = next(c for c in readings["parameters"]["consumers"] if c["component"] == "farm-api")
    assert "kinesis:PutRecord" in writer["actions"] and "kinesis:GetRecords" not in writer["actions"]
    assert readings["owner"] == "farm-api" and readings["parameters"]["tags"]["valxel:feature"] == "FARM-1"
    state = next(p for p in plan if p["name"] == "field-state")
    assert state["parameters"]["indexes"] == ["by-farm:farm_id", "by-crop:crop_id"]  # the ResourceType's list


def test_coders_get_the_workload_bindings():
    snippet = resources.workload_snippet(hld(GOOD), CATALOG, "farm-worker")
    assert "- ref: readings" in snippet and "stream: READINGS_STREAM" in snippet and "field-state" not in snippet
    text = resources.instruction({"hld_text": hld(GOOD), "aws_catalog": CATALOG}, {"repo": "o/farm-api"})
    assert "field-state (dynamodb-table, read/write)" in text and "FIELD_STATE_TABLE" in text
    assert "DynamoDB Local" in text and "never keys" in text
    assert resources.instruction({"hld_text": hld(GOOD), "aws_catalog": CATALOG}, {"repo": "o/farm-web"}) == ""
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
    v = engine.auto_drive(engine.start(product_id="FARM", title="Field log",
                                       description="Keep each field's latest state in DynamoDB")["feature_id"])
    assert v["status"] == "done"
    g4 = seen["g4"]
    assert [r["type"] for r in g4["resources"]] == ["dynamodb-table"] and g4["region"] == "us-east-2"
    assert g4["monthly_usd"]["production"] == 60.0

    assert set(oc.resources) == {"farm-field-api-identity", "field-log-dynamodb"}
    assert oc.resources["field-log-dynamodb"]["parameters"]["consumers"][0]["component"] == "farm-field-api"
    binds = [(b["name"], b["env"], b["size"], b["actor_type"]) for b in oc.resource_bindings]
    assert ("field-log-dynamodb", "dev", "small", "agent") in binds
    assert ("field-log-dynamodb", "production", "medium", "human") in binds
    # In each environment the resources are bound before any component is deployed.
    for env in ("dev", "staging", "production"):
        steps = [kind for kind, _, e in order if e == env]
        assert steps and steps.index("promote") > max(i for i, k in enumerate(steps) if k == "bind_resource")


def test_a_resource_that_fails_to_come_up_goes_to_people(engine):
    engine.services.openchoreo.resource_results[("field-log-dynamodb", "dev")] = "failed"
    v = engine.auto_drive(engine.start(product_id="FARM", title="Field log",
                                       description="Keep each field's latest state in DynamoDB")["feature_id"],
                          max_steps=14, decisions={"escalation:delivery": {"decision": "abort", "feedback": "no table"}})
    assert v["status"] == "aborted"
    assert not any(p["env"] == "production" for p in engine.services.openchoreo.promotions)


def fake_openchoreo_resources():
    """OpenChoreo's MCP resource tools, with their published argument names."""
    from mcp.server import MCPServer  # the server the delivery tests use

    server = MCPServer("openchoreo")
    st = {"resources": {}, "bindings": {}, "calls": []}

    @server.tool()
    def list_resources(namespace_name: str, project_name: str) -> dict:
        return {"items": [r for r in st["resources"].values() if r["spec"]["owner"]["projectName"] == project_name]}

    @server.tool()
    def get_resource(namespace_name: str, name: str) -> dict:
        if name not in st["resources"]:
            raise ValueError("not found")
        return st["resources"][name]

    @server.tool()
    def create_resource(namespace_name: str, project_name: str, name: str, type_name: str, type_kind: str = "",
                        parameters: dict | None = None) -> dict:
        st["calls"].append(("create", name, type_name, type_kind))
        st["resources"][name] = {"metadata": {"name": name}, "spec": {"owner": {"projectName": project_name},
                                 "type": {"name": type_name}, "parameters": parameters or {}},
                                 "status": {"latestRelease": {"name": f"{name}-r1"}}}
        return st["resources"][name]

    @server.tool()
    def update_resource(namespace_name: str, name: str, parameters: dict | None = None) -> dict:
        st["calls"].append(("update", name))
        r = st["resources"][name]
        r["spec"]["parameters"] = parameters or {}
        n = int(r["status"]["latestRelease"]["name"].rsplit("-r", 1)[1]) + 1
        r["status"]["latestRelease"]["name"] = f"{name}-r{n}"
        return r

    @server.tool()
    def list_resource_release_bindings(namespace_name: str, resource_name: str) -> dict:
        return {"items": [b for (r, _), b in st["bindings"].items() if r == resource_name]}

    @server.tool()
    def create_resource_release_binding(namespace_name: str, name: str, project_name: str, resource_name: str,
                                        environment: str, resource_release: str = "", retain_policy: str = "",
                                        resource_type_environment_configs: dict | None = None) -> dict:
        st["calls"].append(("bind", resource_name, environment, resource_release, retain_policy,
                            (resource_type_environment_configs or {}).get("size")))
        st["bindings"][(resource_name, environment)] = {
            "metadata": {"name": name}, "spec": {"environment": environment, "resourceRelease": resource_release},
            "status": {"conditions": [{"type": "Ready", "status": "False", "reason": "Provisioning"}]}}
        return st["bindings"][(resource_name, environment)]

    @server.tool()
    def update_resource_release_binding(namespace_name: str, name: str, resource_release: str = "",
                                        retain_policy: str = "",
                                        resource_type_environment_configs: dict | None = None) -> dict:
        st["calls"].append(("rebind", name, resource_release))
        b = next(b for b in st["bindings"].values() if b["metadata"]["name"] == name)
        b["spec"]["resourceRelease"] = resource_release
        return b

    return server, st


def test_the_openchoreo_adapter_creates_binds_and_promotes_resources():
    from valxel_sdlc.integrations.mcp_client import McpHttp
    from valxel_sdlc.integrations.openchoreo import OpenChoreoMcp

    server, st = fake_openchoreo_resources()
    oc = OpenChoreoMcp(McpHttp("openchoreo", server), default_namespace="valxel")
    params = {"partition_key": "id", "consumers": []}
    assert oc.apply_resource(name="field-state", type="dynamodb-table", parameters=params, project="farm") == {
        "name": "field-state", "release": "field-state-r1", "change": "created"}
    assert st["calls"][0] == ("create", "field-state", "dynamodb-table", "ClusterResourceType")
    assert oc.apply_resource(name="field-state", type="dynamodb-table", parameters=params,
                             project="farm")["change"] == "unchanged"
    with pytest.raises(ValueError, match="never replaces"):
        oc.apply_resource(name="field-state", type="s3-bucket", parameters={}, project="farm")
    assert oc.list_resources(project="farm") == [{"name": "field-state", "type": "dynamodb-table", "parameters": params}]

    scope = {"name": "field-state", "project": "farm"}
    oc.bind_resource(env="dev", size="small", actor="agent:dev", actor_type="agent", retain=True, **scope)
    assert ("bind", "field-state", "dev", "field-state-r1", "Retain", "small") in st["calls"]
    assert oc.resource_status(env="dev", **scope)["state"] == "progressing"
    st["bindings"][("field-state", "dev")]["status"]["conditions"][0]["status"] = "True"
    assert oc.resource_status(env="dev", **scope) == {"state": "ready", "message": ""}
    assert oc.resource_status(env="staging", **scope)["state"] == "absent"

    # A changed resource gets a new release; staging gets what dev runs, not the newest.
    oc.apply_resource(name="field-state", type="dynamodb-table", parameters={**params, "ttl_attribute": "x"},
                      project="farm")
    rec = oc.bind_resource(env="staging", size="small", actor="agent:staging", actor_type="agent", **scope)
    assert rec["release"] == "field-state-r1"
    with pytest.raises(PermissionError):
        oc.bind_resource(env="production", size="medium", actor="agent:x", actor_type="agent", **scope)
    oc.bind_resource(env="dev", size="small", actor="agent:dev", actor_type="agent", **scope)
    assert ("rebind", "field-state-dev", "field-state-r2") in st["calls"]
    st["bindings"][("field-state", "staging")]["status"]["conditions"] = [
        {"type": "Ready", "status": "False", "reason": "ApplyFailed", "message": "AccessDenied"}]
    assert oc.resource_status(env="staging", **scope) == {"state": "failed", "message": "AccessDenied"}

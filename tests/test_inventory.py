"""The resource inventory: what each product runs per environment, read live from OpenChoreo (and
AWS tags), with the identifiers to work on it directly and production against the lower lanes."""

import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import InMemorySaver

from vrixel_sdlc.api.app import create_app
from vrixel_sdlc.config import load_settings
from vrixel_sdlc.engine import FeatureEngine
from vrixel_sdlc.ops import inventory
from vrixel_sdlc.services import build_services
from vrixel_sdlc.web.auth import AuthConfig

EXAMPLES = Path(__file__).resolve().parents[1] / "examples" / "governance"
DESC = "Keep each field's latest state in DynamoDB"


@pytest.fixture
def eng(tmp_path):
    root = tmp_path / "governance"
    shutil.copytree(EXAMPLES, root)
    s = load_settings(mode="stub", database_url="", artifact_dir=tmp_path / "a", governance_source="local",
                      governance_root=root)
    e = FeatureEngine(build_services(s), InMemorySaver())
    e.services.aws_tags = None
    return e


def released(eng):
    fid = eng.create(product_id="AG", title="Field log", description=DESC)
    assert eng.auto_drive(fid)["status"] == "done"
    return fid


def test_each_environment_and_the_identifiers_to_find_things(eng):
    fid = released(eng)
    eng.services.openchoreo.resource_outputs = {("field-log-dynamodb", "production"): [
        {"name": "tableName", "value": "ag-field-log"},
        {"name": "tableArn", "value": "arn:aws:dynamodb:us-east-2:333333333333:table/ag-field-log"},
        {"name": "credentials", "secret": {"name": "field-log-dynamodb-conn", "key": "password"}}]}
    inv = eng.inventory("AG")
    assert inv["pipeline"] == ["dev", "staging", "production"] and inv["problems"] == []
    api = [r for r in inv["items"] if r["name"] == "agtech-field-api"]
    assert [r["env"] for r in api] == ["dev", "staging", "production"] and api[-1]["feature_id"] == fid
    assert api[-1]["ids"]["kubectl"] == ("kubectl get deploy,svc,pods -A -l openchoreo.dev/project=agtech,"
                                         "openchoreo.dev/component=agtech-field-api,openchoreo.dev/environment=production")
    table = next(r for r in inv["items"] if r["name"] == "field-log-dynamodb" and r["env"] == "production")
    assert (table["type"], table["size"], table["planned_monthly_usd"]) == ("dynamodb-table", "medium", 60.0)
    assert table["ids"]["aws_account"] == "333333333333" and table["ids"]["arn"].endswith("table/ag-field-log")
    assert {"name": "tableName", "value": "ag-field-log"} in table["outputs"]
    assert table["secrets"] == [{"output": "credentials", "name": "field-log-dynamodb-conn", "key": "password"}]
    assert "password" not in str(table["outputs"])
    s = inv["summary"]
    assert s["production"]["components"] == 2 and s["lower"]["components"] == 4
    assert s["production"]["planned_monthly_usd"] > s["per_env"]["dev"]["planned_monthly_usd"]
    assert "not checked" in inv["aws"]


def test_a_rollback_shows_in_the_release_running(eng):
    released(eng)
    oc = eng.services.openchoreo
    before = next(r for r in eng.inventory("AG")["items"] if r["name"] == "agtech-field-api" and r["env"] == "production")
    key = next(k for k in oc.deployments if k.endswith("agtech-field-api"))
    oc.rollback(component=key, env="production", actor="oncall-1", actor_type="human")
    after = next(r for r in eng.inventory("AG")["items"] if r["name"] == "agtech-field-api" and r["env"] == "production")
    assert before["release"] != after["release"]


class FakeTagging:
    def __init__(self, pages):
        self.pages, self.calls = pages, []

    def get_resources(self, **kw):
        self.calls.append(kw)
        return self.pages[len(self.calls) - 1]


def test_aws_tags_fill_in_arns_and_show_what_nobody_manages(eng):
    released(eng)
    arn = "arn:aws:dynamodb:us-east-2:333333333333:table/ag-field-log"
    pages = {"333333333333": [
        {"ResourceTagMappingList": [{"ResourceARN": arn, "Tags": [{"Key": "vrixel:resource", "Value": "field-log-dynamodb"}]}],
         "PaginationToken": "next"},
        {"ResourceTagMappingList": [
            {"ResourceARN": "arn:aws:iam::333333333333:role/agtech-field-api", "Tags": []},  # a workload identity
            {"ResourceARN": "arn:aws:s3:::ag-old-exports", "Tags": [{"Key": "vrixel:feature", "Value": "AG-FEAT-0"}]}]}]}
    clients = {acct: FakeTagging(p) for acct, p in pages.items()}

    def client_for(account, region):
        if account == "111111111111":
            raise PermissionError("role not set up in dev")
        return clients.get(account) or FakeTagging([{"ResourceTagMappingList": []}])

    eng.services.aws_tags = inventory.AwsTags(client_for=client_for, role="vrixel-inventory")
    inv = eng.inventory("AG")
    table = next(r for r in inv["items"] if r["name"] == "field-log-dynamodb" and r["env"] == "production")
    assert table["ids"]["arn"] == arn
    assert inv["unmanaged"] == [{"env": "production", "account": "333333333333", "arn": "arn:aws:s3:::ag-old-exports",
                                 "resource_tag": "", "feature": "AG-FEAT-0"}]
    assert inv["aws"] == "partly checked" and "AWS dev (111111111111): PermissionError" in inv["problems"][0]
    assert clients["333333333333"].calls[1]["PaginationToken"] == "next"
    assert clients["333333333333"].calls[0]["TagFilters"] == [{"Key": "vrixel:product", "Values": ["AG"]}]


def test_daily_snapshots_and_the_api(eng):
    released(eng)
    assert eng.snapshot_inventories(day="2026-11-05") >= 1
    eng.snapshot_inventories(day="2026-11-06")
    client = TestClient(create_app(eng, auth=AuthConfig(mode="dev", secret="x" * 40), web_dist=None))
    client.post("/api/auth/dev-login", json={"login": "farmer", "orgs": ["vrixel-agtech"]})
    inv = client.get("/api/inventory/AG").json()
    assert inv["summary"]["production"]["resources"] == 2
    hist = client.get("/api/inventory/AG/history").json()
    assert [h["day"] for h in hist] == ["2026-11-05", "2026-11-06"] and hist[-1]["per_env"]["production"]["components"] == 2
    csv = client.get("/api/inventory/AG/export.csv")
    assert csv.status_code == 200 and csv.text.startswith("environment,kind,name") and "field-log-dynamodb" in csv.text
    assert client.get("/api/inventory/EMR").status_code == 403


def test_the_live_adapter_reads_bindings_and_never_secret_values():
    from mcp.server import MCPServer

    from vrixel_sdlc.integrations.mcp_client import McpHttp
    from vrixel_sdlc.integrations.openchoreo import OpenChoreoMcp

    server = MCPServer("openchoreo")

    @server.tool()
    def list_release_bindings(namespace_name: str, project_name: str, component_name: str) -> list:
        return [{"metadata": {"name": f"{component_name}-production"},
                 "spec": {"environment": "production", "releaseName": f"{component_name}-7"},
                 "status": {"conditions": [{"type": "Ready", "status": "True"}]}},
                {"metadata": {"name": f"{component_name}-dev"}, "spec": {"environment": "dev", "releaseName": "x-8"},
                 "status": {"conditions": [{"type": "Ready", "status": "False", "reason": "DeployFailed"}]}}]

    @server.tool()
    def list_resource_release_bindings(namespace_name: str, resource_name: str) -> list:
        return [{"metadata": {"name": f"{resource_name}-production"},
                 "spec": {"environment": "production", "resourceRelease": f"{resource_name}-3",
                          "resourceTypeEnvironmentConfigs": {"size": "large"}},
                 "status": {"conditions": [{"type": "Ready", "status": "True"}],
                            "outputs": [{"name": "endpoint", "value": "db.internal:5432"},
                                        {"name": "password", "secretKeyRef": {"name": "db-conn", "key": "password"}}]}}]

    oc = OpenChoreoMcp(McpHttp("openchoreo", server), default_namespace="vrixel")
    assert oc.component_bindings(component="org/api", project="agtech") == [
        {"env": "production", "release": "api-7", "state": "ready", "binding": "api-production"},
        {"env": "dev", "release": "x-8", "state": "failed", "binding": "api-dev"}]
    [b] = oc.resource_bindings_of(name="db", project="agtech")
    assert (b["env"], b["release"], b["size"], b["state"]) == ("production", "db-3", "large", "ready")
    assert b["outputs"] == [{"name": "endpoint", "value": "db.internal:5432"},
                            {"name": "password", "secret": {"name": "db-conn", "key": "password"}}]

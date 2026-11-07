"""The platform's default OpenChoreo alert rules: every component gets them before it deploys, and
what they raise reaches the tool (production: incidents; elsewhere: G6)."""

import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from vrixel_sdlc.config import load_settings
from vrixel_sdlc.engine import FeatureEngine
from vrixel_sdlc.ops.alarms import InMemoryAlarmBoard, for_feature
from vrixel_sdlc.ops.alert_rules import AlertRulesError, load, missing, traits_for
from vrixel_sdlc.ops.signals import from_openchoreo
from vrixel_sdlc.services import build_services

EXAMPLES = Path(__file__).resolve().parents[1] / "examples" / "governance"
RULES = EXAMPLES / "vrixel-tools" / "sdlc-governance" / "platform" / "alert-rules.yaml"


def test_the_platform_rules_become_traits_per_component():
    doc = load(RULES.read_text())
    traits = traits_for(doc, "farm-field-api")
    assert [t["instanceName"] for t in traits] == [
        "farm-field-api-exceptions", "farm-field-api-exception-surge", "farm-field-api-memory"]
    t = traits[0]
    assert (t["name"], t["kind"]) == ("observability-alert-rule", "ClusterTrait")
    assert t["parameters"]["source"] == {"type": "log", "query": "stack_trace"}
    assert t["parameters"]["condition"] == {"window": "5m", "interval": "1m", "operator": "gt", "threshold": 10}
    assert t["parameters"]["severity"] == "warning" and "trace.id" in t["parameters"]["description"]
    assert traits[2]["parameters"]["source"] == {"type": "metric", "metric": "memory_usage"}
    assert traits_for(doc, "web", "deployment/web-application")  # code running in the cluster
    assert traits_for(doc, "x", "proxy/api") == []
    assert traits_for({}, "x") == [] and load("") == {}
    assert missing([{"instanceName": "farm-field-api-memory", "parameters": {"tuned": True}}], traits) == traits[:2]


@pytest.mark.parametrize("bad", [
    "rules: {x: {source: {type: log}, condition: {threshold: 1}}}",
    "rules: {x: {source: {type: metric, metric: disk}, condition: {threshold: 1}}}",
    "rules: {x: {source: {type: log, query: e}, severity: page, condition: {threshold: 1}}}",
    "rules: {x: {source: {type: log, query: e}, condition: {threshold: '1'}}}",
])
def test_a_bad_rule_is_refused(bad):
    with pytest.raises(AlertRulesError):
        load(bad)


@pytest.fixture
def engine(tmp_path):
    root = tmp_path / "governance"
    shutil.copytree(EXAMPLES, root)
    s = load_settings(mode="stub", database_url="", artifact_dir=tmp_path / "artifacts",
                      governance_source="local", governance_root=root)
    return FeatureEngine(build_services(s), InMemorySaver())


def test_components_get_the_rules_before_they_deploy_and_tuned_rules_stay(engine):
    oc = engine.services.openchoreo
    oc.traits["acme-farm/farm-field-api"] = [{"name": "observability-alert-rule", "kind": "ClusterTrait",
                                      "instanceName": "farm-field-api-exceptions",
                                      "parameters": {"condition": {"threshold": 50}}}]
    fid = engine.start(product_id="FARM", title="Field log",
                       description="Keep each field's latest state in DynamoDB")["feature_id"]
    assert engine.auto_drive(fid)["status"] == "done"
    s = engine._state(fid)
    comps = {d["component"] for d in s["deployments"].values() if d.get("component") and d.get("kind") not in (
        "resource", "store")}
    assert comps
    for c in comps:
        names = [t["instanceName"] for t in oc.traits[c]]
        short = c.rsplit("/", 1)[-1]  # OpenChoreo's component name
        assert {f"{short}-exceptions", f"{short}-exception-surge", f"{short}-memory"} <= set(names)
        assert len(names) == len(set(names))  # added once, though the feature deployed three times
    tuned = next(t for t in oc.traits["acme-farm/farm-field-api"] if t["instanceName"] == "farm-field-api-exceptions")
    assert tuned["parameters"]["condition"]["threshold"] == 50  # left as someone set it
    notes = [e for e in s["events"] if e["kind"] == "alert_rules"]
    assert notes and all(e["node"] == "deploy_dev" for e in notes)


def oc_alert(env, component="farm-field-api", at=None):
    return {"alertName": f"{component}-exceptions", "alertSeverity": "warning", "alertType": "log",
            "alertDescription": "More than 10 exceptions logged in 5 minutes.", "alertThreshold": 10,
            "alertValue": 37, "namespace": "default", "project": "farm", "component": component,
            "environment": env, "alertTimestamp": at or datetime.now(timezone.utc).isoformat()}


def test_production_alerts_open_incidents_and_staging_ones_show_at_g6(engine):
    out = engine.alarm_changes(from_openchoreo(oc_alert("staging")))
    assert out == {"recorded": 1, "incidents": []}
    [iid] = engine.alarm_changes(from_openchoreo(oc_alert("production")))["incidents"]
    inc = engine._state(iid)
    assert inc["severity"] == "sev3" and inc["title"] == "farm-field-api-exceptions"

    state = {"feature_id": "FARM-FEAT-5", "product_id": "FARM", "resources": [],
             "deployments": {"acme-farm/farm-field-api": {"component": "farm-field-api"}}}
    [shown] = for_feature(engine.services.alarms, state)
    assert shown["env"] == "staging" and "37" in shown["says"]
    later = datetime.now(timezone.utc) + timedelta(hours=2)
    assert for_feature(engine.services.alarms, state, now=later) == []  # OpenChoreo never says it cleared


def test_the_board_ignores_other_components():
    board = InMemoryAlarmBoard()
    board.record({**from_openchoreo(oc_alert("staging", component="other"))[0], "product_id": "FARM"})
    state = {"feature_id": "F", "product_id": "FARM", "resources": [],
             "deployments": {"r": {"component": "farm-field-api"}}}
    assert for_feature(board, state) == []


def test_the_openchoreo_adapter_patches_only_the_missing_traits():
    from mcp.server import MCPServer

    from vrixel_sdlc.integrations.mcp_client import McpHttp
    from vrixel_sdlc.integrations.openchoreo import OpenChoreoMcp

    server = MCPServer("openchoreo")
    st = {"traits": [{"name": "observability-alert-rule", "kind": "ClusterTrait", "instanceName": "api-memory",
                      "parameters": {"tuned": True}}], "patches": []}

    @server.tool()
    def get_component(namespace_name: str, project_name: str, component_name: str) -> dict:
        return {"metadata": {"name": component_name}, "spec": {"traits": st["traits"]}}

    @server.tool()
    def patch_component(namespace_name: str, component_name: str, traits: list | None = None) -> dict:
        st["patches"].append((namespace_name, component_name, traits))
        st["traits"] = traits
        return {"metadata": {"name": component_name}}

    oc = OpenChoreoMcp(McpHttp("openchoreo", server), default_namespace="vrixel")
    wanted = traits_for(load(RULES.read_text()), "api")
    assert oc.ensure_traits(component="org/api", traits=wanted, project="farm") == ["api-exceptions",
                                                                                     "api-exception-surge"]
    [(ns, comp, traits)] = st["patches"]
    assert (ns, comp) == ("vrixel", "api") and traits[0]["parameters"] == {"tuned": True} and len(traits) == 3
    assert oc.ensure_traits(component="org/api", traits=wanted, project="farm") == []
    assert len(st["patches"]) == 1

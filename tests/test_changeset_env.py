"""Change-set environments: a change set's pull requests run together before anything merges."""

from datetime import datetime, timedelta, timezone

import pytest

from valxel_sdlc import changeset_env

from test_delivery import external, to_build, wait_idle

FIELD, MOBILE = "acme-farm/farm-field-api", "acme-farm/farm-mobile"


def events(v, node):
    return [e for e in v["events"] if e["node"] == node]


def test_the_change_set_runs_in_its_own_environment_before_the_merge(engine, services):
    v = to_build(engine)  # every gate approved: runs to production
    fid = v["feature_id"]
    oc = services.openchoreo
    heads = {pr["repo"]: pr["head_sha"] for pr in v["pull_requests"].values()}
    pre = [r for r in oc.runs if r["kind"] == "change_set"]
    assert {(r["component"], r["ref"]) for r in pre} == {(FIELD, heads[FIELD]), (MOBILE, heads[MOBILE])}
    order = [e["node"] for e in v["events"]]
    assert order.index("cs_wait") < order.index("merge_train")  # tested together, then merged
    [deleted] = events(v, "merge_train")[:1]
    assert deleted["kind"] == "change_set_env_deleted" and deleted["detail"] == "cs-farm-feat-1"
    assert oc.environments == {} and oc.env_bindings == {}  # nothing left behind
    assert "change_set" in v["delivery"]


def test_a_failure_there_goes_back_to_the_agents_before_main_sees_it(engine, services):
    oc = services.openchoreo
    oc.env_results[(MOBILE, "cs-farm-feat-1")] = "failed"
    v = to_build(engine)
    fid = v["feature_id"]
    fixes = [e for e in v["events"] if e["node"] == "change_set_wait" and e["kind"] == "auto_fix"]
    assert fixes and "with the pull requests together, before merging" in fixes[0]["detail"] + str(
        engine._state(fid).get("feedback"))
    assert not any(pr["state"] == "merged" for pr in engine._state(fid)["pull_requests"].values())
    oc.env_results.clear()  # the agents' fix works
    wait_idle(engine, fid)
    v = engine.auto_drive(fid)
    assert v["status"] == "done" and oc.environments == {}


def test_without_environments_the_change_set_merges_as_before(engine, services):
    services.openchoreo.refuse_environments = True  # this OpenChoreo can't create them
    v = engine.auto_drive(engine.start(product_id="FARM", title="Offline crop log")["feature_id"])
    assert v["status"] == "done"
    [skip] = events(v, "cs_env")
    assert skip["kind"] == "skipped" and "create_environment" in skip["detail"]


def test_a_product_that_requires_them_asks_people(engine, services, monkeypatch):
    monkeypatch.setitem(changeset_env.DEFAULTS, "required", True)
    services.openchoreo.refuse_environments = True
    v = to_build(engine)
    [esc] = engine.pending(v["feature_id"])
    assert esc["type"] == "escalation" and esc["wait"] == "change_set"
    assert "Couldn't set up the change set's environment" in esc["reason"]


def test_too_many_open_environments_skip_rather_than_pile_up(engine, services, monkeypatch):
    monkeypatch.setitem(changeset_env.DEFAULTS, "max_open", 1)
    services.openchoreo.create_environment(name="cs-farm-feat-99", like="dev")
    v = engine.auto_drive(engine.start(product_id="FARM", title="Offline crop log")["feature_id"])
    [skip] = events(v, "cs_env")
    assert skip["kind"] == "skipped" and "1 change-set environments are already open" in skip["detail"]


def test_shared_lanes_and_libraries_get_none():
    class Lane:
        def __init__(self, kind, runtime):
            self.kind, self.runtime = kind, runtime

    state = {"tasks": [{"repo": "a/api", "role": "provider"}, {"repo": "s/lib", "role": "library"},
                       {"repo": "a/acc", "role": "acceptance"}],
             "pull_requests": {k: {"repo": k, "head_sha": "h", "state": "open"} for k in ("a/api", "s/lib", "a/acc")}}
    lanes = {"a/api": Lane("product", {"openchoreo_project": "a", "pipeline": ["dev", "production"]}),
             "s/lib": Lane("shared", None), "a/acc": Lane("product", {"openchoreo_project": "a"})}
    assert [c["repo"] for c in changeset_env.components(state, lanes)] == ["a/api"]
    assert changeset_env.acceptance_head(state) == {"repo": "a/acc", "sha": "h"}
    assert changeset_env.name_for("FARM-FEAT-12") == "cs-farm-feat-12"
    assert changeset_env.feature_of("cs-farm-feat-12") == "FARM-FEAT-12" and changeset_env.feature_of("dev") is None


def test_the_watcher_deletes_environments_left_behind(engine, services):
    oc = services.openchoreo
    now = datetime.now(timezone.utc)
    for name, age in (("cs-farm-feat-7", 1), ("cs-farm-feat-8", 100), ("dev-copy", 500)):
        oc.environments[name] = {"name": name, "created": (now - timedelta(hours=age)).isoformat()}
    active = {"FARM-FEAT-7": True, "FARM-FEAT-8": True}
    deleted = changeset_env.sweep(services, lambda fid: (active.get(fid, False), 48), [None], now=now)
    assert deleted == ["cs-farm-feat-8"]  # 7 is open and young; 8 outlived its 48 hours; dev-copy isn't ours
    oc.environments["cs-farm-feat-9"] = {"name": "cs-farm-feat-9", "created": now.isoformat()}
    assert engine.sweep_change_set_envs() == ["cs-farm-feat-7", "cs-farm-feat-9"]  # no such features here
    assert set(oc.environments) == {"dev-copy"}


def test_governance_validates_the_setting(settings):
    from pathlib import Path

    import yaml

    from valxel_sdlc.governance.manifest import GovernanceError, parse_manifest

    path = Path(settings.governance_root) / "acme-farm" / "farm-governance" / "product.yaml"
    raw = yaml.safe_load(path.read_text())
    entry = settings.registry["FARM"]
    raw["runtime"]["change_sets"] = {"required": True, "max_open": 2}
    product, _ = parse_manifest(yaml.safe_dump(raw), entry, settings.limits, settings.registry, settings.tools)
    assert changeset_env.config(product.runtime) == {"enabled": True, "required": True, "max_open": 2, "ttl_hours": 48}
    raw["runtime"]["change_sets"] = {"max_open": 0, "colour": "blue"}
    with pytest.raises(GovernanceError, match="runtime.change_sets must be a mapping"):
        parse_manifest(yaml.safe_dump(raw), entry, settings.limits, settings.registry, settings.tools)


def test_the_openchoreo_adapter_creates_binds_and_deletes():
    from mcp.server import MCPServer

    from valxel_sdlc.integrations.mcp_client import McpHttp
    from valxel_sdlc.integrations.openchoreo import OpenChoreoMcp

    server = MCPServer("openchoreo")
    st = {"envs": {"dev": {"metadata": {"name": "dev"}, "spec": {"dataPlaneRef": {"name": "plane-a"}}}},
          "bindings": {}, "calls": []}

    @server.tool()
    def get_environment(namespace_name: str, environment_name: str) -> dict:
        if environment_name not in st["envs"]:
            raise ValueError("not found")
        return st["envs"][environment_name]

    @server.tool()
    def create_environment(namespace_name: str, name: str, display_name: str, data_plane_ref: str,
                           is_production: bool, labels: dict) -> dict:
        st["calls"].append(("create", name, data_plane_ref, is_production, labels))
        st["envs"][name] = {"metadata": {"name": name, "creationTimestamp": "2026-11-10T20:00:00Z", "labels": labels}}
        return st["envs"][name]

    @server.tool()
    def list_environments(namespace_name: str) -> dict:
        return {"items": list(st["envs"].values())}

    @server.tool()
    def delete_environment(namespace_name: str, environment_name: str) -> dict:
        st["calls"].append(("delete", environment_name))
        st["envs"].pop(environment_name)
        return {}

    @server.tool()
    def get_component(namespace_name: str, project_name: str, component_name: str) -> dict:
        return {"status": {"latestRelease": {"name": f"{component_name}-pr7"}}}

    @server.tool()
    def list_release_bindings(namespace_name: str, project_name: str, component_name: str) -> dict:
        return {"items": [b for b in st["bindings"].values() if b["component"] == component_name]}

    @server.tool()
    def create_release_binding(namespace_name: str, project_name: str, component_name: str, environment: str,
                               release_name: str) -> dict:
        b = {"name": f"{component_name}-{environment}", "component": component_name, "environment": environment,
             "releaseName": release_name}
        st["bindings"][(component_name, environment)] = b
        return b

    @server.tool()
    def delete_release_binding(namespace_name: str, project_name: str, component_name: str, binding_name: str) -> dict:
        st["calls"].append(("unbind", binding_name))
        st["bindings"] = {k: v for k, v in st["bindings"].items() if v["name"] != binding_name}
        return {}

    oc = OpenChoreoMcp(McpHttp("openchoreo", server), default_namespace="valxel")
    oc.create_environment(name="cs-farm-feat-1", like="dev", project="farm", labels={"valxel.io/feature": "FARM-FEAT-1"})
    assert st["calls"][0] == ("create", "cs-farm-feat-1", "plane-a", False,
                              {"valxel.io/change-set": "true", "valxel.io/feature": "FARM-FEAT-1"})
    oc.create_environment(name="cs-farm-feat-1", like="dev", project="farm")  # a retry: already there
    assert len([c for c in st["calls"] if c[0] == "create"]) == 1
    assert [e["name"] for e in oc.list_environments()] == ["dev", "cs-farm-feat-1"]
    scope = {"component": "acme-farm/farm-field-api", "project": "farm"}
    rec = oc.bind_latest(env="cs-farm-feat-1", actor="agent:change-set", **scope)
    assert rec["release"] == "farm-field-api-pr7"
    with pytest.raises(ValueError, match="pipeline environment"):
        oc.bind_latest(env="dev", actor="agent:change-set", **scope)
    oc.unbind(env="cs-farm-feat-1", **scope)
    oc.delete_environment(name="cs-farm-feat-1")
    assert st["calls"][-2:] == [("unbind", "farm-field-api-cs-farm-feat-1"), ("delete", "cs-farm-feat-1")]

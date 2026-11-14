"""Lite mode: workload.yaml as Compose, and ComposeRuntime behind the OpenChoreo interface."""

from __future__ import annotations

import inspect
import json
import subprocess
from types import SimpleNamespace

import pytest
import yaml

from valxel_sdlc.integrations.openchoreo import OpenChoreo
from valxel_sdlc.lite.builds import StubBuilds
from valxel_sdlc.lite.docker import DockerCli, DockerError, StubDocker
from valxel_sdlc.lite.runtime import ComposeRuntime, DictSecrets, LiteUnsupported
from valxel_sdlc.lite.state import FileState
from valxel_sdlc.lite.workload import WorkloadError, compose_file, to_service

DIGEST = "ghcr.io/acme/x@sha256:" + "a" * 64

SERVICE = {"metadata": {"name": "emr-chart-api"},
           "endpoints": [{"name": "api", "port": 8080, "type": "HTTP", "basePath": "/api", "visibility": ["project"]}],
           "configurations": {"env": [{"name": "APP_AUDIENCE", "value": "emr-api"}]}}
WEB = {"metadata": {"name": "emr-web"}, "endpoints": [{"name": "web", "port": 8080, "visibility": ["project"]}]}
BFF = {"metadata": {"name": "emr-bff"},
       "endpoints": [{"name": "web", "port": 8080, "visibility": ["external"]}],
       "dependencies": {"endpoints": [
           {"component": "emr-web", "name": "web", "visibility": "project", "envBindings": {"address": "WEB_URL"}},
           {"component": "emr-chart-api", "name": "api", "visibility": "project", "envBindings": {"address": "API_URL"}},
           {"component": "emr-agent", "name": "api", "visibility": "project", "envBindings": {"address": "AGENT_URL"}}]},
       "configurations": {"env": [
           {"name": "BFF_CLIENT_SECRET", "valueFrom": {"secretKeyRef": {"name": "emr-bff-oidc", "key": "client-secret"}}},
           {"name": "MANAGEMENT_PORT", "value": "8081"}]}}
PEERS = {"emr-chart-api": SERVICE, "emr-web": WEB, "emr-bff": BFF}


def secrets(name, key):
    return {("emr-bff-oidc", "client-secret"): "s3cret"}.get((name, key))


# ------------------------------------------------------------------ workload translation

def test_bff_reaches_only_what_it_declares():
    svc = to_service(BFF, component="acme/emr-bff", image=DIGEST, env="dev", peers=PEERS, secrets=secrets)
    assert svc["environment"] == {"API_URL": "http://emr-chart-api:8080/api", "BFF_CLIENT_SECRET": "s3cret",
                                  "MANAGEMENT_PORT": "8081", "WEB_URL": "http://emr-web:8080"}
    assert set(svc["networks"]) == {"to-emr-bff", "to-emr-web", "to-emr-chart-api", "edge"}
    assert "AGENT_URL" not in svc["environment"]  # the product has no agent: no address, no route
    assert svc["labels"]["dev.valxel.port.web"] == "8080"
    assert "ports" not in svc  # nothing published on the VM; the proxy reaches it on the edge network
    assert svc["cap_drop"] == ["ALL"]


def test_an_undeclared_call_has_no_route():
    web = to_service(WEB, component="emr-web", image=DIGEST, env="dev", peers=PEERS, secrets=secrets)
    api = to_service(SERVICE, component="emr-chart-api", image=DIGEST, env="dev", peers=PEERS, secrets=secrets)
    assert set(web["networks"]) == {"to-emr-web"}
    assert "to-emr-web" not in api["networks"] and "to-emr-chart-api" not in web["networks"]
    assert api["networks"]["to-emr-chart-api"] == {"aliases": ["emr-chart-api"]}


def test_images_run_pinned_by_digest():
    with pytest.raises(WorkloadError, match="pinned by digest"):
        to_service(WEB, component="emr-web", image="ghcr.io/acme/emr-web:latest", env="dev", peers=PEERS,
                   secrets=secrets)


def test_a_missing_secret_or_resource_output_stops_the_deploy():
    wl = {**SERVICE, "dependencies": {"resources": [{"ref": "charts", "envBindings": {"url": "CHARTS_URL"}}]}}
    with pytest.raises(WorkloadError, match="resource charts.url"):
        to_service(wl, component="emr-chart-api", image=DIGEST, env="dev", peers=PEERS, secrets=secrets)
    with pytest.raises(WorkloadError, match="secret emr-bff-oidc/client-secret"):
        to_service(BFF, component="emr-bff", image=DIGEST, env="dev", peers=PEERS, secrets=lambda n, k: None)
    ok = to_service(wl, component="emr-chart-api", image=DIGEST, env="dev", peers=PEERS, secrets=secrets,
                    resource_outputs={"charts": {"url": "postgres://charts"}})
    assert ok["environment"]["CHARTS_URL"] == "postgres://charts"


def test_capacity_becomes_replicas_cpu_and_memory():
    cap = {"replicas": 2, "resources": {"requests": {"cpu": "500m", "memory": "512Mi"}, "limits": {"memory": "512Mi"}}}
    svc = to_service(WEB, component="emr-web", image=DIGEST, env="production", peers=PEERS, secrets=secrets,
                     capacity=cap)
    assert (svc["deploy"], svc["cpus"], svc["mem_limit"]) == ({"replicas": 2}, 0.5, "512m")


def test_each_environment_has_its_own_edge_network():
    svc = to_service(BFF, component="emr-bff", image=DIGEST, env="dev", peers=PEERS, secrets=secrets)
    f = compose_file({"emr-bff": svc}, project="emr-dev")
    assert f["networks"]["edge"] == {"external": True, "name": "edge-emr-dev"}
    assert f["networks"]["to-emr-web"] == {}


# ------------------------------------------------------------------ the runtime

def runtime(**kw):
    builds = StubBuilds()
    builds.workloads.update({"acme/emr-chart-api": SERVICE, "acme/emr-web": WEB, "acme/emr-bff": BFF})
    vm = StubDocker()
    rt = ComposeRuntime(default=vm, builds=builds,
                        secrets=DictSecrets({("emr-bff-oidc", "client-secret"): "s3cret"}), **kw)
    return rt, vm, builds


def build(rt, component, ref="abc123"):
    rt.register_component(component=component, project="emr", namespace=None, component_type="service",
                          workflow="build", repo_url=f"https://github.com/{component}", branch="main")
    run = rt.run_workflow(component=component, ref=ref, kind="pr")
    return rt.run_status(run_id=run["run_id"], component=component)


def test_build_then_dev_then_staging_then_production():
    rt, vm, _ = runtime()
    s = build(rt, "acme/emr-chart-api")
    assert (s["status"], s["release"]) == ("succeeded", "emr-chart-api-r1")
    assert "image" not in s and "workload" not in s

    rec = rt.promote(component="acme/emr-chart-api", env="dev", actor="agent:build", actor_type="agent")
    assert rec["release"] == "emr-chart-api-r1"
    svc = vm.applied["emr-dev"]["services"]["emr-chart-api"]
    assert svc["image"].startswith("ghcr.io/example/emr-chart-api@sha256:")
    assert rt.deployment(component="acme/emr-chart-api", env="dev")["state"] == "ready"
    assert rt.environment("acme/emr-chart-api") == "dev"

    with pytest.raises(ValueError, match="doesn't run in 'staging'"):
        rt.promote(component="acme/emr-chart-api", env="production", actor="human:sunil", actor_type="human")
    rt.promote(component="acme/emr-chart-api", env="staging", actor="agent:verify", actor_type="agent")
    with pytest.raises(PermissionError, match="VX-3"):
        rt.promote(component="acme/emr-chart-api", env="production", actor="agent:release", actor_type="agent")
    rt.promote(component="acme/emr-chart-api", env="production", actor="human:sunil", actor_type="human")
    assert set(vm.applied) == {"emr-dev", "emr-staging", "emr-production"}
    assert [b["env"] for b in rt.component_bindings(component="acme/emr-chart-api")] == ["dev", "production", "staging"]


def test_production_on_its_own_vm():
    prod = StubDocker()
    rt, vm, _ = runtime(hosts={"emr-prod": prod})
    services = SimpleNamespace(openchoreo=rt)
    from valxel_sdlc.environments import environments

    with pytest.raises(PermissionError):
        rt.ensure_environment(name="production", data_plane="emr-prod", production=True, actor="agent:setup")
    done = environments(services, product_id="EMR", approver="sunil")
    assert done["created"]["production"] == "created" and done["approved_by"] == "sunil"
    build(rt, "acme/emr-web")
    for env, who, kind in (("dev", "agent:a", "agent"), ("staging", "agent:a", "agent"), ("production", "human:sunil", "human")):
        rt.promote(component="acme/emr-web", env=env, actor=who, actor_type=kind)
    assert set(vm.applied) == {"emr-dev", "emr-staging"} and set(prod.applied) == {"emr-production"}


def test_a_crashing_container_is_a_failed_deployment():
    rt, vm, _ = runtime()
    build(rt, "acme/emr-web")
    rt.promote(component="acme/emr-web", env="dev", actor="agent:a", actor_type="agent")
    vm.states[("emr-dev", "emr-web")] = "restarting"
    vm.log_lines[("emr-dev", "emr-web")] = ["boom: port in use"]
    assert rt.deployment(component="acme/emr-web", env="dev")["state"] == "failed"
    seen = rt.observe(component="acme/emr-web", env="dev")
    assert seen["available"] is False and seen["logs"] == ["boom: port in use"]
    vm.states[("emr-dev", "emr-web")] = "running"
    vm.health[("emr-dev", "emr-web")] = "starting"
    assert rt.deployment(component="acme/emr-web", env="dev")["state"] == "progressing"


def test_rollback_restores_the_previous_release():
    rt, vm, _ = runtime()
    build(rt, "acme/emr-web", "c1")
    rt.promote(component="acme/emr-web", env="dev", actor="agent:a", actor_type="agent")
    first = vm.applied["emr-dev"]["services"]["emr-web"]["image"]
    build(rt, "acme/emr-web", "c2")
    rt.promote(component="acme/emr-web", env="dev", actor="agent:a", actor_type="agent")
    assert vm.applied["emr-dev"]["services"]["emr-web"]["image"] != first
    with pytest.raises(PermissionError):
        rt.rollback(component="acme/emr-web", env="dev", actor="agent:a", actor_type="agent")
    rec = rt.rollback(component="acme/emr-web", env="dev", actor="human:sunil", actor_type="human")
    assert (rec["from"], rec["to"]) == ("emr-web-r2", "emr-web-r1")
    assert vm.applied["emr-dev"]["services"]["emr-web"]["image"] == first
    with pytest.raises(ValueError, match="no earlier release"):
        rt.rollback(component="acme/emr-web", env="dev", actor="human:sunil", actor_type="human")


def test_a_change_docker_refuses_is_not_remembered():
    rt, vm, _ = runtime()
    build(rt, "acme/emr-web")
    vm.fail_up = "no space left on device"
    with pytest.raises(DockerError, match="no space"):
        rt.promote(component="acme/emr-web", env="dev", actor="agent:a", actor_type="agent")
    assert rt.deployment(component="acme/emr-web", env="dev") == {"state": "absent"}
    assert rt.environment("acme/emr-web") is None


def test_a_missing_secret_refuses_the_deploy():
    rt, vm, _ = runtime()
    rt.secrets = DictSecrets()
    for c in ("acme/emr-web", "acme/emr-chart-api", "acme/emr-bff"):
        build(rt, c)
    rt.promote(component="acme/emr-web", env="dev", actor="agent:a", actor_type="agent")
    with pytest.raises(WorkloadError, match="emr-bff-oidc"):
        rt.promote(component="acme/emr-bff", env="dev", actor="agent:a", actor_type="agent")
    assert set(vm.applied["emr-dev"]["services"]) == {"emr-web"}


def test_capacity_is_applied_to_the_running_environment():
    rt, vm, _ = runtime()
    build(rt, "acme/emr-web")
    rt.promote(component="acme/emr-web", env="dev", actor="agent:a", actor_type="agent")
    rt.configure_binding(component="acme/emr-web", env="dev",
                         configs={"replicas": 1, "resources": {"requests": {"cpu": "250m"}, "limits": {"memory": "256Mi"}}},
                         traits={"autoscaler": {"minReplicas": 1, "maxReplicas": 3}})
    svc = vm.applied["emr-dev"]["services"]["emr-web"]
    assert (svc["cpus"], svc["mem_limit"]) == (0.25, "256m")


def test_change_set_environment_lives_and_goes():
    rt, vm, _ = runtime()
    build(rt, "acme/emr-web")
    rt.promote(component="acme/emr-web", env="dev", actor="agent:a", actor_type="agent")
    rt.create_environment(name="cs-42", like="dev", project="emr", labels={"valxel.io/change-set": "42"})
    rec = rt.bind_latest(component="acme/emr-web", env="cs-42", actor="agent:cs")
    assert rec["release"] == "emr-web-r1" and "emr-cs-42" in vm.applied
    assert [e["name"] for e in rt.list_environments()] == ["cs-42"]
    rt.delete_environment(name="cs-42")
    assert "emr-cs-42" not in vm.applied and ("down", "emr-cs-42") in vm.history
    assert rt.list_environments() == []
    assert "emr-dev" in vm.applied


def test_managed_resources_are_refused_clearly():
    rt, _, _ = runtime()
    assert rt.apply_resource(name="charts", type="postgres", parameters={}, project="emr")["change"] == "created"
    with pytest.raises(LiteUnsupported, match="Postgres and Kafka"):
        rt.bind_resource(name="charts", env="dev", size="small", actor="agent:a", actor_type="agent", project="emr")
    with pytest.raises(PermissionError):
        rt.bind_resource(name="charts", env="production", size="small", actor="agent:a", actor_type="agent", project="emr")


def test_state_survives_a_restart(tmp_path):
    vm, builds = StubDocker(), StubBuilds()
    rt = ComposeRuntime(default=vm, builds=builds, state=FileState(tmp_path / "lite.json"))
    build(rt, "acme/emr-web")
    rt.promote(component="acme/emr-web", env="dev", actor="agent:a", actor_type="agent")
    again = ComposeRuntime(default=vm, builds=builds, state=FileState(tmp_path / "lite.json"))
    assert again.deployment(component="acme/emr-web", env="dev")["release"] == "emr-web-r1"


def test_compose_runtime_matches_the_openchoreo_interface():
    for name, member in inspect.getmembers(OpenChoreo, predicate=inspect.isfunction):
        if name.startswith("_"):
            continue
        ours = getattr(ComposeRuntime, name, None)
        assert ours is not None, f"ComposeRuntime lacks {name}"
        want = [p for p in inspect.signature(member).parameters if p != "self"]
        have = [p for p in inspect.signature(ours).parameters if p != "self"]
        assert want == have, f"{name}: {have} != {want}"


# ------------------------------------------------------------------ the Docker CLI

class Recorder:
    def __init__(self, outputs=None):
        self.calls, self.outputs = [], outputs or {}

    def __call__(self, args, *, input=None, capture_output, text, env, timeout):
        self.calls.append({"args": args, "input": input, "host": env.get("DOCKER_HOST")})
        out = next((v for k, v in self.outputs.items() if k in " ".join(args)), "")
        return subprocess.CompletedProcess(args, 0, out, "")


def test_docker_cli_applies_the_compose_file_over_ssh():
    rec = Recorder()
    cli = DockerCli("ssh://valxel@vm", runner=rec)
    cli.up("emr-dev", {"services": {"web": {"image": DIGEST}}}, networks=("edge-emr-dev",))
    assert [c["args"][1:3] for c in rec.calls] == [["network", "ls"], ["network", "create"], ["compose", "-p"]]
    up = rec.calls[-1]
    assert up["args"] == ["docker", "compose", "-p", "emr-dev", "-f", "-", "up", "-d", "--remove-orphans",
                          "--pull", "missing"]
    assert yaml.safe_load(up["input"])["services"]["web"]["image"] == DIGEST
    assert {c["host"] for c in rec.calls} == {"ssh://valxel@vm"}


def test_docker_cli_reads_ps_in_either_format():
    lines = "\n".join(json.dumps({"Service": s, "State": "running", "Health": ""}) for s in ("a", "b"))
    assert [r["service"] for r in DockerCli(runner=Recorder({"ps": lines})).ps("p")] == ["a", "b"]
    arr = json.dumps([{"Service": "a", "State": "Exited", "ExitCode": 1}])
    assert DockerCli(runner=Recorder({"ps": arr})).ps("p")[0]["state"] == "exited"


def test_docker_cli_reports_failures():
    def fail(args, **kw):
        return subprocess.CompletedProcess(args, 1, "", "Cannot connect to the Docker daemon")
    with pytest.raises(DockerError, match="Cannot connect"):
        DockerCli(runner=fail).ps("p")


# ------------------------------------------------------------------ builds from the OpenChoreo build plane

def fake_build_plane():
    from mcp.server import MCPServer

    server = MCPServer("openchoreo")
    st = {"phase": "Running", "latest": "emr-web-a1b2", "image": DIGEST}

    @server.tool()
    def trigger_workflow_run(namespace_name: str, project_name: str, component_name: str, commit: str = "") -> dict:
        return {"name": f"{component_name}-run-1"}

    @server.tool()
    def get_workflow_run_status(namespace_name: str, run_name: str) -> dict:
        ok = st["phase"] == "Succeeded"
        return {"status": {"conditions": [{"type": "WorkflowSucceeded", "status": "True"}] if ok else []}}

    @server.tool()
    def get_component(namespace_name: str, project_name: str, component_name: str) -> dict:
        return {"status": {"latestRelease": {"name": st["latest"]}}}

    @server.tool()
    def get_component_release(namespace_name: str, project_name: str, component_name: str, release_name: str) -> dict:
        return {"metadata": {"name": release_name},
                "spec": {"workload": {"containers": {"main": {"image": st["image"], "env": []}}}}}

    return server, st


def test_lite_releases_come_from_the_openchoreo_build_plane():
    from valxel_sdlc.integrations.github import StubGitHub
    from valxel_sdlc.integrations.mcp_client import McpHttp
    from valxel_sdlc.integrations.openchoreo import OpenChoreoMcp
    from valxel_sdlc.lite.builds import OpenChoreoBuilds

    server, st = fake_build_plane()
    oc = OpenChoreoMcp(McpHttp("openchoreo", server), default_namespace="valxel")
    gh = StubGitHub()
    gh.files[("acme/emr-web", "c0ffee", "workload.yaml")] = yaml.safe_dump(WEB)
    vm = StubDocker()
    rt = ComposeRuntime(default=vm, builds=OpenChoreoBuilds(oc, gh))
    rt.register_component(component="acme/emr-web", project="emr", namespace="valxel", component_type="web",
                          workflow="build", repo_url="https://github.com/acme/emr-web", branch="main")
    run = rt.run_workflow(component="acme/emr-web", ref="c0ffee", kind="main")
    assert rt.run_status(run_id=run["run_id"], component="acme/emr-web")["status"] == "running"
    st["phase"] = "Succeeded"
    done = rt.run_status(run_id=run["run_id"], component="acme/emr-web")
    assert (done["status"], done["release"]) == ("succeeded", "emr-web-r1")
    rt.promote(component="acme/emr-web", env="dev", actor="agent:a", actor_type="agent")
    svc = vm.applied["emr-dev"]["services"]["emr-web"]
    assert svc["image"] == DIGEST and svc["networks"] == {"to-emr-web": {"aliases": ["emr-web"]}}


def test_a_build_without_a_workload_file_fails_clearly():
    from valxel_sdlc.integrations.github import StubGitHub
    from valxel_sdlc.integrations.mcp_client import McpHttp
    from valxel_sdlc.integrations.openchoreo import OpenChoreoMcp
    from valxel_sdlc.lite.builds import OpenChoreoBuilds

    server, st = fake_build_plane()
    st["phase"] = "Succeeded"
    builds = OpenChoreoBuilds(OpenChoreoMcp(McpHttp("openchoreo", server)), StubGitHub())
    run = builds.start(component="acme/emr-web", ref="c0ffee", kind="main", repo_url="", project="emr")
    s = builds.status(run_id=run["run_id"], component="acme/emr-web", ref="c0ffee", project="emr")
    assert s["status"] == "failed" and "no workload.yaml at c0ffee" in s["message"]
    st["image"] = "ghcr.io/acme/emr-web:latest"
    from valxel_sdlc.integrations.mcp_client import McpError
    with pytest.raises(McpError, match="pinned by digest"):
        builds.status(run_id=run["run_id"], component="acme/emr-web", ref="c0ffee", project="emr")

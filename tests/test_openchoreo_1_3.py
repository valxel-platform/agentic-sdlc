"""The platform's OpenChoreo resources against OpenChoreo 1.3.3 (T-72): every component type the repo
standards name exists, builds only with the platform's workflow, reads the image where 1.3 puts it,
labels pods with their role for the network policies, and serves endpoints through a Service; the
build workflow takes 1.3's repository parameters and ends in OpenChoreo's generate-workload step;
logs and metrics come from 1.3's observer, a server of its own."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import yaml

from valxel_sdlc.integrations.mcp_client import McpError
from valxel_sdlc.integrations.openchoreo import OBSERVER_TOOLS, OpenChoreoMcp
from valxel_sdlc.laptop import cluster
from valxel_sdlc.provisioning import component_parameters

ROOT = Path(__file__).resolve().parents[1]
OC = ROOT / "examples" / "governance" / "valxel-platform" / "sdlc-governance" / "platform" / "openchoreo"
TYPES = {d["metadata"]["name"]: d for d in yaml.safe_load_all((OC / "component-types.yaml").read_text())}
FLOW = yaml.safe_load((OC / "build-workflow.yaml").read_text())


def _pod(t):
    for r in t["spec"]["resources"]:
        tpl = r["template"]
        if tpl["kind"] == "Deployment":
            return tpl["spec"]["template"]
        if tpl["kind"] == "CronJob":
            return tpl["spec"]["jobTemplate"]["spec"]["template"]
    raise AssertionError("no workload")


def test_every_type_the_repo_standards_name_exists_and_builds_with_the_platforms_workflow():
    wanted, flows = cluster.repo_standards((ROOT / "config" / "repos.yaml").read_text())
    assert wanted <= set(TYPES) and flows == {"docker"}
    for name, t in TYPES.items():
        assert t["kind"] == "ClusterComponentType" and t["apiVersion"] == "openchoreo.dev/v1alpha1"
        assert t["spec"]["allowedWorkflows"] == [{"kind": "ClusterWorkflow", "name": "docker"}], name


def test_types_follow_1_3s_workload_model():
    text = (OC / "component-types.yaml").read_text()
    assert "workload.containers." not in text  # the 1.2 shape
    for name, t in TYPES.items():
        pod = _pod(t)
        [c] = pod["spec"]["containers"]
        assert c["image"] == "${workload.container.image}", name
        assert "dependencies.toContainerEnvs()" in c["env"] and c["envFrom"] == "${configurations.toContainerEnvFrom()}"
        assert "configurations.toVolumes()" in pod["spec"]["volumes"]
        assert "valxel.io/role" in pod["metadata"]["labels"], name  # what the network policies select on
        assert "role" in t["spec"]["parameters"]["openAPIV3Schema"]["properties"], name
        ids = {r["id"] for r in t["spec"]["resources"]}
        assert {"env-config", "file-config", "secret-env-external", "secret-file-external"} <= ids, name
    for name in ("service", "web-app", "agent", "code-sandbox", "connector"):
        assert "service" in {r["id"] for r in TYPES[name]["spec"]["resources"]}, name


def test_no_type_runs_as_root_or_gains_privileges():
    for name, t in TYPES.items():
        pod = _pod(t)["spec"]
        c = pod["containers"][0]
        assert pod["securityContext"]["runAsNonRoot"] is True, name
        assert pod["securityContext"]["seccompProfile"] == {"type": "RuntimeDefault"}, name
        assert c["securityContext"]["allowPrivilegeEscalation"] is False, name
        assert c["securityContext"]["capabilities"] == {"drop": ["ALL"]}, name


def test_build_parameters_reach_scripts_as_variables_never_spliced_in():
    for tpl in FLOW["spec"]["runTemplate"]["spec"]["templates"][1:]:
        c = tpl.get("container") or {}
        if c:
            assert c.get("command") == ["sh", "-c"], tpl["name"]
            assert "{{" not in " ".join(c.get("args") or []), tpl["name"]


def test_the_platforms_additions_are_kept():
    for name in ("service", "web-app", "worker", "agent", "code-sandbox", "connector"):
        dep = next(r for r in TYPES[name]["spec"]["resources"] if r["id"] == "deployment")["template"]["spec"]
        assert dep["strategy"]["rollingUpdate"]["maxUnavailable"] == 0, name
        assert dep["template"]["spec"]["topologySpreadConstraints"], name
        assert "pdb" in {r["id"] for r in TYPES[name]["spec"]["resources"]}, name
    for name in ("agent", "code-sandbox", "connector", "scheduled-task"):
        pod = _pod(TYPES[name])["spec"]
        assert pod["securityContext"]["runAsNonRoot"] and pod["containers"][0]["securityContext"]["readOnlyRootFilesystem"]
    assert _pod(TYPES["agent"])["spec"]["automountServiceAccountToken"] is False
    assert "gvisor" in _pod(TYPES["agent"])["spec"]["runtimeClassName"]
    env = TYPES["agent"]["spec"]["environmentConfigs"]["openAPIV3Schema"]["properties"]
    assert {"allowedTools", "readOnlyTools", "egressCIDRs"} <= set(env)
    assert "limits" in env["resources"]["properties"] and "cpu" not in env["resources"]["properties"]["limits"]["properties"]


def test_the_build_takes_1_3s_repository_and_ends_in_generate_workload():
    assert FLOW["kind"] == "ClusterWorkflow" and FLOW["spec"]["workflowPlaneRef"]["kind"] == "ClusterWorkflowPlane"
    repo = FLOW["spec"]["parameters"]["openAPIV3Schema"]["properties"]["repository"]
    assert repo["properties"]["url"]["x-openchoreo-component-parameter-repository-url"] is True
    assert set(repo["properties"]["revision"]["properties"]) == {"branch", "commit"}
    steps = [s[0]["name"] for s in FLOW["spec"]["runTemplate"]["spec"]["templates"][0]["steps"]]
    assert steps == ["checkout", "build-image", "push", "scan-image", "sbom", "sign", "attest-provenance",
                     "generate-workload"]
    gen = FLOW["spec"]["runTemplate"]["spec"]["templates"][0]["steps"][-1][0]
    assert gen["templateRef"] == {"name": "generate-workload", "clusterScope": True, "template": "generate-workload-cr"}
    args = {p["name"] for p in FLOW["spec"]["runTemplate"]["spec"]["arguments"]["parameters"]}
    assert {"project-name", "component-name", "namespace-name", "app-path"} <= args  # what generate-workload reads
    text = (OC / "build-workflow.yaml").read_text()
    assert "name: workspace" in text and "/mnt/vol/source" in text and "/work/" not in text


def test_the_tool_registers_each_repo_with_its_role():
    assert component_parameters({}, "general", "bff") == {"role": "bff"}
    agent = {"isolation": {"phi": "gvisor"}}
    assert component_parameters(agent, "phi", "agent") == {"role": "agent", "isolation": "gvisor"}


class Observer:
    name = "openchoreo-observer"

    def __init__(self, fail=()):
        self.calls, self.fail = [], set(fail)

    def tool_names(self):
        return list(OBSERVER_TOOLS)

    def call(self, tool, args):
        self.calls.append((tool, args))
        if tool in self.fail:
            raise McpError("down")
        if tool == "query_component_logs":
            return {"logs": [{"timestamp": "2026-11-21T03:00:00Z", "level": "ERROR", "log": "db timeout"}], "total": 1}
        return {"cpuUsage": [{"timestamp": "t", "value": 0.4}] * 20, "requestCount": [{"timestamp": "t", "value": 3}]}


class Api:
    name = "openchoreo"

    def tool_names(self):
        return []

    def call(self, tool, args):
        raise McpError("the API server has no log tools in 1.3")


def test_logs_and_metrics_come_from_1_3s_observer():
    obs = Observer()
    oc = OpenChoreoMcp(Api(), default_namespace="acme", observer=obs)
    got = oc.observe(component="cln-intake-api", env="production", project="clinic", minutes=30)
    assert got["available"] and got["logs"] == ["2026-11-21T03:00:00Z ERROR db timeout"]
    assert len(got["metrics"]["resources"]["cpuUsage"]) == 12  # the last hour at 5-minute steps
    [args] = [a for t, a in obs.calls if t == "query_component_logs"]
    assert args["namespace"] == "acme" and args["component"] == "cln-intake-api" and args["environment"] == "production"
    start, end = (datetime.strptime(args[k], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC) for k in ("start_time", "end_time"))
    assert (end - start).total_seconds() == 30 * 60
    assert {c[0] for c in obs.calls} == set(OBSERVER_TOOLS)
    down = OpenChoreoMcp(Api(), observer=Observer(fail=OBSERVER_TOOLS)).observe(component="c", env="production")
    assert down == {"available": False, "logs": [], "metrics": {}}  # triage works from the alerts alone


def test_the_laptop_check_names_missing_observer_tools():
    found = cluster.check(tool_names=set(), installed_types=set(), installed_flows=set(), wanted_types=set(),
                          wanted_flows=set(), observer_tools={"query_component_logs"})
    assert [f for f in found if f.startswith("Observer")] == [
        "Observer MCP tool missing: query_resource_metrics", "Observer MCP tool missing: query_http_metrics"]
    assert not [f for f in cluster.check(tool_names=set(), installed_types=set(), installed_flows=set(),
                                         wanted_types=set(), wanted_flows=set()) if f.startswith("Observer")]

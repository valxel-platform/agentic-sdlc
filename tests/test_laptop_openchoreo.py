"""The laptop tier with a local OpenChoreo (valxel_sdlc.laptop.cluster), and what the OpenChoreo
client sends to a 1.3 server."""

import hashlib
import io
import json
import subprocess
from pathlib import Path
from urllib.parse import parse_qs

import pytest

from valxel_sdlc import laptop
from valxel_sdlc.config import load_settings
from valxel_sdlc.integrations.mcp_client import ClientCredentials
from valxel_sdlc.integrations.openchoreo import OpenChoreoMcp, tools_used
from valxel_sdlc.laptop import cluster
from valxel_sdlc.services import DeliveryConfigError, default_delivery

EXAMPLES = Path(__file__).resolve().parents[1] / "examples" / "governance"
WORKFLOW_OBJECT = {"type": "object", "properties": {"name": {"type": "string"}, "kind": {"type": "string"},
                                                    "parameters": {"type": "object"}}}


class Server:
    """Just enough of an MCP server: names, schemas, and a record of calls."""

    name = "openchoreo"

    def __init__(self, schemas):
        self.schemas, self.calls = schemas, []

    def tool_names(self):
        return list(self.schemas)

    def tool_schema(self, tool):
        return self.schemas[tool]

    def call(self, tool, args):
        self.calls.append((tool, args))
        if tool == "get_component":
            raise __import__("valxel_sdlc.integrations.mcp_client", fromlist=["McpError"]).McpError("not found")
        return {}


def _props(*names, workflow=None):
    p = {n: {"type": "string"} for n in names}
    if workflow:
        p["workflow"] = workflow
    return {"properties": p}


def test_create_component_sends_openchoreo_1_3_workflow_object_with_the_git_secret():
    server = Server({
        "get_component": _props("namespace_name", "project_name", "component_name"),
        "create_component": _props("namespace_name", "project_name", "name", "component_type", "parameters",
                                   workflow=WORKFLOW_OBJECT),
    })
    oc = OpenChoreoMcp(server, git_secret_ref="valxel-github")
    oc.register_component(component="acme-farm/farm-api", project="farm", namespace=None,
                          component_type="deployment/service", workflow="docker",
                          repo_url="https://github.com/acme-farm/farm-api", branch="main")
    tool, args = server.calls[-1]
    assert tool == "create_component"
    assert args["workflow"] == {"name": "docker", "kind": "ClusterWorkflow", "parameters": {"repository": {
        "url": "https://github.com/acme-farm/farm-api", "revision": {"branch": "main"}, "secretRef": "valxel-github"}}}
    assert args["component_type"] == "deployment/service" and args["name"] == "farm-api"


def test_create_component_keeps_flat_arguments_for_older_servers():
    server = Server({
        "get_component": _props("namespace_name", "project_name", "component_name"),
        "create_component": _props("namespace_name", "project_name", "component_name", "component_type",
                                   "workflow", "repository_url", "branch"),
    })
    OpenChoreoMcp(server).register_component(
        component="acme-farm/farm-api", project="farm", namespace=None, component_type="deployment/service",
        workflow="docker", repo_url="https://github.com/acme-farm/farm-api", branch="main")
    args = server.calls[-1][1]
    assert args["workflow"] == "docker" and args["repository_url"].endswith("farm-api") and args["branch"] == "main"


def test_token_request_names_the_api_it_is_for():
    sent = {}

    class Opener:
        def open(self, req, timeout=None):
            sent.update({k: v[0] for k, v in parse_qs(req.data.decode()).items()})
            return io.BytesIO(json.dumps({"access_token": "t", "expires_in": 3600}).encode())

    creds = ClientCredentials("http://thunder/oauth2/token", "app", "s", resource="http://api/mcp", opener=Opener())
    assert creds.token() == "t"
    assert sent["resource"] == "http://api/mcp" and sent["grant_type"] == "client_credentials"


def test_tools_used_lists_each_call_with_its_alternatives():
    used = tools_used()
    assert ("create_component",) in used and ("trigger_workflow_run",) in used
    assert ("get_workflow_run_status", "get_workflow_run") in used
    assert len(used) == len(set(used))


def test_check_reports_missing_tools_types_and_workflows():
    offered = {n for names in tools_used() for n in names} - {"get_project", "list_projects"}
    found = cluster.check(tool_names=offered, installed_types={"service", "worker"}, installed_flows={"docker"},
                          wanted_types={"service", "web-app"}, wanted_flows={"docker"})
    assert found == ["MCP tool missing: get_project or list_projects", "Component type missing: web-app"]
    assert not cluster.check(tool_names=offered | {"list_projects"}, installed_types={"service", "web-app"},
                             installed_flows={"docker"}, wanted_types={"web-app"}, wanted_flows={"docker"})


def test_repo_standards_reads_what_the_tool_asks_openchoreo_for():
    text = (Path(__file__).resolve().parents[1] / "config" / "repos.yaml").read_text()
    types, flows = cluster.repo_standards(text)
    assert {"service", "web-app", "worker"} <= types and "docker" in flows


def _settings(tmp_path):
    return load_settings(mode="stub", database_url="", artifact_dir=tmp_path / "a", governance_source="local",
                         governance_root=EXAMPLES)


def test_local_delivery_with_openchoreo_uses_its_mcp_api(monkeypatch, tmp_path):
    monkeypatch.setenv("VALXEL_DELIVERY", "local")
    monkeypatch.setenv("VALXEL_LOCAL_RUNTIME", "openchoreo")
    for k, v in cluster.SETTINGS.items():
        monkeypatch.setenv(k, v)
    _, oc = default_delivery(_settings(tmp_path))
    assert isinstance(oc, OpenChoreoMcp) and oc.git_secret_ref == "valxel-github"
    assert oc.server.auth.resource == "http://api.openchoreo.localhost:8080/mcp"

    monkeypatch.delenv("VALXEL_OPENCHOREO_MCP_URL")
    with pytest.raises(DeliveryConfigError, match="VALXEL_LOCAL_RUNTIME=openchoreo needs VALXEL_OPENCHOREO_MCP_URL"):
        default_delivery(_settings(tmp_path))


# ------------------------------------------------------------------ valxel dev cluster


class Kube:
    """Records commands; answers the reads `aliases` makes from OpenChoreo's defaults."""

    def __init__(self):
        self.calls = []

    def __call__(self, cmd, *, input=None, check=True):
        self.calls.append((cmd, input))
        out = ""
        if cmd[-3:] == ["dockerfile-builder", "-o", "json"]:
            out = json.dumps({"apiVersion": "openchoreo.dev/v1alpha1", "kind": "ClusterWorkflow",
                              "metadata": {"name": "dockerfile-builder", "uid": "u1", "resourceVersion": "7"},
                              "spec": {"parameters": {}}, "status": {"x": 1}})
        elif "clustercomponenttypes" in cmd:
            out = json.dumps({"items": [
                {"apiVersion": "openchoreo.dev/v1alpha1", "kind": "ClusterComponentType",
                 "metadata": {"name": n, "uid": n}, "spec": {"allowedWorkflows": [
                     {"kind": "ClusterWorkflow", "name": "dockerfile-builder"}]}}
                for n in ("service", "web-application", "worker")]})
        elif cmd[:4] == ["k3d", "cluster", "list", "-o"]:
            out = "[]"
        return subprocess.CompletedProcess(cmd, 0, stdout=out, stderr="")


def test_the_git_token_reaches_openbao_on_stdin_only():
    kube = Kube()
    cluster.store_git_token("ghp_secret", run=kube)
    assert all("ghp_secret" not in " ".join(cmd) for cmd, _ in kube.calls)
    assert [i for _, i in kube.calls][:2] == ["x-access-token", "ghp_secret"]
    ref = json.loads(kube.calls[-1][1])
    assert ref["kind"] == "SecretReference" and ref["metadata"]["name"] == "valxel-github"
    assert ref["spec"]["template"]["type"] == "kubernetes.io/basic-auth"


def test_aliases_add_the_names_the_repo_standards_use():
    kube = Kube()
    applied = cluster.aliases(run=kube)
    assert "ClusterWorkflow/docker" in applied and "ClusterComponentType/web-app" in applied
    items = {i["metadata"]["name"]: i for i in json.loads(kube.calls[-1][1])["items"]}
    assert items["docker"]["metadata"]["annotations"]["valxel.dev/laptop-alias-of"] == "dockerfile-builder"
    assert "status" not in items["docker"] and "uid" not in items["docker"]["metadata"]
    for t in ("service", "web-app", "web-application", "worker"):
        assert {"kind": "ClusterWorkflow", "name": "docker"} in items[t]["spec"]["allowedWorkflows"]


def test_installer_is_refused_when_its_hash_changes(tmp_path):
    def opener(url, timeout=None):
        return io.BytesIO(b"echo tampered")

    with pytest.raises(RuntimeError, match="hash changed"):
        cluster.fetch_installer(tmp_path / "i.sh", opener=opener)
    assert not (tmp_path / "i.sh").exists()
    assert cluster.INSTALLER.endswith(f"v{cluster.OPENCHOREO_VERSION}/install/k3d/k3d-install.sh")
    assert len(cluster.INSTALLER_SHA256) == 64 and hashlib.sha256(b"").hexdigest() != cluster.INSTALLER_SHA256


def test_up_installs_once_then_points_env_at_openchoreo_and_down_undoes_it(tmp_path, monkeypatch):
    monkeypatch.setattr(laptop, "docker_gid", lambda: "0")
    monkeypatch.setattr(cluster, "missing_tools", list)
    laptop.init(target=tmp_path, anthropic_key="k", github_token="ghp_t")
    installs, kube = [], Kube()
    assert cluster.up(tmp_path, run=kube, installer=lambda **kw: installs.append(1)) == 0
    assert installs == [1]
    env = laptop.read_env(tmp_path / ".env")
    assert env["VALXEL_LOCAL_RUNTIME"] == "openchoreo" and env["VALXEL_PORT"] == "8090"
    assert env["ANTHROPIC_API_KEY"] == "k"  # the rest of .env is kept
    assert laptop.tool_port(tmp_path) == 8090

    cluster.down(tmp_path, run=kube)
    env = laptop.read_env(tmp_path / ".env")
    assert "VALXEL_LOCAL_RUNTIME" not in env and laptop.tool_port(tmp_path) == 8080
    assert (["k3d", "cluster", "delete", "valxel"], None) in kube.calls


def test_compose_passes_openchoreo_settings_but_never_the_model_key():
    import yaml

    spec = yaml.safe_load((laptop.bundle() / "compose.yaml").read_text())
    api = spec["services"]["api"]
    assert set(cluster.SETTINGS) - {"VALXEL_PORT"} <= set(api["environment"])
    assert "api.openchoreo.localhost:host-gateway" in api["extra_hosts"]
    assert api["ports"] == ["127.0.0.1:${VALXEL_PORT:-8080}:8080"]
    assert "ANTHROPIC_API_KEY" not in api["environment"]


def test_projects_and_environments_are_found_by_listing_when_the_server_has_no_get():
    class Lists(Server):
        def call(self, tool, args):
            self.calls.append((tool, args))
            return {"items": [{"name": "farm"}]} if tool == "list_projects" else {"items": [{"name": "dev"}]}

    ns = {"properties": {"namespace_name": {"type": "string"}}}
    server = Lists({"list_projects": ns, "list_environments": ns, "create_project": ns, "create_environment": ns})
    oc = OpenChoreoMcp(server)
    assert oc.ensure_project(project="farm", pipeline="farm-pipeline") == "exists"
    assert oc.ensure_environment(name="dev", data_plane="np", production=False, actor="agent:x") == "exists"
    assert not [t for t, _ in server.calls if t.startswith("create_")]
    assert oc.ensure_project(project="shop", pipeline="shop-pipeline") == "created"


def test_up_with_aws_starts_moto_and_installs_the_aws_catalog_on_the_cluster(tmp_path, monkeypatch):
    """`valxel dev cluster up --aws` (CP-118): AWS mode on, moto up, then platform-resources'
    local/aws/install.py against this cluster, with OpenChoreo's resource types."""
    from valxel_sdlc.laptop import aws

    monkeypatch.setattr(laptop, "docker_gid", lambda: "0")
    monkeypatch.setattr(cluster, "missing_tools", list)
    monkeypatch.delenv("VALXEL_PLATFORM_RESOURCES", raising=False)
    laptop.init(target=tmp_path, anthropic_key="k", github_token="ghp_t")
    kube, installs = Kube(), []
    assert cluster.up(tmp_path, run=kube, installer=lambda **kw: None, aws=True,
                      catalog=lambda cmd: installs.append(cmd) or 0) == 0
    env = laptop.read_env(tmp_path / ".env")
    assert aws.is_on(env) and env["VALXEL_LOCAL_RUNTIME"] == "openchoreo"
    cmds = [c for c, _ in kube.calls]
    assert any(c[:2] == ["docker", "compose"] and c[-1] == "moto" for c in cmds)
    fetch = next(c for c in cmds if "fetch" in c)
    assert fetch[-2:] == [cluster.PLATFORM_RESOURCES, cluster.PLATFORM_RESOURCES_REF]
    (cmd,) = installs
    assert cmd[1].endswith("platform-resources/local/aws/install.py")
    assert cmd[2:] == ["--context", "k3d-valxel", "--moto", "valxel-moto", "--resource-types"]  # Crossplane is there


def test_up_with_aws_installs_crossplane_when_openchoreo_has_none(tmp_path, monkeypatch):
    monkeypatch.setattr(laptop, "docker_gid", lambda: "0")
    monkeypatch.setenv("VALXEL_PLATFORM_RESOURCES", str(tmp_path / "pr"))
    laptop.init(target=tmp_path, anthropic_key="k", github_token="ghp_t")

    def kube(cmd, *, input=None, check=True):
        missing = "crossplane" in cmd and "deployment" in cmd
        return subprocess.CompletedProcess(cmd, 1 if missing else 0, stdout="", stderr="")

    installs = []
    assert cluster.with_aws(tmp_path, run=kube, catalog=lambda cmd: installs.append(cmd) or 0) == 0
    assert installs[0][1] == str(tmp_path / "pr" / "local" / "aws" / "install.py") and installs[0][-1] == "--crossplane"


def test_platform_resources_is_pinned_to_a_commit():
    assert len(cluster.PLATFORM_RESOURCES_REF) == 40 and int(cluster.PLATFORM_RESOURCES_REF, 16) >= 0

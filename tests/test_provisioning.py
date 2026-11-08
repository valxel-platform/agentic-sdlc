"""New repositories: proposed in the HLD, approved at G4, created on the golden path, and then
part of the feature's change set like any other repo."""

import json
import re
import shutil
from io import BytesIO
from pathlib import Path
from urllib.error import HTTPError

import pytest
import yaml
from langgraph.checkpoint.memory import InMemorySaver
from mcp.server import MCPServer

from vrixel_sdlc.config import load_settings
from vrixel_sdlc.engine import FeatureEngine
from vrixel_sdlc.evaluators import run_checks
from vrixel_sdlc.governance import GovernanceError
from vrixel_sdlc.integrations.github import GitHubRest
from vrixel_sdlc.integrations.mcp_client import McpHttp
from vrixel_sdlc.integrations.openchoreo import OpenChoreoMcp
from vrixel_sdlc.services import build_services

ROOT = Path(__file__).resolve().parents[1]
NEW = "acme-clinic/cln-consent-registry-service"


@pytest.fixture
def gov_root(tmp_path):
    root = tmp_path / "governance"
    shutil.copytree(ROOT / "examples" / "governance", root)
    return root


def make(tmp_path, gov_root):
    s = load_settings(mode="stub", database_url="", artifact_dir=tmp_path / "a",
                      governance_source="local", governance_root=gov_root)
    svc = build_services(s)
    return FeatureEngine(svc, InMemorySaver()), svc


def approve_gates(eng, fid, seen=None):
    while True:
        p = eng.pending(fid)
        gates = [x for x in p if x["type"] == "gate"]
        if not gates or any(x["type"] != "gate" for x in p):
            return eng.view(fid)
        if seen is not None:
            seen[gates[0]["gate"]] = gates[0]
        eng.decide(fid, gates[0]["interrupt_id"], {"decision": "approve", "approver": "lead"})


def start_new_service(eng, product="CLN", title="Consent registry"):
    return eng.start(product_id=product, title=title,
                     description="A new service that stores patient consent records")["feature_id"]


def test_new_repo_is_created_on_the_golden_path_and_joins_the_change_set(tmp_path, gov_root):
    eng, svc = make(tmp_path, gov_root)
    tpl = "vrixel-tools/template-service"
    svc.github.files[(tpl, "main", ".vrixel/identity")] = "# files that carry the template's name\nworkload.yaml\nsrc/main/resources/application.yaml\nmissing.txt\n"
    svc.github.files[(tpl, "main", "workload.yaml")] = "metadata:\n  name: template-service\n"
    svc.github.files[(tpl, "main", "src/main/resources/application.yaml")] = "spring:\n  application:\n    name: template-service\n"
    seen = {}
    v = approve_gates(eng, start_new_service(eng), seen)
    assert v["status"] == "done"

    (plan,) = seen["g4"]["new_repos"]  # G4 sees exactly what it approves
    assert plan["repo"] == NEW and plan["ruleset"]["required_approvals"] == 0  # gates, not GitHub reviews
    assert plan["ruleset"]["required_checks"] == ["unit", "contract", "CodeQL"]

    repo = svc.github.repos[NEW]
    assert repo["created"]["template"] == "vrixel-tools/template-service" and not repo["created"]["private"]
    assert repo["security"]["secret_scanning"] and repo["security"]["push_protection"]
    assert repo["security"]["code_scanning_default_setup"]
    assert repo["teams"] == {"tech-leads": "maintain", "architects": "push", "security": "push",
                             "product-owners": "pull"}
    assert repo["properties"]["vrixel_data_class"] == "phi" and "cln" in repo["topics"]
    assert repo["ruleset"]["branch"] == "main" and not repo["ruleset"]["code_owner_review"]
    codeowners = svc.github.files[(NEW, "main", ".github/CODEOWNERS")]
    assert "* @acme-clinic/tech-leads" in codeowners and "/src/phi/ @acme-clinic/security" in codeowners
    assert svc.openchoreo.components[NEW]["component_type"] == "deployment/service"
    # Its Workload, app name and error reports carry its component's name, so its signals route to it.
    short = NEW.split("/", 1)[1]
    assert svc.github.files[(NEW, "main", "workload.yaml")] == f"metadata:\n  name: {short}\n"
    assert f"name: {short}" in svc.github.files[(NEW, "main", "src/main/resources/application.yaml")]

    manifest = svc.github.files[("acme-clinic/cln-governance", "vrixel/CLN-FEAT-1-repos", "product.yaml")]
    assert {"name": NEW, "role": "provider"} in yaml.safe_load(manifest)["repos"]
    (rec,) = v["new_repos"]
    assert rec["status"] == "provisioned" and rec["governance_pr"].startswith("https://github.com/acme-clinic/cln-governance/pull/")

    # The rest of the pipeline treats it like any repo: tasks, PRs, builds, deployments.
    tasks = eng.graph.get_state({"configurable": {"thread_id": v["feature_id"]}}).values["tasks"]
    assert NEW in {t["repo"] for t in tasks}
    assert any(pr["repo"] == NEW for pr in v["pull_requests"].values())
    assert NEW in v["runs"] and v["deployments"][NEW]["env"] == "production"


def test_general_products_get_the_platform_default_approvals(tmp_path, gov_root):
    eng, svc = make(tmp_path, gov_root)
    seen = {}
    approve_gates(eng, start_new_service(eng, "FARM", "Yield forecasting"), seen)
    (plan,) = seen["g4"]["new_repos"]
    assert plan["repo"] == "acme-farm/farm-yield-forecasting-service"
    assert plan["ruleset"]["required_approvals"] == 0


def test_a_failed_step_goes_to_architects_and_retry_resumes(tmp_path, gov_root):
    eng, svc = make(tmp_path, gov_root)
    svc.github.fail_steps.add("upsert_ruleset")
    v = approve_gates(eng, start_new_service(eng))
    (esc,) = eng.pending(v["feature_id"])
    assert esc["phase"] == "provisioning" and "ruleset failed" in esc["reason"]
    assert next(s for s in v["rail"] if s["key"] == "plan")["state"] == "waiting"
    svc.github.fail_steps.clear()
    eng.decide(v["feature_id"], esc["interrupt_id"], {"decision": "retry", "approver": "arch"})
    v = approve_gates(eng, v["feature_id"])
    assert v["status"] == "done" and v["new_repos"][0]["status"] == "provisioned"
    assert v["new_repos"][0]["steps"][0] == {"step": "create", "ok": True, "detail": "exists"}  # not created twice


def test_proposals_are_checked_against_the_standards(tmp_path, gov_root):
    eng, svc = make(tmp_path, gov_root)
    product = svc.snapshot("CLN").product
    from vrixel_sdlc.provisioning import naming_policy

    hld = "\n".join([
        "# HLD: x", "", "## ADRs", "- ADR-1: Extend the API", "- ADR-2: Create acme-clinic/cln-good-service because",
        "", "## Affected repos", "- acme-clinic/cln-intake-api (provider)", "- acme-clinic/cln-good-service (provider)",
        "- acme-shop/cln-elsewhere (provider)", "- acme-clinic/Bad_Name (provider)", "- acme-clinic/cln-no-adr (provider)",
        "", "## New repos",
        "- acme-clinic/cln-good-service (provider): Owns consent [ADR-2]",
        "- acme-shop/cln-elsewhere (provider): Wrong org [ADR-2]",
        "- acme-clinic/Bad_Name (provider): Bad name [ADR-2]",
        "- acme-clinic/cln-no-adr (provider): No reason given",
        "- acme-clinic/cln-intake-api (provider): Already there [ADR-2]",
        "", "## Contracts", "- x", "", "## Diagram", "```mermaid",
        "flowchart LR\n  a[\"cln-intake-api\"]\n  b[\"cln-good-service\"]\n  c[\"cln-elsewhere\"]\n  d[\"Bad_Name\"]\n  e[\"cln-no-adr\"]",
        "```"])
    ctx = {"repos": [{"name": r.name, "role": r.role} for r in product.repos], "dependency_repos": [],
           "repo_policy": naming_policy(svc.settings.repos, product)}
    findings = " | ".join(run_checks("architecture", hld, ctx))
    assert "cln-good-service" not in findings
    assert "acme-shop/cln-elsewhere must be created in acme-clinic" in findings
    assert "'Bad_Name' must match" in findings
    assert "cln-no-adr needs an ADR" in findings
    assert "acme-clinic/cln-intake-api already exists" in findings


@pytest.mark.parametrize("block,message", [
    ("repo_standards:\n  required_approvals: -1\n", "required_approvals -1 is below the platform's 0"),
    ("repo_standards:\n  security:\n    secret_scanning: false\n", "secret_scanning cannot be turned off"),
])
def test_products_cannot_loosen_repo_standards(tmp_path, gov_root, block, message):
    path = gov_root / "acme-clinic" / "cln-governance" / "product.yaml"
    path.write_text(path.read_text() + block)
    eng, svc = make(tmp_path, gov_root)
    with pytest.raises(GovernanceError, match=message):
        svc.snapshot("CLN")


def test_products_choose_their_own_templates_and_extra_checks(tmp_path, gov_root):
    path = gov_root / "acme-clinic" / "cln-governance" / "product.yaml"
    path.write_text(path.read_text() + "repo_standards:\n  templates:\n    provider: acme-clinic/template-fhir-service\n"
                                       "  extra_required_checks: [fhir-conformance]\n  required_approvals: 3\n")
    eng, svc = make(tmp_path, gov_root)
    seen = {}
    approve_gates(eng, start_new_service(eng), seen)
    (plan,) = seen["g4"]["new_repos"]
    assert plan["template"] == "acme-clinic/template-fhir-service"
    assert plan["ruleset"]["required_checks"][-1] == "fhir-conformance" and plan["ruleset"]["required_approvals"] == 3


# --------------------------------------------------------------------------- live adapters


class Resp(BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeAdminApi:
    def __init__(self):
        self.calls, self.repos, self.rulesets = [], set(), []

    def open(self, req, timeout=None):
        m, path = req.get_method(), req.full_url.split("https://api.github.com", 1)[1]
        body = json.loads(req.data.decode()) if req.data else None
        self.calls.append((m, path, body))
        if m == "GET" and re.fullmatch(r"/repos/[^/]+/[^/]+", path):
            if path[7:] not in self.repos:
                raise HTTPError(req.full_url, 404, "nf", {}, BytesIO(b"{}"))
            return Resp(b'{"default_branch": "main"}')
        if m == "POST" and path.endswith("/generate"):
            self.repos.add(f"{body['owner']}/{body['name']}")
            return Resp(b"{}")
        if m == "GET" and path.endswith("/rulesets"):
            return Resp(json.dumps(self.rulesets).encode())
        if m == "POST" and path.endswith("/rulesets"):
            self.rulesets.append({"id": 9, "name": body["name"]})
        return Resp(b"{}")


class Tokens:
    def token(self, org):
        return "t"


def test_github_admin_calls():
    api = FakeAdminApi()
    gh = GitHubRest(Tokens(), opener=api)
    assert gh.create_repo(repo="o/cln-x-service", template="vrixel-tools/template-service", private=True,
                          description="d") == "created"
    assert gh.create_repo(repo="o/cln-x-service", template="vrixel-tools/template-service", private=True,
                          description="d") == "exists"
    gh.update_repo(repo="o/cln-x-service", merge={"squash": True}, default_branch="main", description="d")
    gh.enable_security(repo="o/cln-x-service", secret_scanning=True, push_protection=True)
    gh.grant_team(org="o", team="tech-leads", repo="o/cln-x-service", permission="maintain")
    gh.set_topics(repo="o/cln-x-service", topics=["vrixel"])
    gh.set_properties(repo="o/cln-x-service", properties={"vrixel_managed": "true"})
    spec = {"name": "vrixel-default-branch", "required_approvals": 2, "required_checks": ["unit", "CodeQL"],
            "code_scanning_merge_protection": "high", "linear_history": True, "bypass_app_id": "12345"}
    gh.upsert_ruleset(repo="o/cln-x-service", branch="main", spec=spec)
    gh.upsert_ruleset(repo="o/cln-x-service", branch="main", spec=spec)

    calls = [(m, p) for m, p, _ in api.calls]
    assert ("POST", "/repos/vrixel-tools/template-service/generate") in calls
    assert ("PUT", "/repos/o/cln-x-service/vulnerability-alerts") in calls
    assert ("PATCH", "/repos/o/cln-x-service/code-scanning/default-setup") in calls
    assert ("PUT", "/orgs/o/teams/tech-leads/repos/o/cln-x-service") in calls
    assert ("PATCH", "/repos/o/cln-x-service/properties/values") in calls
    assert calls.count(("POST", "/repos/o/cln-x-service/rulesets")) == 1  # second run updates
    assert ("PUT", "/repos/o/cln-x-service/rulesets/9") in calls
    body = next(b for m, p, b in api.calls if m == "POST" and p.endswith("/rulesets"))
    rules = {r["type"]: r.get("parameters") for r in body["rules"]}
    assert rules["pull_request"]["required_approving_review_count"] == 2
    assert [c["context"] for c in rules["required_status_checks"]["required_status_checks"]] == ["unit", "CodeQL"]
    assert rules["code_scanning"]["code_scanning_tools"][0]["security_alerts_threshold"] == "high_or_higher"
    assert body["bypass_actors"] == [{"actor_id": 12345, "actor_type": "Integration", "bypass_mode": "pull_request"}]
    sec = next(b for m, p, b in api.calls if m == "PATCH" and p == "/repos/o/cln-x-service" and "security_and_analysis" in b)
    assert sec["security_and_analysis"]["secret_scanning_push_protection"] == {"status": "enabled"}


def test_openchoreo_component_registration():
    server, calls = MCPServer("openchoreo"), []

    @server.tool()
    def get_component(namespace_name: str, project_name: str, component_name: str) -> dict:
        if component_name not in [c[2] for c in calls]:
            raise ValueError("component not found")
        return {"name": component_name}

    @server.tool()
    def create_component(namespace_name: str, project_name: str, component_name: str, component_type: str,
                         workflow: str = "", repository_url: str = "", branch: str = "main",
                         auto_deploy: bool = False) -> dict:
        calls.append((namespace_name, project_name, component_name, component_type, repository_url, auto_deploy))
        return {"name": component_name}

    oc = OpenChoreoMcp(McpHttp("openchoreo", server), default_namespace="vrixel")
    args = dict(component=NEW, project="cln", namespace=None, component_type="deployment/service",
                workflow="docker", repo_url=f"https://github.com/{NEW}", branch="main")
    assert oc.register_component(**args) == "created"
    assert oc.register_component(**args) == "exists"
    assert calls == [("vrixel", "cln", "cln-consent-registry-service", "deployment/service",
                      f"https://github.com/{NEW}", False)]

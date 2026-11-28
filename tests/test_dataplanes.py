"""A new product's cloud accounts, asked at inception (cloud/onboarding.py), and its data planes on
Google Cloud and Azure, built by the tool at the product gate (dataplanes.py)."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
import yaml

from valxel_sdlc import dataplanes, environments
from valxel_sdlc.cloud import onboarding
from valxel_sdlc.intake import Draft, IntakeAgent, IntakeSession
from valxel_sdlc.resources import check_cloud

GCP = {"provider": "gcp", "region": "us-central1",
       "accounts": {"dev": "acme-cln-nonprod", "staging": "acme-cln-nonprod", "production": "acme-cln-prod"}}
SUB_NP, SUB_P = "1b2c3d4e-0000-4000-8000-000000000001", "1b2c3d4e-0000-4000-8000-000000000002"
AZURE = {"provider": "azure", "region": "eastus2", "accounts": {"dev": SUB_NP, "staging": SUB_NP, "production": SUB_P}}
SETTINGS = {"image": "ghcr.io/valxel-platform/plane-builder:main@sha256:" + "a" * 64, "namespace": "valxel",
            "service_account": "valxel", "domain": "planes.example.com", "control_plane_domain": "cp.example.com",
            "acme_email": "ops@example.com", "admin_cidrs": {"valxel-0": "203.0.113.10/32"}, "gitops_branch": "main",
            "db_logins_image": "ghcr.io/valxel-platform/db-logins:main@sha256:" + "b" * 64}


# ------------------------------------------------------------------------------ onboarding

@pytest.mark.parametrize("cloud,text,want", [
    ("gcp", "non-production: acme-cln-dev, production: acme-cln-prod, region: us-central1",
     ({"non_production": "acme-cln-dev", "production": "acme-cln-prod"}, "us-central1")),
    ("gcp", "nonprod=acme-cln-dev prod=acme-cln-prod region=europe-west4",
     ({"non_production": "acme-cln-dev", "production": "acme-cln-prod"}, "europe-west4")),
    ("azure", f"{SUB_NP} and {SUB_P}, region: eastus2", ({"non_production": SUB_NP, "production": SUB_P}, "eastus2")),
    ("aws", "production: 222222222222, non-production: 111111111111, region: us-east-1",
     ({"non_production": "111111111111", "production": "222222222222"}, "us-east-1")),
])
def test_the_two_accounts_and_the_region_are_read_from_what_was_said(cloud, text, want):
    got, why = onboarding.parse(cloud, text)
    assert not why and (got["accounts"], got["region"]) == want


@pytest.mark.parametrize("text,says", [
    ("non-production: acme-cln, production: acme-cln, region: us-central1", "Production needs a project of its own"),
    ("non-production: acme-cln-dev, production: acme-cln-prod", "Which region"),
    ("production: acme-cln-prod, region: us-central1", "non-production project ID"),
])
def test_what_is_missing_or_wrong_is_said(text, says):
    got, why = onboarding.parse("gcp", text)
    assert not got and says in why


def test_the_guide_links_to_the_clouds_own_pages_and_never_signs_anyone_up():
    for cloud in ("aws", "gcp", "azure"):
        g = onboarding.guide(cloud, "CLN")
        assert all(u.startswith("https://") for u in g["links"].values())
        text = onboarding.guide_text(cloud, "CLN")
        assert "can't open" in text and g["links"]["sign_up"] in text and "connections page" in text
    assert "https://console.cloud.google.com/projectcreate" in onboarding.guide_text("gcp")


def test_runtime_cloud_puts_dev_and_staging_in_the_non_production_account():
    rc = onboarding.runtime_cloud("gcp", {"non_production": "a-nonprod", "production": "a-prod"}, "us-central1")
    assert rc == {"provider": "gcp", "region": "us-central1", "placement": "dedicated",
                  "accounts": {"dev": "a-nonprod", "staging": "a-nonprod", "production": "a-prod"}}
    assert onboarding.plane_accounts(rc) == {"non_production": "a-nonprod", "production": "a-prod"}


def test_dev_and_staging_may_share_an_account_but_production_may_not():
    catalog = {"cloud": "gcp", "regions": {"general": ["us-central1"]}}
    pipeline = ["dev", "staging", "production"]
    assert check_cloud(GCP, "general", pipeline, catalog) == []
    same = {**GCP, "accounts": {"dev": "acme-cln-prod", "staging": "acme-cln-nonprod", "production": "acme-cln-prod"}}
    problems = check_cloud(same, "general", pipeline, catalog)
    assert any("production needs its own Google Cloud project" in p for p in problems)


# ------------------------------------------------------------------------------ inception asks

class _Settings:
    mode = "stub"


def _agent():
    return IntakeAgent(SimpleNamespace(settings=_Settings(), registry=None))


def _product_session(monkeypatch, cloud="gcp"):
    monkeypatch.setenv("VALXEL_CLOUD", cloud)
    s = IntakeSession(id="s1", owner="sunil", lanes=[], starter=True)
    s.draft = Draft(size="product", problem="Clinic notes", name="CLN", org="acme")
    return s


def test_the_question_names_the_installations_cloud_and_offers_help(monkeypatch):
    agent, s = _agent(), _product_session(monkeypatch)
    q = agent._next_question(s)
    assert s.awaiting == "cloud" and q.startswith("CLN will run on Google Cloud") and "two projects" in q
    assert s.options == ["I have them", "Help me create them", "Set them up later"]
    help_ = agent._answer_cloud(s, "Help me create them")
    assert "https://console.cloud.google.com/projectcreate" in help_ and s.awaiting == "cloud"
    asked = agent._answer_cloud(s, "I have them")
    assert asked.startswith("Give the two project IDs") and s.draft.cloud == ""
    assert agent._answer_cloud(s, "non-production: acme-cln-dev, production: acme-cln-prod, region: us-central1") == ""
    assert (s.draft.cloud, s.draft.region) == ("given", "us-central1")


def test_later_is_fine_and_leave_the_rest_means_later(monkeypatch):
    agent, s = _agent(), _product_session(monkeypatch, "azure")
    assert agent._answer_cloud(s, "Set them up later") == "" and s.draft.cloud == "later"
    s2 = _product_session(monkeypatch, "azure")
    agent._delegate_rest(s2)
    assert s2.draft.cloud == "later"


def test_product_yaml_carries_runtime_cloud_when_the_accounts_were_given():
    from valxel_sdlc.inception import Vision
    from valxel_sdlc.inception.setup import _runtime_cloud

    v = Vision(name="CLN", org="acme", vision="x",
               cloud={"provider": "gcp", "region": "us-central1",
                      "accounts": {"non_production": "acme-cln-nonprod", "production": "acme-cln-prod"}})
    assert _runtime_cloud(v) == {"cloud": {**GCP, "placement": "dedicated"}}
    assert _runtime_cloud(Vision(name="CLN", org="acme", vision="x", cloud={"provider": "gcp", "later": True})) == {}


# ------------------------------------------------------------------------------ the planes

def test_plane_vars_follow_the_stacks_variables():
    np_ = dataplanes.plane_vars("CLN", "non_production", GCP, SETTINGS)
    assert np_["project"] == "acme-cln-nonprod" and np_["name"] == "cln-nonprod" and np_["zone_count"] == 1
    assert np_["nodes"]["spot"] and np_["domain"] == "cln-nonprod.planes.example.com"
    assert np_["cluster_vars"] == {"db_logins_image": SETTINGS["db_logins_image"]}
    p = dataplanes.plane_vars("CLN", "production", AZURE, SETTINGS, zones=3)
    assert p["location"] == "eastus2" and p["zones"] == ["1", "2", "3"] and not p["nodes"]["spot"]
    with pytest.raises(ValueError):
        dataplanes.plane_vars("CLN", "production", {"provider": "aws", "accounts": {}}, SETTINGS)


def test_state_stays_in_the_customers_account_where_the_connect_template_put_it():
    assert dataplanes.state_location("gcp", "acme-cln-prod") == {"STATE_BUCKET": "acme-cln-prod-valxel-state"}
    assert dataplanes.state_location("azure", SUB_P) == {"STATE_ACCOUNT": "vxst1b2c3d4e000040008000"}


class FakeRunner:
    def __init__(self, states=None):
        self.states, self.started = dict(states or {}), {}

    def status(self, name):
        return self.states.get(name, {"state": "absent"})

    def start(self, name, *, args, env, files):
        self.started[name] = {"args": args, "env": env, "vars": json.loads(files["terraform.tfvars.json"])}
        self.states[name] = {"state": "running"}


class FakeStore:
    def __init__(self, rows):
        self.rows = rows

    def list(self, kind, workspace=None):
        return list(self.rows)


class FakeGitHub:
    def __init__(self):
        self.files, self.prs = {}, []

    def put_file(self, *, repo, branch, path, content, message):
        self.files[path] = content

    def open_pr(self, **kw):
        self.prs.append(kw)
        return {"url": f"https://github.com/acme/infra/pull/{len(self.prs)}"}


def _services(rows, ready=()):
    oc = SimpleNamespace(data_plane=lambda name, namespace=None: {"state": "ready" if name in ready else "absent"})
    return SimpleNamespace(connections=SimpleNamespace(store=FakeStore(rows)), github=FakeGitHub(), openchoreo=oc)


def _conn(tool, account, mode="full", **reach):
    return {"tool": tool, "account": account, "status": "connected", "reach": {"mode": mode, **reach}}


def test_nothing_starts_until_the_accounts_are_known_and_connected_in_full_mode():
    runner = FakeRunner()
    out = dataplanes.build(_services([]), product_id="CLN", cloud={"provider": "gcp"}, approver="maria",
                           runner=runner, s=SETTINGS, infra_repo="acme/infra")
    assert "set runtime.cloud" in out["waiting"][0] and not runner.started
    out = dataplanes.build(_services([_conn("gcp", "acme-cln-nonprod", mode="lite")]), product_id="CLN", cloud=GCP,
                           approver="maria", runner=runner, s=SETTINGS, infra_repo="acme/infra")
    assert out["planes"]["cln-nonprod"]["why"].endswith("connect it again in full mode")
    assert out["planes"]["cln-prod"]["why"].startswith("connect project acme-cln-prod in full mode")
    assert not runner.started
    unset = dataplanes.build(_services([]), product_id="CLN", cloud=GCP, approver="maria", runner=runner,
                             s={**SETTINGS, "image": ""}, infra_repo="acme/infra")
    assert "VALXEL_PLANE_BUILDER_IMAGE" in unset["waiting"][0]


def test_a_connected_project_gets_a_job_acting_as_its_deploy_account():
    runner = FakeRunner()
    svc = _services([_conn("gcp", "acme-cln-nonprod"), _conn("gcp", "acme-cln-prod")])
    out = dataplanes.build(svc, product_id="CLN", cloud=GCP, approver="maria", runner=runner, s=SETTINGS,
                           infra_repo="acme/infra")
    assert {p["state"] for p in out["planes"].values()} == {"building"}
    job = runner.started["plane-apply-cln-prod"]
    assert job["args"] == ["gcp", "apply"]
    assert job["env"]["GOOGLE_IMPERSONATE_SERVICE_ACCOUNT"] == "valxel-deploy@acme-cln-prod.iam.gserviceaccount.com"
    assert job["env"]["STATE_BUCKET"] == "acme-cln-prod-valxel-state" and job["env"]["PLANE"] == "cln-prod"
    assert job["vars"]["project"] == "acme-cln-prod" and job["vars"]["env"] == "prod"
    again = dataplanes.build(svc, product_id="CLN", cloud=GCP, approver="maria", runner=runner, s=SETTINGS,
                             infra_repo="acme/infra")
    assert len(runner.started) == 2 and all(p["state"] == "building" for p in again["planes"].values())


def test_an_azure_job_signs_in_through_the_tools_federated_token():
    runner = FakeRunner()
    reach = {"tenant": "t-1", "deploy_client_id": "c-1"}
    svc = _services([_conn("azure", SUB_NP, **reach), _conn("azure", SUB_P, **reach)])
    dataplanes.build(svc, product_id="CLN", cloud=AZURE, approver="maria", runner=runner, s=SETTINGS,
                     infra_repo="acme/infra")
    env = runner.started["plane-apply-cln-nonprod"]["env"]
    assert env["ARM_USE_OIDC"] == "true" and env["ARM_CLIENT_ID"] == "c-1" and env["ARM_TENANT_ID"] == "t-1"
    assert env["ARM_SUBSCRIPTION_ID"] == SUB_NP and env["STATE_ACCOUNT"].startswith("vxst")


def test_a_built_plane_is_registered_by_a_pull_request_and_a_failed_one_says_where_to_look(monkeypatch):
    out_np = {"name": "cln-nonprod", "domain": "cln-nonprod.planes.example.com", "endpoint": "1.2.3.4",
              "name_servers": ["ns1.example."]}
    runner = FakeRunner({"plane-apply-cln-nonprod": {"state": "succeeded", "output": out_np},
                         "plane-apply-cln-prod": {"state": "failed", "log": "Error: quota"}})
    svc = _services([_conn("gcp", "acme-cln-nonprod"), _conn("gcp", "acme-cln-prod")])
    monkeypatch.setattr(dataplanes, "read_agent_ca", lambda provider, out, tok: "-----BEGIN CERTIFICATE-----\nAAA\n"
                                                                                 "-----END CERTIFICATE-----")
    out = dataplanes.build(svc, product_id="CLN", cloud=GCP, approver="maria", runner=runner, s=SETTINGS,
                           infra_repo="acme/infra", token=lambda *a: "tok")
    np_, p = out["planes"]["cln-nonprod"], out["planes"]["cln-prod"]
    assert np_["state"] == "registering" and np_["pr"].endswith("/pull/1")
    assert np_["delegate"] == {"domain": "cln-nonprod.planes.example.com", "name_servers": ["ns1.example."]}
    doc = yaml.safe_load(svc.github.files["gitops/clusters/tools/planes/cln-nonprod.yaml"])
    assert doc["kind"] == "ClusterDataPlane" and doc["spec"]["planeID"] == "cln-nonprod"
    assert "BEGIN CERTIFICATE" in doc["spec"]["clusterAgent"]["clientCA"]["value"]
    assert doc["spec"]["gateway"]["ingress"]["external"]["https"]["host"] == "apps.cln-nonprod.planes.example.com"
    assert p["state"] == "failed" and "Error: quota" in p["log"]
    done = dataplanes.build(_services([_conn("gcp", "acme-cln-nonprod"), _conn("gcp", "acme-cln-prod")],
                                      ready=("cln-nonprod",)), product_id="CLN", cloud=GCP, approver="maria",
                            runner=runner, s=SETTINGS, infra_repo="acme/infra", token=lambda *a: "tok")
    assert done["planes"]["cln-nonprod"]["state"] == "registered"


def test_a_plane_still_installing_waits_for_its_agent():
    runner = FakeRunner({"plane-apply-cln-nonprod": {"state": "succeeded", "output": {"name": "cln-nonprod"}}})
    svc = _services([_conn("gcp", "acme-cln-nonprod")])

    def no_ca(*a):
        raise ConnectionError("refused")

    out = dataplanes.build(svc, product_id="CLN", cloud=GCP, approver="maria", runner=runner, s=SETTINGS,
                           infra_repo="acme/infra", token=no_ca)
    plane = out["planes"]["cln-nonprod"]
    assert plane["state"] == "built" and "still be installing" in plane["why"]


def test_the_job_log_gives_the_output_or_its_tail():
    assert dataplanes.parse_log('Apply complete!\n{"name": "cln-prod"}\n') == {"output": {"name": "cln-prod"}}
    assert dataplanes.parse_log("Error: nope\n")["log"] == "Error: nope"


def test_the_product_gate_builds_planes_on_gcp_and_proposes_cluster_files_on_aws(monkeypatch):
    calls = []
    monkeypatch.setattr(dataplanes, "build",
                        lambda services, **kw: calls.append(kw) or {"waiting": ["x"], "planes": {}})
    oc = SimpleNamespace(data_plane=lambda name, namespace=None: {"state": "absent"},
                         ensure_environment=lambda **kw: "created")
    svc = SimpleNamespace(github=FakeGitHub(), openchoreo=oc)
    monkeypatch.setenv("VALXEL_CLOUD", "gcp")
    steps = environments.setup(svc, product_id="CLN", tier=2, approver="maria", infra_repo="acme/infra", cloud=GCP)
    assert [s["step"] for s in steps] == ["data_planes", "environments"] and calls[0]["cloud"] == GCP
    monkeypatch.setenv("VALXEL_CLOUD", "aws")
    steps = environments.setup(svc, product_id="CLN", tier=2, approver="maria", infra_repo="acme/infra")
    assert [s["step"] for s in steps] == ["clusters_pr", "environments"] and len(calls) == 1

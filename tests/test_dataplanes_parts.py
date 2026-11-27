"""The parts of building data planes that reach outside the tool: the Jobs API, the new cluster, the
clouds' tokens; and the places a person meets them (the CLI, the API, the draft editor)."""

from __future__ import annotations

import base64
import json
import sys
from types import SimpleNamespace

import pytest

from valxel_sdlc import dataplanes, environments
from valxel_sdlc.cloud import onboarding


class Resp:
    def __init__(self, status=200, body=None, text=None):
        self.status_code, self._body = status, body
        self.text = text if text is not None else (json.dumps(body) if body is not None else "")
        self.content = self.text.encode()

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)


class FakeHttp:
    """Answers by (method, path prefix); records every call."""

    def __init__(self, answers):
        self.answers, self.calls = answers, []

    def _find(self, method, url):
        path = url.split("://", 1)[1].split("/", 1)[1]
        self.calls.append((method, "/" + path))
        for (m, prefix), resp in self.answers.items():
            if m == method and ("/" + path).startswith(prefix):
                return resp
        return Resp(404)

    def request(self, method, url, json=None, headers=None):
        resp = self._find(method, url)
        if json is not None:
            self.calls[-1] = (method, self.calls[-1][1], json)
        return resp

    def get(self, url, headers=None):
        return self._find("GET", url)


@pytest.fixture
def jobs(tmp_path, monkeypatch):
    (tmp_path / "token").write_text("sa-token\n")
    (tmp_path / "ca.crt").write_text("")
    monkeypatch.setattr(dataplanes.KubernetesJobs, "SA", tmp_path)
    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "10.0.0.1")

    def make(answers):
        http = FakeHttp(answers)
        return dataplanes.KubernetesJobs({"namespace": "valxel", "image": "img@sha256:" + "a" * 64,
                                          "service_account": "valxel"}, http=http), http
    return make


JOB = "/apis/batch/v1/namespaces/valxel/jobs/plane-apply-cln-prod"
PODS = "/api/v1/namespaces/valxel/pods?labelSelector=job-name%3Dplane-apply-cln-prod"
POD = {"items": [{"metadata": {"name": "p-1", "creationTimestamp": "2026-11-27T10:00:00Z"}}]}


def test_a_job_is_absent_running_succeeded_or_failed(jobs):
    k, _ = jobs({})
    assert k.status("plane-apply-cln-prod") == {"state": "absent"}
    k, _ = jobs({("GET", JOB): Resp(body={"status": {"startTime": "t0"}})})
    assert k.status("plane-apply-cln-prod") == {"state": "running", "started": "t0"}
    k, _ = jobs({("GET", JOB): Resp(body={"status": {"succeeded": 1}}), ("GET", PODS): Resp(body=POD),
                 ("GET", "/api/v1/namespaces/valxel/pods/p-1/log"): Resp(text='Apply complete!\n{"name": "cln-prod"}')})
    assert k.status("plane-apply-cln-prod")["output"] == {"name": "cln-prod"}
    failed = {"status": {"conditions": [{"type": "Failed", "status": "True"}]}}
    k, _ = jobs({("GET", JOB): Resp(body=failed), ("GET", PODS): Resp(body={"items": []})})
    assert k.status("plane-apply-cln-prod") == {"state": "failed", "started": "", "log": ""}


def test_starting_a_job_replaces_its_configmap_and_runs_as_the_tool(jobs):
    k, http = jobs({("POST", "/api/v1/namespaces/valxel/configmaps"): Resp(body={}),
                    ("POST", "/apis/batch/v1/namespaces/valxel/jobs"): Resp(body={})})
    k.start("plane-apply-cln-prod", args=["gcp", "apply"], env={"PLANE": "cln-prod"},
            files={"terraform.tfvars.json": "{}"})
    assert [c[:2] for c in http.calls] == [("DELETE", "/api/v1/namespaces/valxel/configmaps/plane-apply-cln-prod"),
                                           ("POST", "/api/v1/namespaces/valxel/configmaps"),
                                           ("POST", "/apis/batch/v1/namespaces/valxel/jobs")]
    job = http.calls[-1][2]
    pod = job["spec"]["template"]["spec"]
    assert pod["serviceAccountName"] == "valxel" and pod["containers"][0]["args"] == ["gcp", "apply"]
    assert pod["containers"][0]["securityContext"]["runAsNonRoot"] is True
    assert pod["containers"][0]["env"] == [{"name": "PLANE", "value": "cln-prod"}]


def test_the_cluster_refusing_says_so(jobs):
    k, _ = jobs({("GET", JOB): Resp(403)})
    with pytest.raises(RuntimeError, match="refused GET"):
        k.status("plane-apply-cln-prod")


def test_the_agent_ca_is_read_from_the_new_cluster():
    pem = "-----BEGIN CERTIFICATE-----\nAAA\n-----END CERTIFICATE-----\n"
    secret = "/api/v1/namespaces/openchoreo-data-plane/secrets/cluster-agent-tls"
    http = FakeHttp({("GET", secret): Resp(body={"data": {"ca.crt": base64.b64encode(pem.encode()).decode()}})})
    assert dataplanes.read_agent_ca("gcp", {"endpoint": "1.2.3.4"}, "tok", http=http) == pem
    assert http.calls == [("GET", secret)]
    assert dataplanes.read_agent_ca("azure", {"endpoint": "https://x.hcp.azmk8s.io:443"}, "tok", http=FakeHttp({})) == ""
    empty = FakeHttp({("GET", secret): Resp(body={"data": {}})})
    assert dataplanes.read_agent_ca("gcp", {"endpoint": "1.2.3.4"}, "tok", http=empty) == ""


def test_tokens_for_the_cluster_come_from_each_clouds_connection():
    calls = []
    gcp = SimpleNamespace(token=lambda account, job: calls.append(("gcp", account, job)) or "g")
    azure = SimpleNamespace(token=lambda account, job, reach, scope: calls.append(("azure", account, scope)) or "a")
    svc = SimpleNamespace(connections=SimpleNamespace(gcp=gcp, azure=azure))
    assert dataplanes.cluster_token(svc, "gcp", "p", {}) == "g"
    assert dataplanes.cluster_token(svc, "azure", "s", {"reach": {}}) == "a"
    assert calls == [("gcp", "p", "deploy"), ("azure", "s", f"{dataplanes.AKS_SERVER_APP}/.default")]


def test_an_azure_token_for_another_scope_is_traded_for_the_tools_own(monkeypatch, tmp_path):
    from valxel_sdlc.cloud.azure import accounts

    seen = {}

    class Cred:
        def __init__(self, tenant, client_id, assertion):
            seen.update(tenant=tenant, client=client_id, assertion=assertion())

        def get_token(self, scope):
            seen["scope"] = scope
            return SimpleNamespace(token="aks-token")

    monkeypatch.setitem(sys.modules, "azure.identity", SimpleNamespace(ClientAssertionCredential=Cred))
    f = tmp_path / "t"
    f.write_text("projected\n")
    assert accounts.federated_token("t-1", "c-1", str(f), "x/.default") == "aks-token"
    assert seen == {"tenant": "t-1", "client": "c-1", "assertion": "projected", "scope": "x/.default"}
    conn = accounts.AzureConnect(issuer="i", subject="s", exchange=lambda *a: "arm", tenant=lambda s: "t-1")
    conn.token_file = str(f)
    reach = {"tenant": "t-1", "deploy_client_id": "c-1"}
    assert conn.token("sub", "deploy", reach, scope="x/.default") == "aks-token"


def test_settings_say_what_is_missing():
    s = dataplanes.settings({"VALXEL_PLANE_ADMIN_CIDRS": "203.0.113.1/32, 198.51.100.0/24",
                             "POD_NAMESPACE": "tools"})
    assert s["admin_cidrs"] == {"valxel-0": "203.0.113.1/32", "valxel-1": "198.51.100.0/24"}
    assert s["namespace"] == "tools" and s["service_account"] == "valxel"
    assert "VALXEL_PLANE_BUILDER_IMAGE" in dataplanes.missing_settings(s)
    assert "VALXEL_PLANE_ADMIN_CIDRS" not in dataplanes.missing_settings(s)


def test_aws_products_are_left_to_cluster_files_and_unconnected_tools_wait():
    out = dataplanes.build(SimpleNamespace(), product_id="CLN", cloud={"provider": "aws"}, approver="m")
    assert out["skipped"] and "cluster files" in out["why"]
    assert dataplanes._connection(None, "gcp", "p") is None


def test_without_an_infrastructure_repo_registration_is_by_hand():
    out = dataplanes._register(SimpleNamespace(), "gcp", "p", "cln-prod", {}, {}, infra_repo="", thread_id="t",
                               approver="m")
    assert out["state"] == "built" and "register-plane.sh" in out["why"]


def test_a_plane_with_no_agent_ca_yet_waits():
    svc = SimpleNamespace(openchoreo=None)
    out = dataplanes._register(svc, "gcp", "p", "cln-prod", {}, {}, infra_repo="acme/infra", thread_id="t",
                               approver="m", token=lambda *a: "tok")
    assert out["state"] == "built"  # the fake endpoint can't be reached: retried next time


def test_unknown_clouds_are_named():
    assert onboarding.parse("ibm", "x") == ({}, "ibm isn't a cloud Valxel builds on")


def test_an_unknown_installation_cloud_counts_as_aws(monkeypatch):
    from valxel_sdlc.intake import installation_cloud

    monkeypatch.setenv("VALXEL_CLOUD", "oracle")
    assert installation_cloud() == "aws"
    oc = SimpleNamespace(data_plane=lambda name, namespace=None: {"state": "absent"})
    gh = SimpleNamespace(put_file=lambda **kw: None, open_pr=lambda **kw: {"url": "u"})
    steps = environments.setup(SimpleNamespace(github=gh, openchoreo=oc), product_id="CLN", tier=2, approver="m",
                               infra_repo="acme/infra")
    assert steps[0]["step"] == "clusters_pr"


def test_the_draft_editor_takes_the_accounts_or_later(monkeypatch):
    from valxel_sdlc.intake import Draft, IntakeAgent, IntakeSession, set_cloud

    monkeypatch.setenv("VALXEL_CLOUD", "gcp")
    d = Draft(size="product")
    set_cloud(d, "non-production: acme-a-np, production: acme-a-prod, region: us-east1")
    assert (d.cloud, d.region) == ("given", "us-east1")
    set_cloud(d, "later")
    assert (d.cloud, d.cloud_accounts) == ("later", {})
    with pytest.raises(ValueError, match="production"):
        set_cloud(d, "production: acme-a-prod")
    agent = IntakeAgent(SimpleNamespace(settings=SimpleNamespace(mode="stub"), registry=None))
    s = IntakeSession(id="s", owner="o", lanes=[], starter=True)
    s.draft = Draft(size="product", name="A")
    assert "couldn't find" in agent._answer_cloud(s, "acme-only-one, region: us-east1")
    assert s.options == ["Help me create them", "Set them up later"]


def test_the_api_gives_the_installations_guide(engine, monkeypatch):
    from fastapi.testclient import TestClient

    from valxel_sdlc.api.app import create_app
    from valxel_sdlc.web.auth import AuthConfig

    monkeypatch.setenv("VALXEL_CLOUD", "azure")
    with TestClient(create_app(engine, auth=AuthConfig(mode="dev", secret="x" * 40), web_dist=None)) as c:
        c.post("/api/auth/dev-login", json={"login": "sunil"})
        g = c.get("/api/cloud/onboarding", params={"product": "CLN"}).json()
    assert g["name"] == "Azure" and g["noun"] == "subscription" and any("CLN" in s for s in g["steps"])


def test_the_cli_reports_each_plane_before_the_environments(monkeypatch, capsys):
    from valxel_sdlc import cli, config, services

    runtime = {"mode": "full", "cloud": {"provider": "gcp", "region": "r", "accounts": {}}}
    svc = SimpleNamespace(snapshot=lambda pid: SimpleNamespace(product=SimpleNamespace(runtime=runtime)))
    monkeypatch.setattr(config, "load_settings", lambda mode: None)
    monkeypatch.setattr(services, "build_services", lambda s: svc)
    monkeypatch.setattr(dataplanes, "build", lambda *a, **kw: {
        "planes": {"cln-nonprod": {"state": "registering", "why": "waiting for the PR", "pr": "https://pr/1"}},
        "waiting": ["cln-prod is being built"]})
    monkeypatch.setattr(environments, "environments",
                        lambda *a, **kw: {"why": "waiting for the data planes", "waiting": ["x"]})
    assert cli._main(["environments", "--product", "CLN", "--approver", "maria"]) == 2
    out = capsys.readouterr().out
    assert "cln-nonprod: registering (waiting for the PR) https://pr/1" in out
    assert "waiting: cln-prod is being built" in out and "waiting for the data planes" in out

"""The self-hosted Helm chart (deploy/helm/valxel, T-122): what it renders, and what it refuses.

Rendering needs helm, which GitHub's Ubuntu runners have; in CI these tests fail rather than skip
without it, so the chart is always checked there."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
CHART = ROOT / "deploy" / "helm" / "valxel"
CI_VALUES = CHART / "ci" / "test-values.yaml"


def test_the_sessions_egress_list_is_the_tools_own():
    """One egress list (T-129): the chart's copy is config/egress.yaml's."""
    chart = yaml.safe_load((CHART / "values.yaml").read_text())["sessions"]["egress"]
    assert chart == yaml.safe_load((ROOT / "config" / "egress.yaml").read_text())["allowed"]


def _helm():
    helm = shutil.which("helm")
    if not helm:
        if os.environ.get("CI"):
            pytest.fail("helm is not on this runner: the chart must be rendered in CI")
        pytest.skip("helm is not installed here")
    return helm


def _render(*sets: str, values=CI_VALUES, ok=True):
    args = [_helm(), "template", "valxel", str(CHART), "--namespace", "valxel", "-f", str(values)]
    for s in sets:
        args += ["--set", s]
    run = subprocess.run(args, capture_output=True, text=True, check=False)
    if not ok:
        assert run.returncode != 0, "expected helm to refuse these values"
        return run.stderr
    assert run.returncode == 0, run.stderr
    return [d for d in yaml.safe_load_all(run.stdout) if d]


def _pods(docs):
    for d in docs:
        if d["kind"] in ("Deployment",):
            yield d["metadata"]["name"], d["spec"]["template"]["spec"]
        if d["kind"] == "SandboxTemplate":
            yield d["metadata"]["name"], d["spec"]["podTemplate"]["spec"]


def test_the_chart_lints():
    run = subprocess.run([_helm(), "lint", "--strict", str(CHART), "-f", str(CI_VALUES)],
                         capture_output=True, text=True, check=False)
    assert run.returncode == 0, run.stdout + run.stderr


@pytest.mark.parametrize("sets", [(), ("kafka.mode=external", "kafka.bootstrap=kafka.example:9094",
                                       "kafka.caSecret=kafka-ca", "kafka.users.gateway=k-gw",
                                       "kafka.users.sink=k-sink", "kafka.users.tool=k-tool"),
                                  ("networkPolicy.flavor=kubernetes", "mesh.istioAmbient=true",
                                   "route.gatewayApi.enabled=false", "route.ingress.enabled=true",
                                   "route.ingress.tlsSecretName=tls")])
def test_every_process_is_hardened_and_runs_images_by_digest(sets):
    docs = _render(*sets)
    pods = dict(_pods(docs))
    assert {"valxel-api", "valxel-worker", "valxel-provisioner", "valxel-llm-gateway", "valxel-audit-sink",
            "valxel-egress-proxy", "agent-session"} <= set(pods)
    for name, spec in pods.items():
        assert spec["securityContext"]["runAsNonRoot"] is True, name
        for c in spec.get("initContainers", []) + spec["containers"]:
            assert "@sha256:" in c["image"] and ":latest" not in c["image"], (name, c["image"])
            sc = c["securityContext"]
            assert sc["allowPrivilegeEscalation"] is False and sc["readOnlyRootFilesystem"] is True, (name, c["name"])
            assert sc["capabilities"]["drop"] == ["ALL"], (name, c["name"])
            # No secret is written into a manifest: secret values come from Secrets only.
            for e in c.get("env", []):
                if any(e["name"].endswith(s) for s in ("_KEY", "_SECRET", "_PASSWORD", "_TOKEN", "DATABASE_URL")):
                    assert "valueFrom" in e and "value" not in e, (name, e["name"])
    session = pods["agent-session"]
    assert session["runtimeClassName"] == "gvisor" and session["automountServiceAccountToken"] is False


def test_the_settings_are_productions():
    settings = next(d for d in _render()
                    if d["kind"] == "ConfigMap" and d["metadata"]["name"] == "valxel-settings")
    data = settings["data"]
    assert data["VALXEL_STRICT"] == "1" and data["VALXEL_TIER"] == "cluster" and data["VALXEL_MODE"] == "live"
    assert data["VALXEL_SESSION_EXECUTOR"] == "kubernetes" and data["VALXEL_TENANCY"] == "rls"
    assert data["VALXEL_SECRET_STORE"] == "files" and data["VALXEL_PROVISIONING"] == "queue"
    assert data["VALXEL_INDEPENDENT_REVIEW"] == "off"  # until switched on (tool.independentReview)
    assert data["VALXEL_LLM_GATEWAY_URL"] == "http://valxel-llm-gateway.valxel.svc.cluster.local:8080"
    assert data["VALXEL_GIT_BROKER_URL"] == "http://valxel-egress-proxy.valxel.svc.cluster.local:3128"
    assert "sasl.password" not in data["VALXEL_AUDIT_KAFKA_CONFIG"]
    # The strict-mode settings the tool checks are all set (valxel_sdlc.strict).
    for name in ("VALXEL_AUDIT_ANCHOR_BUCKET", "VALXEL_CHECKPOINT_ARCHIVE_BUCKET", "VALXEL_GITHUB_APP_KMS_KEY_ID"):
        assert data[name], name


def test_sign_in_through_the_teams_own_identity_provider():
    settings = next(d for d in _render("auth.mode=oidc", "auth.oidc.issuer=https://login.acme.example",
                                       "auth.oidc.clientId=valxel", "auth.oidc.groupsClaim=groups",
                                       "auth.oidc.strongAcr={a,b}")
                    if d["kind"] == "ConfigMap" and d["metadata"]["name"] == "valxel-settings")["data"]
    assert settings["VALXEL_AUTH"] == "oidc" and settings["VALXEL_OIDC_ISSUER"] == "https://login.acme.example"
    assert settings["VALXEL_OIDC_GROUPS_CLAIM"] == "groups" and settings["VALXEL_OIDC_STRONG_ACR"] == "a,b"
    assert "VALXEL_GITHUB_OAUTH_CLIENT_ID" not in settings and "VALXEL_OIDC_CLIENT_SECRET" not in settings
    assert "auth.oidc.issuer" in _render("auth.mode=oidc", ok=False)


def test_only_the_provisioner_can_sign_for_the_provisioning_app():
    docs = _render()
    deployments = {d["metadata"]["name"]: d for d in docs if d["kind"] == "Deployment"}

    def env(name):
        containers = deployments[name]["spec"]["template"]["spec"]["containers"]
        return {e["name"] for c in containers for e in c.get("env", [])}

    assert "VALXEL_GITHUB_PROVISIONER_APP_KMS_KEY_ID" in env("valxel-provisioner")
    for other in ("valxel-api", "valxel-worker"):
        assert not any("PROVISIONER" in e for e in env(other)), other
    sa = {n: d["spec"]["template"]["spec"]["serviceAccountName"] for n, d in deployments.items()}
    assert sa["valxel-provisioner"] != sa["valxel-api"]


def _named(docs, kind, name):
    return next(d for d in docs if d["kind"] == kind and d["metadata"]["name"] == name)


def test_sessions_reach_only_the_egress_proxy_and_the_gateway():
    """The engineering review's egress recommendation: no session connects out on its own."""
    docs = _render()
    policy = _named(docs, "CiliumNetworkPolicy", "agent-session")
    assert not any("toFQDNs" in e for e in policy["spec"]["egress"])
    reached = [m["matchLabels"].get("app") for e in policy["spec"]["egress"] for m in e.get("toEndpoints", [])]
    assert "egress-proxy" in reached and "llm-gateway" in reached
    dns = [r["matchName"] for e in policy["spec"]["egress"] for p in e.get("toPorts", [])
           for r in (p.get("rules") or {}).get("dns", [])]
    assert dns == ["valxel-egress-proxy.valxel.svc.cluster.local", "valxel-llm-gateway.valxel.svc.cluster.local"]
    assert {"toCIDR": ["169.254.169.254/32", "fd00:ec2::254/128"]} in policy["spec"]["egressDeny"]
    proxy = _named(docs, "CiliumNetworkPolicy", "valxel-egress-proxy")
    fqdns = [r["matchName"] for e in proxy["spec"]["egress"] for r in e.get("toFQDNs", [])]
    assert fqdns == yaml.safe_load((ROOT / "config" / "egress.yaml").read_text())["allowed"]


def test_with_portable_policy_sessions_still_reach_only_the_proxy_and_the_gateway():
    docs = _render("networkPolicy.flavor=kubernetes")
    session = _named(docs, "NetworkPolicy", "agent-session")
    assert not any("ipBlock" in t for e in session["spec"]["egress"] for t in e["to"])
    proxy = _named(docs, "NetworkPolicy", "valxel-egress-proxy")
    assert any("ipBlock" in t for e in proxy["spec"]["egress"] for t in e["to"])


def test_the_proxy_holds_the_broker_key_and_nothing_else_of_the_tools():
    docs = _render()
    spec = _named(docs, "Deployment", "valxel-egress-proxy")["spec"]["template"]["spec"]
    assert spec["automountServiceAccountToken"] is False
    key = next(v for v in spec["volumes"] if v["name"] == "broker-key")["secret"]
    assert key["items"] == [{"key": "VALXEL_GIT_BROKER_KEY", "path": "VALXEL_GIT_BROKER_KEY"}]
    env = {e["name"]: e.get("value") for e in spec["containers"][0]["env"]}
    assert env["VALXEL_EGRESS_GITHUB_DIRECT"] == "false" and "DATABASE_URL" not in env
    session = _named(docs, "SandboxTemplate", "agent-session")["spec"]["podTemplate"]["spec"]["containers"][0]
    senv = {e["name"]: e.get("value") for e in session["env"]}
    assert senv["HTTPS_PROXY"] == "http://valxel-egress-proxy.valxel.svc.cluster.local:3128"
    assert "valxel-llm-gateway.valxel.svc.cluster.local" in senv["NO_PROXY"]


def test_registry_mirrors_are_used_and_allowed():
    docs = _render("sessions.registryMirrors.npm=https://artifacts.acme.test/api/npm/npm/",
                   "sessions.registryMirrors.pypi=https://artifacts.acme.test/api/pypi/pypi/simple")
    session = _named(docs, "SandboxTemplate", "agent-session")["spec"]["podTemplate"]["spec"]["containers"][0]
    senv = {e["name"]: e.get("value") for e in session["env"]}
    assert senv["NPM_CONFIG_REGISTRY"] == "https://artifacts.acme.test/api/npm/npm/"
    assert senv["PIP_INDEX_URL"] == "https://artifacts.acme.test/api/pypi/pypi/simple"
    allowed = yaml.safe_load(_named(docs, "ConfigMap", "valxel-egress-proxy")["data"]["egress.yaml"])["allowed"]
    assert allowed.count("artifacts.acme.test") == 1


@pytest.mark.parametrize("sets,says", [
    (("image.digest=",), "image: set its digest"),
    (("gateway.image.digest=v1.2.3",), "digest must be sha256"),
    (("publicUrl=http://sdlc.example.test",), "publicUrl must be https"),
    (("gateway.toolKeySha256=",), "gateway.toolKeySha256"),
    (("sessions.namespace=valxel",), "sessions.namespace must differ"),
    (("tool.extraSettings.VALXEL_SMTP_PASSWORD=x",), "looks like a secret"),
    (("archives.checkpointBucket=",), "archives.checkpointBucket"),
    (("sessions.registryMirrors.npm=http://artifacts.acme.test/npm/",), "registryMirrors.npm must be https"),
    (("cloud.provider=oracle",), "cloud.provider is aws, gcp or azure"),
])
def test_it_refuses_what_production_would_refuse(sets, says):
    assert says in _render(*sets, ok=False)


def _settings(*sets):
    return next(d for d in _render(*sets)
                if d["kind"] == "ConfigMap" and d["metadata"]["name"] == "valxel-settings")["data"]


def test_the_installation_names_its_cloud():
    """ADR 0010: one cloud per installation. awsRegion still reads as cloud.region."""
    data = _settings()
    assert (data["VALXEL_CLOUD"], data["AWS_REGION"]) == ("aws", "us-east-1")
    assert _settings("cloud.region=eu-west-1")["AWS_REGION"] == "eu-west-1"
    gcp = _settings(*GCP)
    assert gcp["VALXEL_CLOUD"] == "gcp" and "AWS_REGION" not in gcp and gcp["VALXEL_GCP_PROJECT"] == "acme-valxel"
    more = _settings(*GCP, "cloud.serviceAccount=valxel@acme-valxel.iam.gserviceaccount.com",
                     "cloud.billingDataset=acme-billing.valxel_billing")
    assert more["VALXEL_GCP_SERVICE_ACCOUNT"].startswith("valxel@") and more["VALXEL_GCP_BILLING_DATASET"]


GCP = ("cloud.provider=gcp", "cloud.region=us-central1", "cloud.project=acme-valxel",
       "github.appKmsKeyId=projects/acme-valxel/locations/global/keyRings/valxel/cryptoKeys/app/cryptoKeyVersions/1",
       "github.provisionerKmsKeyId=projects/acme-valxel/locations/global/keyRings/valxel/cryptoKeys/prov/"
       "cryptoKeyVersions/1",
       "archives.checkpointBucket=gs://acme-checkpoints", "archives.auditAnchorBucket=acme-anchors",
       "archives.auditArchiveBucket=gs://acme-audit/")


def test_on_google_cloud_buckets_and_keys_are_googles():
    """CP-107: gs:// buckets by name, Cloud KMS key versions, and the audit sink told to use GCS."""
    docs = _render(*GCP)
    data = next(d for d in docs if d["kind"] == "ConfigMap" and d["metadata"]["name"] == "valxel-settings")["data"]
    assert (data["VALXEL_CHECKPOINT_ARCHIVE_BUCKET"], data["VALXEL_AUDIT_ANCHOR_BUCKET"]) == (
        "acme-checkpoints", "acme-anchors")
    sink = next(spec for name, spec in _pods(docs) if name == "valxel-audit-sink")
    env = {e["name"]: e.get("value") for e in sink["containers"][0]["env"]}
    assert (env["AUDIT_SINK_ARCHIVE_BUCKET"], env["AUDIT_SINK_ARCHIVE_PROVIDER"]) == ("acme-audit", "gcs")
    aws_env = {e["name"]: e.get("value") for e in next(
        spec for name, spec in _pods(_render()) if name == "valxel-audit-sink")["containers"][0]["env"]}
    assert aws_env["AUDIT_SINK_ARCHIVE_PROVIDER"] == "s3"


@pytest.mark.parametrize("extra,says", [
    (("cloud.project=",), "cloud.project"),
    (("github.appKmsKeyId=arn:aws:kms:us-east-1:1:key/x",), "github.appKmsKeyId must be a Cloud KMS key version"),
    (("archives.auditAnchorBucket=s3://acme-anchors",), "is an S3 bucket, but cloud.provider is gcp"),
    (("auth.mode=cognito",), "auth.mode cognito is AWS's"),
])
def test_on_google_cloud_it_refuses_aws_settings(extra, says):
    assert says in _render(*GCP, *extra, ok=False)


AZURE = ("cloud.provider=azure", "cloud.region=eastus", "cloud.subscription=1b2c3d4e-0000-4000-8000-00000000aa01",
         "cloud.keyVault=https://acme-valxel.vault.azure.net/",
         "cloud.federationIssuer=https://eastus.oic.prod-aks.azure.com/tenant/cluster/",
         "github.appKmsKeyId=https://acme-valxel.vault.azure.net/keys/delivery-app/0123456789abcdef0123456789abcdef",
         "github.provisionerKmsKeyId=https://acme-valxel.vault.azure.net/keys/provisioner-app/"
         "0123456789abcdef0123456789abcdef",
         "archives.checkpointBucket=https://acmevalxel.blob.core.windows.net/checkpoints",
         "archives.auditAnchorBucket=https://acmevalxel.blob.core.windows.net/anchors",
         "archives.auditArchiveBucket=https://acmevalxel.blob.core.windows.net/audit/")


def test_on_azure_containers_keys_and_workload_identity_are_azures():
    """CP-401/402/404: Key Vault, Blob Storage containers by URL, AKS Workload Identity on the pods."""
    docs = _render(*AZURE)
    data = next(d for d in docs if d["kind"] == "ConfigMap" and d["metadata"]["name"] == "valxel-settings")["data"]
    assert data["VALXEL_CLOUD"] == "azure" and "AWS_REGION" not in data
    assert data["VALXEL_AZURE_KEY_VAULT"] == "https://acme-valxel.vault.azure.net"
    assert data["VALXEL_AZURE_FEDERATION_SUBJECT"] == "system:serviceaccount:valxel:valxel"
    assert data["VALXEL_CHECKPOINT_ARCHIVE_BUCKET"] == "https://acmevalxel.blob.core.windows.net/checkpoints"
    sink = next(spec for name, spec in _pods(docs) if name == "valxel-audit-sink")
    env = {e["name"]: e.get("value") for e in sink["containers"][0]["env"]}
    assert (env["AUDIT_SINK_ARCHIVE_BUCKET"], env["AUDIT_SINK_ARCHIVE_PROVIDER"]) == (
        "https://acmevalxel.blob.core.windows.net/audit", "azure")
    tool = next(d for d in docs if d["kind"] == "Deployment" and d["metadata"]["name"] == "valxel-api")
    assert tool["spec"]["template"]["metadata"]["labels"]["azure.workload.identity/use"] == "true"


@pytest.mark.parametrize("extra,says", [
    (("cloud.subscription=",), "cloud.subscription"),
    (("cloud.keyVault=",), "cloud.keyVault"),
    (("github.appKmsKeyId=projects/p/locations/l/keyRings/r/cryptoKeys/k/cryptoKeyVersions/1",),
     "github.appKmsKeyId must be a Key Vault key version"),
    (("archives.auditAnchorBucket=gs://acme-anchors",), "is a Cloud Storage bucket, but cloud.provider is azure"),
    (("archives.auditAnchorBucket=acme-anchors",), "is a Blob Storage container on Azure"),
    (("auth.mode=cognito",), "auth.mode cognito is AWS's: on Azure"),
])
def test_on_azure_it_refuses_other_clouds_settings(extra, says):
    assert says in _render(*AZURE, *extra, ok=False)


def test_building_data_planes_lets_the_tool_make_jobs_and_nothing_more():
    off = _render()
    assert not [d for d in off if d["kind"] == "Role" and d["metadata"]["name"].endswith("-build-planes")]
    sets = ("planes.enabled=true", "planes.builderImage=ghcr.io/valxel-platform/plane-builder:main@sha256:" + "a" * 64,
            "planes.dbLoginsImage=ghcr.io/valxel-platform/db-logins:main@sha256:" + "b" * 64,
            "planes.domain=planes.example.com", "planes.controlPlaneDomain=cp.example.com",
            "planes.acmeEmail=ops@example.com", "planes.adminCidrs={203.0.113.10/32}")
    docs = _render(*sets)
    role = next(d for d in docs if d["kind"] == "Role" and d["metadata"]["name"].endswith("-build-planes"))
    assert {r["resources"][0]: tuple(r["verbs"]) for r in role["rules"]} == {
        "jobs": ("create", "get", "delete"), "configmaps": ("create", "delete"), "pods": ("list",),
        "pods/log": ("get",)}
    settings = next(d for d in docs if d["kind"] == "ConfigMap" and d["metadata"]["name"].endswith("-settings"))
    assert settings["data"]["VALXEL_PLANE_ADMIN_CIDRS"] == "203.0.113.10/32"
    assert "planes.builderImage" in _render("planes.enabled=true", ok=False)

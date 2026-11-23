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
            "agent-session"} <= set(pods)
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
    assert data["VALXEL_LLM_GATEWAY_URL"] == "http://valxel-llm-gateway.valxel.svc.cluster.local:8080"
    assert "sasl.password" not in data["VALXEL_AUDIT_KAFKA_CONFIG"]
    # The strict-mode settings the tool checks are all set (valxel_sdlc.strict).
    for name in ("VALXEL_AUDIT_ANCHOR_BUCKET", "VALXEL_CHECKPOINT_ARCHIVE_BUCKET", "VALXEL_GITHUB_APP_KMS_KEY_ID"):
        assert data[name], name


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


def test_sessions_reach_only_the_egress_list_and_the_gateway():
    policy = next(d for d in _render()
                  if d["kind"] == "CiliumNetworkPolicy" and d["metadata"]["name"] == "agent-session")
    fqdns = [r["matchName"] for e in policy["spec"]["egress"] for r in e.get("toFQDNs", [])]
    assert fqdns == yaml.safe_load((ROOT / "config" / "egress.yaml").read_text())["allowed"]
    assert {"toCIDR": ["169.254.169.254/32", "fd00:ec2::254/128"]} in policy["spec"]["egressDeny"]


@pytest.mark.parametrize("sets,says", [
    (("image.digest=",), "image: set its digest"),
    (("gateway.image.digest=v1.2.3",), "digest must be sha256"),
    (("publicUrl=http://sdlc.example.test",), "publicUrl must be https"),
    (("gateway.toolKeySha256=",), "gateway.toolKeySha256"),
    (("sessions.namespace=valxel",), "sessions.namespace must differ"),
    (("tool.extraSettings.VALXEL_SMTP_PASSWORD=x",), "looks like a secret"),
    (("archives.checkpointBucket=",), "archives.checkpointBucket"),
])
def test_it_refuses_what_production_would_refuse(sets, says):
    assert says in _render(*sets, ok=False)

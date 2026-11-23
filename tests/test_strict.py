"""Strict mode: in production, nothing quietly runs as an offline stand-in."""

import tempfile
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from valxel_sdlc import strict
from valxel_sdlc.api.app import create_app
from valxel_sdlc.integrations.worktracking import StubTracker
from valxel_sdlc.web.auth import AuthConfig

# Secrets in a files store (as the deployment mounts them), never the environment.
_SECRETS = tempfile.mkdtemp(prefix="strict-secrets-")
Path(_SECRETS, "VALXEL_SERVICENOW_AUTH_CLN").write_text("Basic x")
Path(_SECRETS, "VALXEL_GIT_BROKER_KEY").write_text("ab" * 32)
LIVE = {"VALXEL_DELIVERY": "live", "VALXEL_PROVISIONING": "queue", "VALXEL_SECRET_STORE": "files",
        "VALXEL_SECRETS_DIR": _SECRETS, "VALXEL_TENANCY": "rls", "VALXEL_SESSION_EXECUTOR": "kubernetes",
        "VALXEL_LLM_GATEWAY_URL": "http://llm-gateway", "VALXEL_AUDIT_SINK": "kafka", "VALXEL_AUDIT_ANCHOR_BUCKET": "valxel-audit-anchors", "VALXEL_RUN_QUEUE": "postgres", "VALXEL_CHECKPOINT_ARCHIVE_BUCKET": "valxel-checkpoints", "VALXEL_SIGNALS_TOKENS": "scoped", "VALXEL_GITHUB_APP_KMS_KEY_ID": "arn:aws:kms:us-east-2:1:key/app",
        "VALXEL_GIT_BROKER_URL": "http://egress-proxy:3128"}


class LiveTracker:
    """Stands for a configured Jira or OpenProject: anything but the stand-in class."""

    def __init__(self, name):
        self.name = name


def live_ish(services):
    s = services.settings
    s.mode, s.governance_source, s.database_url = "live", "github", "postgresql://db"
    services.trackers = {name: LiveTracker(name) for name in services.trackers}
    from valxel_sdlc.integrations.diagrams import LucidPublisher

    services.diagrams["lucid"] = LucidPublisher(object())
    services.flags.describe = lambda pid: "flipt"
    from valxel_sdlc.telemetry import Telemetry, TelemetryConfig

    services.telemetry = Telemetry(TelemetryConfig(endpoint="http://collector:4318"), provider=_Provider())
    return services


class _Provider:  # stands for the OTLP exporter's tracer provider
    def get_tracer(self, name):
        from opentelemetry.sdk.trace import TracerProvider

        return TracerProvider().get_tracer(name)


def test_the_default_setup_lists_every_stand_in(services):
    found = strict.stand_ins(services, auth_mode="dev", env={})
    assert "VALXEL_MODE is not live: agents return canned output" in found
    assert any(f.startswith("VALXEL_DELIVERY is not live") for f in found)
    assert any(f.startswith("VALXEL_AUTH is dev") for f in found)
    assert not any(f.startswith("VALXEL_AUTH") for f in strict.stand_ins(services, auth_mode="oidc", env={}))
    assert any(f.startswith("DATABASE_URL is not set") for f in found)
    assert "FARM: tools.work_tracking names jira, which has no server URL or credentials" in found
    assert "FARM: tools.diagrams names lucid, which has no server URL or credentials" in found
    assert "CLN: tools.work_tracking names openproject, which has no server URL or credentials" in found
    assert not any("drawio" in f for f in found)  # draw.io commits files to the repo: it has no stand-in


def test_a_fully_live_setup_passes_and_only_warns_about_known_gaps(services, monkeypatch, tmp_path):
    monkeypatch.setenv("VALXEL_CONFIG_REPO", "valxel-platform/sdlc-config")
    monkeypatch.setenv("VALXEL_SECRET_STORE", "files")
    monkeypatch.setenv("VALXEL_SECRETS_DIR", str(tmp_path))
    (tmp_path / "VALXEL_SERVICENOW_AUTH_CLN").write_text("Basic x")
    (tmp_path / "VALXEL_GIT_BROKER_KEY").write_text("cd" * 32)
    live_ish(services)
    env = {"VALXEL_DELIVERY": "live", "VALXEL_PROVISIONING": "queue", "VALXEL_SECRET_STORE": "files", "VALXEL_TENANCY": "rls", "VALXEL_SESSION_EXECUTOR": "kubernetes", "VALXEL_LLM_GATEWAY_URL": "http://llm-gateway", "VALXEL_AUDIT_SINK": "kafka", "VALXEL_AUDIT_ANCHOR_BUCKET": "valxel-audit-anchors", "VALXEL_RUN_QUEUE": "postgres", "VALXEL_CHECKPOINT_ARCHIVE_BUCKET": "valxel-checkpoints", "VALXEL_SIGNALS_TOKENS": "scoped", "VALXEL_GITHUB_APP_KMS_KEY_ID": "arn:aws:kms:us-east-2:1:key/app",
           "VALXEL_SECRETS_DIR": str(tmp_path), "VALXEL_GIT_BROKER_URL": "http://egress-proxy:3128"}
    gaps = strict.check(services, auth_mode="github", env=env)
    assert gaps == []
    from valxel_sdlc.telemetry import Telemetry

    traced, services.telemetry = services.telemetry, Telemetry()  # no endpoint: sessions aren't traced
    assert strict.check(services, auth_mode="github", env=LIVE)[0].startswith("VALXEL_OTEL_ENDPOINT is not set")
    services.telemetry = traced
    real = services.snapshot

    def no_packages(pid, pins=None):  # a shared lane whose libraries don't say where they publish
        snap = real(pid, pins)
        if pid != "SS":
            return snap
        repos = tuple(replace(r, package=None) for r in snap.product.repos)
        return replace(snap, product=replace(snap.product, repos=repos))

    services.snapshot = no_packages
    assert strict.check(services, auth_mode="github", env=LIVE) == [
        "SS: acme-shared/ss-communication, acme-shared/ss-audit-framework, acme-shared/ss-security declare no "
        "package, so G6 can't check that a release candidate was published to GitHub Packages"]


def test_one_missing_token_is_enough_to_refuse(services, monkeypatch):
    monkeypatch.setenv("VALXEL_CONFIG_REPO", "valxel-platform/sdlc-config")
    live_ish(services)
    services.trackers["jira"] = StubTracker("jira")  # the Jira token has a typo
    with pytest.raises(strict.StrictModeError) as err:
        strict.check(services, auth_mode="github", env=LIVE)
    assert "FARM: tools.work_tracking names jira" in str(err.value) and "openproject" not in str(err.value)
    services.trackers["jira"] = LiveTracker("jira")
    real = services.snapshot

    def with_servicenow(pid, pins=None):  # CLN sends receipts to its ServiceNow
        snap = real(pid, pins)
        if pid != "CLN":
            return snap
        tools = {**snap.product.tools, "change_management": {"provider": "servicenow", "instance": "https://x"}}
        return replace(snap, product=replace(snap.product, tools=tools))

    services.snapshot = with_servicenow
    with pytest.raises(strict.StrictModeError, match="VALXEL_SERVICENOW_AUTH_CLN is not set"):
        strict.check(services, auth_mode="github", env={**LIVE, "VALXEL_SECRETS_DIR": tempfile.mkdtemp()})
    assert strict.check(services, auth_mode="github", env=LIVE) == []


def test_live_mode_needs_a_secret_store(services, monkeypatch):
    monkeypatch.setenv("VALXEL_CONFIG_REPO", "valxel-platform/sdlc-config")
    live_ish(services)
    env = {k: v for k, v in LIVE.items() if not k.startswith("VALXEL_SECRET")}
    with pytest.raises(strict.StrictModeError, match="VALXEL_SECRET_STORE is env"):
        strict.check(services, auth_mode="github", env={**env, "VALXEL_SERVICENOW_AUTH_CLN": "Basic x"})
    # A store, with a secret left in the environment as well: refused too.
    with pytest.raises(strict.StrictModeError, match="also in the environment: ANTHROPIC_API_KEY"):
        strict.check(services, auth_mode="github", env={**LIVE, "ANTHROPIC_API_KEY": "sk"})
    # Off live mode (a laptop), environment variables are fine.
    assert strict.secrets_in_environment({"ANTHROPIC_API_KEY": "sk"}) == []


def test_the_server_refuses_to_start_in_strict_mode(monkeypatch, settings, services):
    import valxel_sdlc.api.app as app_mod
    from langgraph.checkpoint.memory import InMemorySaver

    from valxel_sdlc.engine import FeatureEngine

    monkeypatch.setenv("VALXEL_STRICT", "1")
    monkeypatch.setattr(app_mod, "create_engine_from_env", lambda: (FeatureEngine(services, InMemorySaver()), None))
    app = create_app(auth=AuthConfig(mode="dev", secret="x" * 40), web_dist=None)
    with pytest.raises(strict.StrictModeError, match="VALXEL_MODE is not live"):
        with TestClient(app):
            pass
    monkeypatch.setenv("VALXEL_STRICT", "0")
    with TestClient(create_app(auth=AuthConfig(mode="dev", secret="x" * 40), web_dist=None)) as c:
        assert c.get("/api/healthz").json()["strict"] is False


def test_live_delivery_needs_the_app_key_in_kms_and_nowhere_else(tmp_path):
    key = tmp_path / "app.pem"
    key.write_text("-----BEGIN PRIVATE KEY-----\nx\n-----END PRIVATE KEY-----\n")
    live = {"VALXEL_DELIVERY": "live"}
    assert strict.app_key_in_kms({**live, "VALXEL_GITHUB_APP_PRIVATE_KEY_FILE": str(key)}) == [(
        "VALXEL_GITHUB_APP_KMS_KEY_ID is not set: the GitHub App's private key is held by this process "
        "(VALXEL_GITHUB_APP_PRIVATE_KEY_FILE); import it into KMS as a sign-only key and set the key's id "
        "(docs: GitHub App key in KMS)")]
    both = strict.app_key_in_kms({**live, "VALXEL_GITHUB_APP_KMS_KEY_ID": "k", "VALXEL_GITHUB_APP_PRIVATE_KEY_FILE": str(key)})
    assert both and "still held here too" in both[0]
    assert strict.app_key_in_kms({**live, "VALXEL_GITHUB_APP_KMS_KEY_ID": "k"}) == []
    assert strict.app_key_in_kms({"VALXEL_GITHUB_APP_PRIVATE_KEY_FILE": str(key)}) == []  # stub delivery: a laptop


def test_regulated_lanes_need_the_checkpoint_archive(services):
    from valxel_sdlc import strict

    services.settings.database_url = "postgresql://db/valxel"
    gaps = strict.stand_ins(services, auth_mode="github", env={})
    [gap] = [g for g in gaps if "CHECKPOINT_ARCHIVE" in g]
    assert "CLN" in gap and "FARM" not in gap
    gaps = strict.stand_ins(services, auth_mode="github", env={"VALXEL_CHECKPOINT_ARCHIVE_BUCKET": "b"})
    assert not [g for g in gaps if "CHECKPOINT_ARCHIVE" in g]


# --------------------------------------------------------------------------- tiers (ADR 0007, T-69)

VM = {"VALXEL_TIER": "vm", "VALXEL_PUBLIC_URL": "https://valxel.acme.dev", "VALXEL_SESSION_RUNTIME": "runsc",
      "VALXEL_BACKUP_MARKER": "/backups/last-success"}


def test_the_vm_tier_requires_tls_gvisor_no_docker_socket_and_backups(tmp_path):
    assert strict.tier_checks(VM, tier="vm", live=True, docker_socket=str(tmp_path / "none")) == []
    sock = tmp_path / "docker.sock"
    sock.write_text("")
    found = strict.tier_checks({"VALXEL_PUBLIC_URL": "http://x"}, tier="vm", live=True, docker_socket=str(sock))
    assert [f.split(":")[0] for f in found] == ["VALXEL_PUBLIC_URL is not https", "VALXEL_SESSION_RUNTIME is not runsc",
                                                 f"{sock} is in the tool's container",
                                                 "VALXEL_BACKUP_MARKER is not set"]


def test_the_laptop_tier_isnt_for_strict_mode_and_tiers_are_named():
    [f] = strict.tier_checks({}, tier="laptop", live=True)
    assert f.startswith("VALXEL_TIER is laptop")
    assert strict.tier_checks({}, tier="edge", live=True) == ["VALXEL_TIER is one of laptop, vm, cluster, not 'edge'"]
    assert strict.tier_checks({}, tier="cluster", live=True) == []


def test_on_a_vm_sessions_run_in_containers_and_the_app_key_is_a_file_secret(services, monkeypatch, tmp_path):
    import os

    live_ish(services)
    real = os.path.exists
    monkeypatch.setattr(os.path, "exists", lambda p: False if p == "/var/run/docker.sock" else real(p))
    env = {**LIVE, **VM, "VALXEL_SESSION_EXECUTOR": "docker"}
    env.pop("VALXEL_GITHUB_APP_KMS_KEY_ID")
    found = strict.stand_ins(services, auth_mode="github", env=env)
    assert not any("SESSION_EXECUTOR" in f or "KMS_KEY_ID" in f or f.startswith("VALXEL_TIER") for f in found), found
    cluster = strict.stand_ins(services, auth_mode="github", env={**LIVE, "VALXEL_SESSION_EXECUTOR": "docker"})
    assert any("docker on the cluster tier" in f for f in cluster)


def test_production_sessions_reach_github_only_through_the_broker(services):
    """The engineering review's egress recommendation: sessions hold a pass, not GitHub tokens."""
    live_ish(services)
    env = {k: v for k, v in LIVE.items() if k != "VALXEL_GIT_BROKER_URL"}
    assert any("VALXEL_GIT_BROKER_URL" in f for f in strict.stand_ins(services, auth_mode="github", env=env))
    assert not any("GIT_BROKER" in f for f in strict.stand_ins(services, auth_mode="github", env=LIVE))
    bad = strict.stand_ins(services, auth_mode="github", env={**LIVE, "VALXEL_GIT_BROKER_URL": "egress-proxy"})
    assert any("must be http(s)" in f for f in bad)


def test_strict_mode_names_the_installations_cloud():
    """ADR 0010: VALXEL_CLOUD is a known cloud with adapters; aws is the default."""
    from valxel_sdlc.cloud import strict_problems

    assert strict_problems({}) == [] and strict_problems({"VALXEL_CLOUD": "AWS"}) == []
    assert "no adapters yet" in strict_problems({"VALXEL_CLOUD": "gcp"})[0]
    assert "not 'oracle'" in strict_problems({"VALXEL_CLOUD": "oracle"})[0]

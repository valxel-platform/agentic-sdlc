"""Strict mode: in production, nothing quietly runs as an offline stand-in."""

from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from valxel_sdlc import strict
from valxel_sdlc.api.app import create_app
from valxel_sdlc.integrations.worktracking import StubTracker
from valxel_sdlc.web.auth import AuthConfig

LIVE = {"VALXEL_DELIVERY": "live", "VALXEL_SERVICENOW_AUTH_CLN": "Basic x"}


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
    assert any(f.startswith("VALXEL_AUTH is not github") for f in found)
    assert any(f.startswith("DATABASE_URL is not set") for f in found)
    assert "FARM: tools.work_tracking names jira, which has no server URL or credentials" in found
    assert "FARM: tools.diagrams names lucid, which has no server URL or credentials" in found
    assert "CLN: tools.work_tracking names openproject, which has no server URL or credentials" in found
    assert not any("drawio" in f for f in found)  # draw.io commits files to the repo: it has no stand-in


def test_a_fully_live_setup_passes_and_only_warns_about_known_gaps(services, monkeypatch):
    monkeypatch.setenv("VALXEL_CONFIG_REPO", "valxel-platform/sdlc-config")
    live_ish(services)
    gaps = strict.check(services, auth_mode="github", env=LIVE)
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
        strict.check(services, auth_mode="github", env={"VALXEL_DELIVERY": "live"})
    assert strict.check(services, auth_mode="github", env=LIVE) == []


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

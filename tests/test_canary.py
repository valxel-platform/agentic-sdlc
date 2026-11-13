"""A canary for the agents' config: a share of new features runs on a candidate config, and the
pilot report compares the two by config version."""

import shutil

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from valxel_sdlc import pilot
from valxel_sdlc.config import load_settings
from valxel_sdlc.engine import FeatureEngine
from valxel_sdlc.services import build_services, default_canary


@pytest.fixture
def candidate(settings, tmp_path):
    """The config in use, with the spec agent on Opus instead of Sonnet."""
    d = tmp_path / "candidate"
    shutil.copytree(settings.config_dir, d)
    p = d / "models.yaml"
    p.write_text(p.read_text().replace("spec:       { runtime: generic,     model: anthropic/claude-sonnet-5-5 }",
                                       "spec:       { runtime: generic,     model: anthropic/claude-opus-5-5 }"))
    return d


def engine_with_canary(settings, monkeypatch, candidate, percent="50", classes="general"):
    monkeypatch.setenv("VALXEL_CANARY_CONFIG_DIR", str(candidate))
    monkeypatch.setenv("VALXEL_CANARY_PERCENT", percent)
    monkeypatch.setenv("VALXEL_CANARY_DATA_CLASSES", classes)
    return FeatureEngine(build_services(settings), InMemorySaver())


def test_a_share_of_new_features_runs_on_the_candidate_config(settings, monkeypatch, candidate):
    eng = engine_with_canary(settings, monkeypatch, candidate)
    svc = eng.services
    assert svc.canary.gateway.config_version != svc.gateway.config_version
    titles = ["Offline crop log", "Soil sensor import", "Harvest export", "Weather alerts", "Field notes", "Seed orders"]
    fids = [eng.create(product_id="FARM", title=t, pilot=True) for t in titles]
    for fid in fids:
        assert eng.auto_drive(fid)["status"] == "done"
    variants = {fid: eng.view(fid)["config_variant"] for fid in fids}
    canary = [f for f, v in variants.items() if v == "canary"]
    assert canary and len(canary) < len(fids)  # some, not all
    for fid in fids:  # every call of a canary feature ran under the candidate config, and only those
        versions = {r.config_version for r in svc.ledger.records(fid)}
        expected = svc.canary.gateway.config_version if fid in canary else svc.gateway.config_version
        assert versions == {expected}
    spec_models = {r.model for fid in canary for r in svc.ledger.records(fid) if r.role == "spec"}
    assert spec_models == {"anthropic/claude-opus-5-5"}
    rows = pilot.report(eng, "FARM")["by_config"]
    assert rows[svc.canary.gateway.config_version]["variant"] == "canary"
    assert rows[svc.gateway.config_version]["variant"] == "main"
    assert rows[svc.canary.gateway.config_version]["features"] == len(canary)


def test_regulated_lanes_and_hotfixes_stay_on_the_config_in_use(settings, monkeypatch, candidate):
    eng = engine_with_canary(settings, monkeypatch, candidate, percent="50")
    svc = eng.services
    assert all(svc.canary_variant(f"DNT-FEAT-{i}", "phi") == "" for i in range(40))
    assert all(svc.canary_variant(f"FARM-FEAT-{i}", "general", track="hotfix") == "" for i in range(40))
    assert any(svc.canary_variant(f"FARM-FEAT-{i}", "general") == "canary" for i in range(40))
    eng2 = engine_with_canary(settings, monkeypatch, candidate, percent="50", classes="general,phi")
    assert any(eng2.services.canary_variant(f"DNT-FEAT-{i}", "phi") == "canary" for i in range(40))


def test_a_bad_canary_refuses_to_start(settings, monkeypatch, candidate):
    monkeypatch.setenv("VALXEL_CANARY_CONFIG_DIR", str(candidate))
    monkeypatch.setenv("VALXEL_CANARY_PERCENT", "60")
    with pytest.raises(ValueError, match="at most 50"):
        default_canary(settings, None)
    monkeypatch.setenv("VALXEL_CANARY_PERCENT", "10")
    p = candidate / "models.yaml"
    p.write_text(p.read_text().replace("  release:    { runtime: generic,     model: anthropic/claude-sonnet-5-5 }\n", ""))
    with pytest.raises(ValueError, match="lacks roles release"):
        default_canary(settings, None)
    monkeypatch.setenv("VALXEL_CANARY_PERCENT", "0")
    assert default_canary(settings, None) is None


def test_canary_sessions_are_traced_under_the_candidates_config_version(settings, monkeypatch, candidate):
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    from valxel_sdlc.telemetry import Telemetry, TelemetryConfig

    eng = engine_with_canary(settings, monkeypatch, candidate, percent="50")
    exporter, provider = InMemorySpanExporter(), TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    eng.services.telemetry = Telemetry(TelemetryConfig(endpoint="http://c"), provider=provider,
                                       config_version=eng.services.gateway.config_version)
    fid = next(f for f in (f"FARM-FEAT-{i}" for i in range(1, 40)) if eng.services.canary_variant(f, "general"))
    n = int(fid.rsplit("-", 1)[1])
    for i in range(1, n):  # take the ids before it, so the next feature is the canary one
        eng.index.add(f"FARM-FEAT-{i}", "FARM", f"placeholder {i}", i)
    got = eng.create(product_id="FARM", title="Offline crop log")
    assert got == fid and eng.view(fid)["config_variant"] == "canary"
    versions = {s.attributes["valxel.config_version"] for s in exporter.get_finished_spans()}
    assert versions == {eng.services.canary.gateway.config_version}

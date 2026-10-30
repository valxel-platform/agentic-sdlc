import pytest
from langgraph.checkpoint.memory import InMemorySaver

from vrixel_sdlc.config import load_settings
from vrixel_sdlc.engine import FeatureEngine
from vrixel_sdlc.services import build_services


@pytest.fixture
def settings(tmp_path):
    return load_settings(mode="stub", database_url="", artifact_dir=tmp_path / "artifacts")


@pytest.fixture
def services(settings):
    return build_services(settings)


@pytest.fixture
def engine(services):
    return FeatureEngine(services, InMemorySaver())


def pending_of(engine, feature_id, kind=None):
    return [p for p in engine.pending(feature_id) if kind is None or p.get("gate") == kind or p["type"] == kind]


def approve(engine, feature_id, gate, **extra):
    [p] = [p for p in engine.pending(feature_id) if p.get("gate") == gate]
    return engine.decide(feature_id, p["interrupt_id"], {"decision": "approve", "approver": f"{gate}-owner", **extra})

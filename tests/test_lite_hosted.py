"""Lite mode, part 3: a lite product is hosted on Valxel's shared plane while it is built."""

from __future__ import annotations

import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from valxel_sdlc import environments
from valxel_sdlc.config import load_settings
from valxel_sdlc.engine import FeatureEngine
from valxel_sdlc.governance.manifest import GovernanceError
from valxel_sdlc.integrations.github import StubGitHub
from valxel_sdlc.integrations.openchoreo import StubOpenChoreo
from valxel_sdlc.services import build_services

ROOT = Path(__file__).resolve().parents[1]
PIPELINE = "  pipeline: [dev, staging, production]"


@pytest.fixture
def gov_root(tmp_path):
    root = tmp_path / "governance"
    shutil.copytree(ROOT / "examples" / "governance", root)
    return root


def farm_runtime(gov_root, extra: str) -> None:
    path = gov_root / "acme-farm" / "farm-governance" / "product.yaml"
    path.write_text(path.read_text().replace(PIPELINE, PIPELINE + "\n" + extra))


def settings(tmp_path, gov_root):
    return load_settings(mode="stub", database_url="", artifact_dir=tmp_path / "a",
                         governance_source="local", governance_root=gov_root)


# ------------------------------------------------------------------ setup at the product gate

def test_a_lite_product_gets_no_clusters_and_only_dev_and_staging(monkeypatch):
    monkeypatch.delenv("VALXEL_SHARED_DATA_PLANE", raising=False)
    monkeypatch.delenv("VALXEL_LITE_NAMESPACE", raising=False)
    oc, gh = StubOpenChoreo(), StubGitHub()
    oc.data_planes.add("valxel-shared-nonprod")
    services = SimpleNamespace(github=gh, openchoreo=oc)
    steps = environments.setup(services, product_id="EMR", tier=2, approver="sunil", infra_repo="acme/infra",
                               mode="lite")
    clusters, envs = steps
    assert clusters["ok"] and clusters["skipped"] and "lite mode" in clusters["why"]
    assert gh.prs == {}  # no cluster pull request
    assert envs["ok"] and envs["hosted"] and envs["namespace"] == "valxel-lite"
    assert set(oc.product_environments) == {"dev", "staging"}  # no production until go-live
    assert {e["data_plane"] for e in oc.product_environments.values()} == {"valxel-shared-nonprod"}
    assert oc.pipelines == {"emr-pipeline": ["dev", "staging"]}
    assert oc.projects["emr"]["namespace"] == "valxel-lite"


def test_hosting_waits_for_the_shared_plane(monkeypatch):
    monkeypatch.setenv("VALXEL_SHARED_DATA_PLANE", "shared-np")
    oc = StubOpenChoreo()
    out = environments.environments(SimpleNamespace(openchoreo=oc), product_id="EMR", approver="sunil", mode="lite")
    assert out["waiting"] == ["shared-np"] and "valxel environments --product EMR" in out["why"]
    assert oc.product_environments == {}


def test_a_full_product_still_gets_its_clusters():
    oc, gh = StubOpenChoreo(), StubGitHub()
    steps = environments.setup(SimpleNamespace(github=gh, openchoreo=oc), product_id="EMR", tier=2,
                               approver="sunil", infra_repo="acme/infra")
    assert "url" in steps[0] and steps[1]["waiting"] == ["emr-nonprod", "emr-prod"]


def test_runtime_entry_for_lite(monkeypatch):
    monkeypatch.delenv("VALXEL_LITE_NAMESPACE", raising=False)
    e = environments.runtime_entry("EMR", 2, "lite", "2026-11-13")
    assert (e["mode"], e["phase"], e["hosted_since"], e["namespace"]) == ("lite", "hosted", "2026-11-13", "valxel-lite")
    assert "production" not in e["data_planes"]
    assert "mode" not in environments.runtime_entry("EMR", 2)
    monkeypatch.setenv("VALXEL_RUNTIME_MODE", "LITE")
    assert environments.default_mode() == "lite"
    monkeypatch.setenv("VALXEL_RUNTIME_MODE", "tiny")
    assert environments.default_mode() == "full"


def test_hosted_means_lite_and_not_live():
    assert environments.hosted({"mode": "lite", "phase": "hosted"})
    assert environments.hosted({"mode": "lite", "phase": None})
    assert not environments.hosted({"mode": "lite", "phase": "live"})
    assert not environments.hosted({"mode": "full"}) and not environments.hosted(None)


# ------------------------------------------------------------------ product.yaml

def test_the_manifest_reads_the_runtime_mode(tmp_path, gov_root):
    farm_runtime(gov_root, "  mode: lite\n  hosted_since: '2026-11-13'")
    rt = build_services(settings(tmp_path, gov_root)).snapshot("FARM").product.runtime
    assert (rt["mode"], rt["phase"], rt["hosted_since"]) == ("lite", "hosted", "2026-11-13")


def test_the_manifest_defaults_to_full(tmp_path, gov_root):
    rt = build_services(settings(tmp_path, gov_root)).snapshot("FARM").product.runtime
    assert (rt["mode"], rt["phase"], rt["hosted_since"]) == ("full", None, "")


@pytest.mark.parametrize("extra, problem", [("  mode: tiny", "runtime.mode is full or lite"),
                                            ("  mode: lite\n  phase: half", "runtime.phase is hosted or live")])
def test_the_manifest_refuses_an_unknown_mode_or_phase(tmp_path, gov_root, extra, problem):
    farm_runtime(gov_root, extra)
    with pytest.raises(GovernanceError, match=problem):
        build_services(settings(tmp_path, gov_root)).snapshot("FARM")


# ------------------------------------------------------------------ G6 waits for go-live

def test_g6_waits_for_go_live_then_asks_as_usual(tmp_path, gov_root):
    farm_runtime(gov_root, "  mode: lite\n  phase: hosted")
    svc = build_services(settings(tmp_path, gov_root))
    eng = FeatureEngine(svc, InMemorySaver())
    fid = eng.create(product_id="FARM", title="Offline crop log")
    v = eng.auto_drive(fid)
    [w] = [p for p in v["pending"] if p["type"] == "external"]
    assert w["wait"] == "go_live" and "Waiting for FARM to go live" in w["label"]
    assert not any(d.get("env") == "production" for d in (v.get("deployments") or {}).values())
    assert eng.waits.check(w) is None  # still hosted

    path = gov_root / "acme-farm" / "farm-governance" / "product.yaml"
    path.write_text(path.read_text().replace("  phase: hosted", "  phase: live"))
    assert eng.waits.check(w)["status"] == "success"
    eng._execute(fid, eng._ready_wait(fid))
    assert any(p.get("gate") == "g6" for p in eng.pending(fid))  # a person approves production as usual

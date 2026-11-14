"""Lite mode, part 4: go-live moves a hosted lite product to the customer's VM."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import yaml

from valxel_sdlc.config import load_settings
from valxel_sdlc.governance.errors import GovernanceError
from valxel_sdlc.lite.docker import StubDocker
from valxel_sdlc.lite.golive import _manifest_text, go_live, retire_hosted
from valxel_sdlc.lite.wiring import RuntimeRouter
from valxel_sdlc.services import build_services

ROOT = Path(__file__).resolve().parents[1]
PIPELINE = "  pipeline: [dev, staging, production]"
API = "acme-farm/farm-field-api"
WORKLOAD = {"endpoints": [{"name": "api", "port": 8080, "visibility": ["project"]}],
            "configurations": {"env": [{"name": "DB_PASSWORD",
                                        "valueFrom": {"secretKeyRef": {"name": "farm-db", "key": "password"}}}]}}


@pytest.fixture
def gov_root(tmp_path):
    root = tmp_path / "governance"
    shutil.copytree(ROOT / "examples" / "governance", root)
    path = root / "acme-farm" / "farm-governance" / "product.yaml"
    path.write_text(path.read_text().replace(PIPELINE, PIPELINE + "\n  mode: lite\n  phase: hosted"))
    return root


@pytest.fixture
def hosted_farm(tmp_path, gov_root, monkeypatch):
    """FARM hosted while built, with farm-field-api running in hosted staging."""
    monkeypatch.setenv("VALXEL_LITE_STATE_DIR", str(tmp_path / "lite"))
    s = load_settings(mode="stub", database_url="", artifact_dir=tmp_path / "a",
                      governance_source="local", governance_root=gov_root)
    svc = build_services(s)
    svc.openchoreo.deployments[API] = "staging"  # dev and staging ran it
    svc.github.files[(API, "main", "workload.yaml")] = yaml.safe_dump(WORKLOAD)
    gov = svc.snapshot("FARM").pins["product"]
    manifest = gov_root / "acme-farm" / "farm-governance" / "product.yaml"
    svc.github.files[(f"{gov['org']}/{gov['repo']}", "main", "product.yaml")] = manifest.read_text()
    (tmp_path / "lite").mkdir()
    (tmp_path / "lite" / "farm-secrets.yaml").write_text(yaml.safe_dump({"*": {"farm-db": {"password": "pw"}}}))
    hosts: dict[str, StubDocker] = {}
    return svc, hosts, (lambda h: hosts.setdefault(h, StubDocker())), manifest


VMS = {"non_production": "ssh://valxel@np", "production": "ssh://valxel@prod"}


def test_go_live_moves_staging_releases_to_the_vms(hosted_farm):
    svc, hosts, docker_for, manifest = hosted_farm
    out = go_live(svc, product_id="FARM", approver="sunil", vms=VMS, docker_for=docker_for, today="2026-11-14")
    assert out["ok"], out["steps"]
    assert [s["step"] for s in out["steps"]] == ["releases", "secrets", "vm", "deploy", "manifest_pr"]
    staging = next(b for b in svc.openchoreo.component_bindings(component=API, project="farm") if b["env"] == "staging")
    hosted_image = svc.openchoreo.release_image(component=API, release=staging["release"])["image"]
    np, prod = hosts["ssh://valxel@np"], hosts["ssh://valxel@prod"]
    assert set(np.applied) == {"farm-dev", "farm-staging"} and set(prod.applied) == {"farm-production"}
    for host, project in ((np, "farm-dev"), (np, "farm-staging"), (prod, "farm-production")):
        svc_spec = host.applied[project]["services"]["farm-field-api"]
        assert svc_spec["image"] == hosted_image  # nothing rebuilt
        assert svc_spec["environment"]["DB_PASSWORD"] == "pw"
    deploy = next(s for s in out["steps"] if s["step"] == "deploy")
    assert set(deploy["states"].values()) == {"ready"}

    # The governance pull request makes it live; once merged, deployments go through the VM.
    [pr] = svc.github.prs.values()
    assert pr["title"] == "FARM goes live"
    gov = svc.snapshot("FARM").pins["product"]
    text = svc.github.files[(f"{gov['org']}/{gov['repo']}", "valxel/go-live-farm", "product.yaml")]
    manifest.write_text(text)
    rt = svc.snapshot("FARM").product.runtime
    assert (rt["phase"], rt["vms"], rt["went_live"]) == ("live", VMS, "2026-11-14")
    router = RuntimeRouter(svc.openchoreo, svc, docker_for=docker_for)
    assert router.deployment(component=API, env="production", project="farm")["state"] == "ready"
    assert router.runtime_for("farm") is not None and router.runtime_for("pay") is None

    removed = retire_hosted(svc, product_id="FARM")["removed"]
    assert f"{API}@dev" in removed and f"{API}@staging" in removed


def test_go_live_checks_every_secret_before_deploying(hosted_farm, tmp_path):
    svc, hosts, docker_for, _ = hosted_farm
    (tmp_path / "lite" / "farm-secrets.yaml").write_text(yaml.safe_dump({"dev": {"farm-db": {"password": "pw"}}}))
    out = go_live(svc, product_id="FARM", approver="sunil", vms=VMS, docker_for=docker_for)
    assert not out["ok"]
    failed = out["steps"][-1]
    assert failed["step"] == "secrets" and "production: farm-db/password" in failed["error"]
    assert "staging: farm-db/password" in failed["error"] and "dev:" not in failed["error"]
    assert all(not h.applied for h in hosts.values()) and not svc.github.prs


def test_go_live_needs_something_in_staging(hosted_farm):
    svc, _, docker_for, _ = hosted_farm
    svc.openchoreo.deployments.clear()
    out = go_live(svc, product_id="FARM", approver="sunil", vms=VMS, docker_for=docker_for)
    assert not out["ok"] and "deploy a feature there first" in out["steps"][-1]["error"]


def test_only_a_person_takes_a_hosted_lite_product_live(hosted_farm):
    svc, _, docker_for, _ = hosted_farm
    out = go_live(svc, product_id="FARM", approver="agent:release", vms=VMS, docker_for=docker_for)
    assert not out["ok"] and "VX-3" in out["steps"][0]["error"]
    out = go_live(svc, product_id="PAY", approver="sunil", vms=VMS, docker_for=docker_for)
    assert not out["ok"] and "isn't a lite product hosted" in out["steps"][0]["error"]
    out = go_live(svc, product_id="FARM", approver="sunil", vms={}, docker_for=docker_for)
    assert not out["ok"] and "vms.non_production" in out["steps"][0]["error"]


def test_one_vm_runs_production_too(hosted_farm):
    svc, hosts, docker_for, _ = hosted_farm
    out = go_live(svc, product_id="FARM", approver="sunil", vms={"non_production": "ssh://valxel@one"},
                  docker_for=docker_for)
    assert out["ok"] and set(hosts) == {"ssh://valxel@one"}
    assert set(hosts["ssh://valxel@one"].applied) == {"farm-dev", "farm-staging", "farm-production"}


def test_retire_waits_until_live(hosted_farm):
    svc, *_ = hosted_farm
    with pytest.raises(ValueError, match="isn't live yet"):
        retire_hosted(svc, product_id="FARM")


def test_the_manifest_edit_keeps_the_file_and_validates(hosted_farm):
    svc, _, _, manifest = hosted_farm
    before = manifest.read_text()
    after = _manifest_text(before, VMS, "2026-11-14")
    assert "# Runtime infrastructure stays with the product" in after and "phase: hosted" not in after
    assert _manifest_text(after, {"non_production": "ssh://valxel@new"}, "2026-11-15").count("phase: live") == 1
    assert "production: ssh://valxel@prod" not in _manifest_text(after, {"non_production": "ssh://valxel@new"}, "x")
    manifest.write_text(before.replace("phase: hosted", "phase: live"))  # live without VMs
    with pytest.raises(GovernanceError, match="runtime.vms.non_production"):
        svc.snapshot("FARM")


def test_services_route_through_the_lite_router_when_asked(tmp_path, gov_root, monkeypatch):
    monkeypatch.setenv("VALXEL_LITE_ROUTING", "on")
    s = load_settings(mode="stub", database_url="", artifact_dir=tmp_path / "a",
                      governance_source="local", governance_root=gov_root)
    svc = build_services(s)
    assert isinstance(svc.openchoreo, RuntimeRouter)
    rec = svc.openchoreo.promote(component=API, env="dev", actor="agent:a", actor_type="agent", project="farm")
    assert rec["env"] == "dev" and svc.openchoreo.base.deployments[API] == "dev"  # hosted: OpenChoreo
    monkeypatch.delenv("VALXEL_LITE_ROUTING")
    assert not isinstance(build_services(s).openchoreo, RuntimeRouter)

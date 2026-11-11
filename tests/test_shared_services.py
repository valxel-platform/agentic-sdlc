"""The shared-services lane: shared libraries and frameworks with no runtime of their own,
consumed by products that run them in their own runtimes."""

import shutil
from pathlib import Path

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from vrixel_sdlc.config import load_settings
from vrixel_sdlc.engine import FeatureEngine
from vrixel_sdlc.governance import GovernanceError
from vrixel_sdlc.services import build_services

EXAMPLES = Path(__file__).resolve().parents[1] / "examples" / "governance"


@pytest.fixture
def gov_root(tmp_path):
    root = tmp_path / "governance"
    shutil.copytree(EXAMPLES, root)
    return root


@pytest.fixture
def services(tmp_path, gov_root):
    s = load_settings(mode="stub", database_url="", artifact_dir=tmp_path / "artifacts",
                      governance_source="local", governance_root=gov_root)
    return build_services(s)


@pytest.fixture
def engine(services):
    return FeatureEngine(services, InMemorySaver())


def edit(path: Path, old: str, new: str) -> None:
    text = path.read_text()
    assert old in text
    path.write_text(text.replace(old, new))


def test_shared_lane_is_governed_from_its_own_org_without_a_runtime(services):
    snap = services.snapshot("SS")
    assert snap.product.kind == "shared" and snap.product.org == "acme-shared"
    assert snap.product.runtime is None
    assert {r.role for r in snap.product.repos} == {"library"}
    assert "SS-6" in snap.constitution.article_ids


def test_a_shared_lane_may_not_declare_a_runtime(services, gov_root):
    edit(gov_root / "acme-shared/ss-governance/product.yaml", "max_loops: 3\n",
         "max_loops: 3\nruntime:\n  openchoreo_project: shared\n")
    with pytest.raises(GovernanceError, match="a shared lane owns no runtime"):
        services.snapshot("SS")


def test_a_product_must_own_its_runtime(services, gov_root):
    edit(gov_root / "acme-shop/shop-governance/product.yaml", "  openchoreo_project: shop\n", "")
    with pytest.raises(GovernanceError, match="runtime.openchoreo_project is required"):
        services.snapshot("SHOP")


def test_unknown_or_upward_dependencies_are_rejected(services, gov_root):
    edit(gov_root / "acme-farm/farm-governance/product.yaml", "depends_on: [SS, PAY]", "depends_on: [SS, NOPE]")
    with pytest.raises(GovernanceError, match="'NOPE' is not a registered lane"):
        services.snapshot("FARM")
    edit(gov_root / "acme-shared/ss-governance/product.yaml", "max_loops: 3\n",
         "max_loops: 3\ndepends_on: [CLN]\n")
    with pytest.raises(GovernanceError, match="shared lane may not depend on product CLN"):
        services.snapshot("SS")


def test_feature_without_shared_changes_needs_only_its_own_approvals(engine):
    v = engine.auto_drive(engine.start(product_id="FARM", title="Yield chart")["feature_id"])
    assert v["status"] == "done" and v["lanes_touched"] == []
    assert {g["lane"] for g in v["gate_decisions"]} == {"FARM"}


def test_product_feature_that_changes_a_shared_framework(engine, services):
    fid = engine.start(product_id="FARM", title="Audit trail for field edits",
                       description="Record who changed field data using the shared audit framework")["feature_id"]
    v = engine.auto_drive(fid)
    assert v["status"] == "done"
    assert v["lanes_touched"] == ["SS"]

    # Shared code serves PHI products, so any change set touching it is regulated and goes through G5.
    assert v["risk_tier"] == "regulated"
    lanes_by_gate = {}
    for g in v["gate_decisions"]:
        lanes_by_gate.setdefault(g["gate"], []).append(g["lane"])
    assert lanes_by_gate["g1"] == ["FARM"] and lanes_by_gate["g2"] == ["FARM"] and lanes_by_gate["g3"] == ["FARM"]
    for gate in ("g4", "g5", "g6"):
        assert lanes_by_gate[gate] == ["FARM", "SS"], f"{gate} needs both owners"

    # The shared library merges first and publishes; the product's services deploy in its runtime.
    order = v["change_set"]["merge_order"]
    assert order[0] == "acme-shared/ss-audit-framework"
    assert v["change_set"]["published"] == ["acme-shared/ss-audit-framework"]
    # Tagged as a release candidate at the merge, then the same commit as the stable version at G6.
    rc = v["change_set"]["packages"]["acme-shared/ss-audit-framework"]
    assert (rc["version"], rc["tag"], rc["channel"]) == ("0.1.0", "v0.1.0-rc.1", "rc")
    assert services.packages.channel("acme-shared/ss-audit-framework", "0.1.0") == "stable"
    assert services.packages.tags["acme-shared/ss-audit-framework"] == {"v0.1.0-rc.1": rc["sha"], "v0.1.0": rc["sha"]}
    assert v["release"]["versions"] == {"acme-shared/ss-audit-framework": "0.1.0"}
    oc = services.openchoreo
    assert "acme-shared/ss-audit-framework" not in oc.deployments
    assert {r["project"] for r in oc.runs if r["kind"] == "main"} == {"farm"}
    assert {p["component"] for p in oc.promotions if p["env"] == "production"} == {
        c for c in order if c.startswith("acme-farm/")}

    # The shared constitution governs the change: it is part of the pinned version.
    assert "+shared-services@1.0.0#" in v["constitution_version"]


def test_g5_shows_each_lane_only_its_own_pull_requests(engine):
    fid = engine.start(product_id="FARM", title="Security headers",
                       description="Adopt the shared security library for request signing")["feature_id"]
    seen = {}
    for _ in range(20):
        pending = engine.pending(fid)
        if not pending:
            break
        p = pending[0]
        if p.get("gate") == "g5":
            seen[p["lane"]] = p["pull_requests"]
        engine.decide(fid, p["interrupt_id"], {"decision": "approve", "approver": f"{p.get('lane')}-owner"})
    assert set(seen) == {"FARM", "SS"}
    assert all(k.startswith("acme-farm/") for k in seen["FARM"])
    assert seen["SS"] and all(k.startswith("acme-shared/") for k in seen["SS"])


def test_shared_owner_can_send_the_architecture_back(engine):
    fid = engine.start(product_id="SHOP", title="Receipt notification",
                       description="Send receipts through the shared communication service")["feature_id"]
    for gate in ("g1", "g2"):
        [p] = [p for p in engine.pending(fid) if p.get("gate") == gate]
        engine.decide(fid, p["interrupt_id"], {"decision": "approve", "approver": "po"})
    [g4_pos] = [p for p in engine.pending(fid) if p.get("gate") == "g4"]
    assert g4_pos["lane"] == "SHOP" and g4_pos["lanes"] == ["SHOP", "SS"]
    engine.decide(fid, g4_pos["interrupt_id"], {"decision": "approve", "approver": "shop-architect"})
    [g4_ss] = [p for p in engine.pending(fid) if p.get("gate") == "g4"]
    assert g4_ss["lane"] == "SS" and g4_ss["approvers"] == ["acme-shared/architects"]
    v = engine.decide(fid, g4_ss["interrupt_id"], {"decision": "revise", "approver": "ss-architect",
                                                    "feedback": "Use the existing template API"})
    assert any(g["gate"] == "g4" and g["lane"] == "SS" and g["decision"] == "revise" for g in v["gate_decisions"])
    assert [(p["gate"], p["lane"]) for p in v["pending"]] == [("g3", "SHOP")]  # G3 and G4 resolve as a pair
    [g3] = v["pending"]
    v = engine.decide(fid, g3["interrupt_id"], {"decision": "approve", "approver": "shop-design-lead"})
    assert v["artifacts"]["HLD"].endswith(".HLD.v2")  # architecture re-ran with the shared owner's feedback
    assert [(p["gate"], p["lane"]) for p in v["pending"]] == [("g4", "SHOP")]  # both owners decide again


def test_shared_lane_feature_publishes_a_stable_package_and_deploys_nothing(engine, services):
    fid = engine.start(product_id="SS", title="Audit export",
                       description="Export audit events to a customer SIEM")["feature_id"]
    v = engine.auto_drive(fid)
    assert v["status"] == "done"
    assert v["release"]["components"] == [] and v["release"]["production"] is False
    assert set(v["release"]["stable"]) == {r for r in v["change_set"]["merge_order"]}
    assert services.openchoreo.deployments == {}
    assert services.openchoreo.promotions == []


def test_shared_amendment_mid_feature_does_not_change_the_pinned_version(engine, gov_root):
    first = engine.start(product_id="CLN", title="Consent audit", description="Audit consent changes")
    con = gov_root / "acme-shared/ss-governance/constitution.md"
    edit(con, "version: 1.0.0", "version: 2.0.0")
    v = engine.auto_drive(first["feature_id"])
    assert "shared-services@1.0.0" in v["constitution_version"]
    second = engine.start(product_id="CLN", title="Consent audit v2",
                          not_duplicate_reason="A second round after the shared lane amended its constitution")
    assert "shared-services@2.0.0" in second["constitution_version"]

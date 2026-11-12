"""The inception thread: the product council's brief gate and product gate, and the setup that
records an approved product as pull requests."""

from __future__ import annotations

import pytest
import yaml
from fastapi.testclient import TestClient

from test_api import SECRET, sign_in, wait_idle
from vrixel_sdlc.api.app import create_app
from vrixel_sdlc.governance.manifest import parse_manifest
from vrixel_sdlc.inception import setup
from vrixel_sdlc.web.auth import AuthConfig

COUNCIL = ["vrixel-tools/product-council"]
BODY = {"name": "Aqua Watch", "org": "acme-water",
        "vision": "Small farms lose crops because nobody sees soil moisture until too late. Alert the manager's phone.",
        "goals": ["Fewer dry-field incidents (measure: under 2 a month per farm)",
                  "Less water used (measure: 15% less per hectare)"],
        "users": ["Farm managers: out in the fields on a phone"], "budget_usd": 1500}


@pytest.fixture
def client(engine):
    app = create_app(engine, auth=AuthConfig(mode="dev", secret=SECRET), web_dist=None)
    with TestClient(app) as c:
        yield c


def decide(client, engine, tid, decision, **extra):
    v = wait_idle(engine, tid)
    [item] = [p for p in v["pending"] if p["type"] == "gate"]
    r = client.post(f"/api/features/{tid}/decisions/{item['interrupt_id']}", json={"decision": decision, **extra})
    assert r.status_code == 202, r.text
    return item


def test_only_starters_begin_and_only_the_council_decides(client, engine):
    sign_in(client, "priya", orgs=["acme-farm"])
    assert client.post("/api/inceptions", json=BODY).status_code == 403
    sign_in(client, "lead", teams=COUNCIL)
    tid = client.post("/api/inceptions", json=BODY).json()["feature_id"]
    assert tid == "INCEPTION-PRD-1"
    v = wait_idle(engine, tid)
    [gate] = v["pending"]
    assert gate["gate"] == "brief" and gate["approvers"] == COUNCIL and "# Product brief: Aqua Watch" in gate["preview"]
    assert v["kind"] == "inception" and v["status"] == "brief_gate" and v["architecture"] == ""
    sign_in(client, "priya", orgs=["acme-farm"])
    r = client.post(f"/api/features/{tid}/decisions/{gate['interrupt_id']}", json={"decision": "approve"})
    assert r.status_code == 403
    assert [i["id"] for i in client.get("/api/inceptions").json()] == [tid]


def test_two_gates_then_setup_opens_the_pull_requests(client, engine, services, monkeypatch):
    monkeypatch.setenv("VRIXEL_CONFIG_REPO", "vrixel-tools/sdlc-config")
    sign_in(client, "lead", teams=COUNCIL)
    tid = client.post("/api/inceptions", json=BODY).json()["feature_id"]
    decide(client, engine, tid, "revise", feedback="Name agronomists as users too")
    v = wait_idle(engine, tid)
    assert any(e["node"] == "brief_gate" and e["kind"] == "revise" for e in v["events"])
    assert v["status"] == "brief_gate"
    decide(client, engine, tid, "approve")
    item = decide(client, engine, tid, "approve")
    assert item["gate"] == "product" and item["monthly_usd"] == {"dev": 5.0, "staging": 5.0, "production": 15.0}
    assert item["capacity"]["tier"] == 3 and item["capacity"]["stages"]["pilot"]["monthly_usd"] > 0
    assert "# Product architecture" in item["preview"] and "# Roadmap" in item["preview"]
    v = wait_idle_status(engine, tid, "set_up")
    s = v["setup"]
    assert s["ok"] and s["product_id"] == "AW" and s["governance_repo"] == "acme-water/aw-governance"
    gh = services.github
    files = {path for (repo, branch, path) in gh.files if repo == "acme-water/aw-governance"}
    assert {"product.yaml", "constitution.md", "roadmap/goals.yaml", "roadmap/initiatives/AW-INI-1.md",
            "architecture/product-architecture.md", "runtime/capacity.yaml"} <= files
    registry = gh.files[("vrixel-tools/sdlc-config", "vrixel/register-aw", "config/registry.yaml")]
    assert "AW: { kind: product, org: acme-water, repo: aw-governance" in registry
    assert [st["step"] for st in s["steps"]] == ["governance_repo", "governance_pr", "registry_pr", "clusters_pr",
                                                 "environments", "teams"]
    # The clusters wait for the platform's infrastructure repo; the environments for the data planes.
    by = {st["step"]: st for st in s["steps"]}
    assert by["clusters_pr"]["skipped"] and by["environments"]["waiting"] == ["aw-nonprod", "aw-prod"]
    import yaml

    manifest = yaml.safe_load(gh.files[("acme-water/aw-governance", "vrixel/inception", "product.yaml")])
    assert manifest["runtime"]["stage"] == "pilot" and manifest["runtime"]["data_planes"]["production"] == "aw-prod"


def wait_idle_status(engine, tid, status, timeout=10.0):
    import time

    end = time.time() + timeout
    while time.time() < end:
        v = engine.view(tid)
        if tid not in engine.running and v["status"] == status:
            return v
        time.sleep(0.02)
    raise TimeoutError(f"{tid} never reached {status}")


def test_dropping_at_the_brief_gate_writes_nothing(client, engine, services):
    sign_in(client, "lead", teams=COUNCIL)
    tid = client.post("/api/inceptions", json=BODY).json()["feature_id"]
    decide(client, engine, tid, "abort", feedback="Not this year")
    v = wait_idle_status(engine, tid, "dropped")
    assert v["architecture"] == "" and not v["setup"]
    assert not [k for k in services.github.files if k[0] == "acme-water/aw-governance"]


def test_an_existing_product_gets_one_pull_request(client, engine, services):
    sign_in(client, "lead", teams=COUNCIL, orgs=["acme-farm"])
    tid = client.post("/api/inceptions", json={**BODY, "name": "Farm app", "org": "", "product_id": "FARM"}).json()["feature_id"]
    decide(client, engine, tid, "approve")
    decide(client, engine, tid, "approve")
    s = wait_idle_status(engine, tid, "set_up")["setup"]
    assert [st["step"] for st in s["steps"]] == ["governance_pr"] and s["governance_repo"] == "acme-farm/farm-governance"
    files = {path for (repo, branch, path) in services.github.files if repo == "acme-farm/farm-governance"
             and branch == setup.BRANCH}
    assert "architecture/product-architecture.md" in files and "product.yaml" not in files


def test_the_records_setup_writes_load_as_a_product(services):
    from vrixel_sdlc.config import RegistryEntry, RepoPointer
    from vrixel_sdlc.inception import Vision
    from vrixel_sdlc.inception.run import run

    v = Vision(name="Aqua Watch", org="acme-water", vision="Alert farm managers when fields run dry.",
               goals=["Fewer dry fields (measure: under 2 a month)"], users=["Farm managers"], budget_usd=1500)
    [b] = run(services, v)
    a, r = run(services, v, brief=b.text)
    files = setup.governance_files(v, b.text, a.text, r.text, "lead")
    s = services.settings
    entry = RegistryEntry(product_id="AW", governance=RepoPointer(org="acme-water", repo="aw-governance", ref="main"),
                          data_class="general", workspace="wrkspc-aw", kind="product")
    product, _ = parse_manifest(files["product.yaml"], entry, s.limits, {**s.registry, "AW": entry}, s.tools,
                                s.quality, s.repos)
    assert product.repo_prefix == "aw-" and product.approvers["g4"] == ["acme-water/architects"]
    assert yaml.safe_load(files["roadmap/goals.yaml"])["goals"][0]["id"] == "GOAL-AW-1"
    assert "goal: GOAL-AW-1" in files["roadmap/initiatives/AW-INI-1.md"]
    text = "products:\n  FARM: { kind: product }\n\nother: 1\n"
    once = setup.add_to_registry(text, setup.registry_line(b.text))
    assert once.index("  AW:") < once.index("other: 1") and setup.add_to_registry(once, setup.registry_line(b.text)) == once

"""Capacity plans: from a product's volumes and load to replicas, databases, partitions and cost;
its environments in OpenChoreo; the capacity each deployment runs with; and what happens after launch."""

from __future__ import annotations

from pathlib import Path

import pytest

from valxel_sdlc import capacity, environments
from valxel_sdlc.inception import checks, formats
from valxel_sdlc.integrations.openchoreo import StubOpenChoreo
from valxel_sdlc.resources import load_catalog, parse_resources

PLATFORM = Path(__file__).resolve().parents[1] / "examples/governance/valxel-platform/sdlc-governance/platform"
CATALOG = load_catalog((PLATFORM / "aws-resources.yaml").read_text())
CFG = capacity.load_platform((PLATFORM / "capacity.yaml").read_text())

BRIEF = """## Scale
- launch: 300 clinics (A2)
- year one: 2000 clinics (A3)
- busy hour: 3 times the daily average, over a 10-hour day
- where: United States
## Availability
- tier: 2: front desks can't check pets in while it is down
## Data class
- phi: it stores pets' medical records and their owners' details
## Product
- id: CLN
- run budget: pilot 400; growth 1000; scale 3000
"""
ARCH = """## Components
- intake-api (provider): keeps visits [repo: acme/cln-intake-api]
- bff (bff): the web app's way in [repo: acme/cln-bff]
- worker (event-worker): applies recorded changes [repo: acme/cln-worker]
## Resources
- records: postgres-crossplane; database=cln
    writes: intake-api, worker; reads: intake-api
## Load
- J1: Check a pet in (clinic): 60 a day per clinic; bff 20 requests; intake-api 20 requests; worker 4 events
## Scaling
- intake-api: stateless; scales on cpu; first limit: database connections at about 80 pods
- bff: stateless; scales on cpu; first limit: the API behind it, which scales first
- worker: stateless; scales on cpu; first limit: sixteen partitions, so sixteen consumers at most
## Failure
- bff -> intake-api: timeout 2000 ms; retries 1; idempotency: visit id; when down: the desk sees a retry banner
- worker -> records: timeout 1000 ms; retries 3; idempotency: event id; when down: events wait on Kafka
- records: recovery: Multi-AZ failover and point-in-time recovery
"""


def plan(brief=BRIEF, arch=ARCH):
    return capacity.plan(brief, arch, CFG, CATALOG, data_class="phi")


def test_the_worked_example_reaches_the_plans_numbers():
    p = plan()
    assert p["findings"] == []
    rows = {r["component"]: r for r in p["components"]}
    api = rows["intake-api"]
    assert api["busy"] == {"launch": 30.0, "year_one": 200.0, "top": 2000.0}
    assert api["production"]["growth"]["min"] == 2 and api["production"]["growth"]["max"] == 34
    assert rows["bff"]["production"]["growth"]["max"] == 17 and rows["worker"]["production"]["growth"]["max"] == 14
    # The pilot: one small pod each, a ceiling at twice the launch busy hour, the small database.
    assert {r["production"]["pilot"]["min"] for r in rows.values()} == {1}
    assert api["production"]["pilot"]["max"] == 2
    db = p["databases"][0]
    assert db["connections"]["growth"] == 410
    assert db["sizes"] == {"pilot": "small", "growth": "medium-ha", "scale": "medium-ha", "dev": "paused",
                           "staging": "paused"}
    assert p["partitions"] == 16
    assert p["stages"]["pilot"]["monthly_usd"] == 45 and p["stages"]["growth"]["monthly_usd"] == 400
    assert p["stages"]["scale"]["monthly_usd_max"] == 1765
    assert p["fixed"]["monthly_usd"] == 146 and "NAT gateways" in p["fixed"]["unpriced"]
    assert p["stages"]["pilot"]["availability"]["availability_pct"] == 99.5  # tier 3 in the pilot
    assert "| intake-api (provider) | 30 / 200 requests/s | 1–2 × 0.5 CPU | 2–34 × 1 CPU | 2 | 1 |" in capacity.render(p)


def test_what_doesnt_add_up_is_said_plainly():
    arch = ARCH.replace("; worker 4 events", "").replace("busy hour", "busy hour")
    p = plan(arch=arch)
    assert any("worker: no journey in '## Load' sends it events" in f for f in p["findings"])
    # Over the run budget: the pilot with the clusters is $191, over $150.
    p = plan(brief=BRIEF.replace("pilot 400", "pilot 150"))
    assert any("pilot stage costs about $191" in f for f in p["findings"])
    # A pool too large for any Multi-AZ size.
    cfg = {**CFG, "runtimes": {**CFG["runtimes"], "provider": {**CFG["runtimes"]["provider"], "pool": 60}}}
    p = capacity.plan(BRIEF, ARCH, cfg, CATALOG, data_class="phi")
    assert any("no Multi-AZ size of postgres-crossplane takes" in f for f in p["findings"])
    # PHI at tier 3.
    p = plan(brief=BRIEF.replace("tier: 2", "tier: 3"))
    assert any("phi products target tier 2" in f for f in p["findings"])


def test_the_brief_and_architecture_checks():
    found = checks._scale(BRIEF.replace("- busy hour: 3 times the daily average, over a 10-hour day\n", ""))
    assert any("busy hour" in f for f in found)
    assert any("run budget" in f for f in checks._scale(BRIEF.replace("- run budget: pilot 400; growth 1000; scale 3000\n", "")))
    comps = formats.components(ARCH)
    res = [r for r in parse_resources(ARCH) if r.get("type")]
    assert capacity.check_doc(ARCH, comps, res) == []
    worse = (ARCH.replace("- bff: stateless; scales on cpu; first limit: the API behind it, which scales first\n", "")
             .replace("timeout 2000 ms; retries 1; idempotency: visit id; ", "")
             .replace("- records: recovery: Multi-AZ failover and point-in-time recovery\n", "")
             .replace("worker: stateless; scales on cpu", "worker: stateless; scales on lag"))
    found = capacity.check_doc(worse, comps, res)
    assert "bff: say how it scales under '## Scaling'" in found
    assert "bff -> intake-api: give the timeout in ms" in found
    assert any(f.startswith("records: say under '## Failure' how it recovers") for f in found)
    assert any("scale on cpu" in f for f in found)


def test_the_approved_plan_becomes_each_bindings_capacity():
    doc = capacity.load_product(capacity.capacity_yaml(plan(), "CLN"))
    assert set(doc["components"]) == {"cln-intake-api", "cln-bff", "cln-worker"}
    configs, traits = capacity.binding_configs(doc, "acme/cln-intake-api", "production")
    assert configs == {"replicas": 1, "resources": {"requests": {"cpu": "500m", "memory": "512Mi"},
                                                    "limits": {"memory": "512Mi"}}}
    assert traits == {"autoscaler": {"minReplicas": 1, "maxReplicas": 2, "targetCPUUtilizationPercentage": 60}}
    configs, traits = capacity.binding_configs(capacity.with_stage(doc, "growth"), "cln-intake-api", "production")
    assert configs["replicas"] == 2 and configs["resources"]["requests"]["cpu"] == "1000m" and traits["autoscaler"]["maxReplicas"] == 34
    configs, traits = capacity.binding_configs(doc, "cln-intake-api", "staging")
    assert configs["replicas"] == 2 and traits == {}  # staging is a fixed size
    assert capacity.binding_configs(doc, "cln-unknown", "dev") is None


class _Snap:
    def __init__(self, cap):
        self.capacity = cap


class _Services:
    def __init__(self):
        self.openchoreo = StubOpenChoreo()


def test_a_deployment_carries_its_environments_capacity():
    s = _Services()
    doc = capacity.load_product(capacity.capacity_yaml(plan(), "CLN"))
    out = environments.apply_capacity(s, _Snap(doc), {"component": "acme/cln-intake-api", "project": "cln"}, "production")
    assert out["replicas"] == 1 and out["autoscaler"] == {"cln-intake-api-autoscaler": {
        "minReplicas": 1, "maxReplicas": 2, "targetCPUUtilizationPercentage": 60}}
    assert s.openchoreo.traits["acme/cln-intake-api"][0]["instanceName"] == "cln-intake-api-autoscaler"
    assert s.openchoreo.binding_configs[("acme/cln-intake-api", "production")]["configs"]["replicas"] == 1
    # No plan, nothing changes.
    assert environments.apply_capacity(s, _Snap({}), {"component": "acme/x"}, "dev") is None


def test_environments_wait_for_the_data_planes_and_production_is_the_approvers():
    s = _Services()
    out = environments.environments(s, product_id="CLN", approver="maria")
    assert out["waiting"] == ["cln-nonprod", "cln-prod"]
    s.openchoreo.data_planes |= {"cln-nonprod", "cln-prod"}
    out = environments.environments(s, product_id="CLN", approver="maria, li")
    assert out["created"] == {"project": "created", "pipeline": "created", "dev": "created", "staging": "created",
                              "production": "created"}
    envs = s.openchoreo.product_environments
    assert envs["production"]["data_plane"] == "cln-prod" and envs["production"]["actor"] == "human:maria"
    assert envs["dev"]["data_plane"] == envs["staging"]["data_plane"] == "cln-nonprod"
    assert s.openchoreo.pipelines["cln-pipeline"] == ["dev", "staging", "production"]
    assert environments.environments(s, product_id="CLN", approver="maria")["created"]["dev"] == "exists"
    with pytest.raises(PermissionError):
        s.openchoreo.ensure_environment(name="production", data_plane="cln-prod", production=True, actor="agent:setup")


def test_the_clusters_come_from_the_platforms_template():
    files = environments.cluster_files("CLN", 2, (PLATFORM / "openchoreo/clusters.yaml").read_text(), zones=2)
    assert sorted(files) == ["clusters/cln/cln-nonprod.yaml", "clusters/cln/cln-observe.yaml", "clusters/cln/cln-prod.yaml"]
    import yaml

    nonprod = yaml.safe_load(files["clusters/cln/cln-nonprod.yaml"])
    prod = yaml.safe_load(files["clusters/cln/cln-prod.yaml"])
    assert nonprod["spec"]["nodes"]["spot"] is True and nonprod["spec"]["awake"].startswith("Mon-Fri")
    assert prod["spec"]["nodes"]["spot"] is False and prod["spec"]["zones"] == 2 and prod["spec"]["awake"] == ""


def test_after_launch_features_measurements_and_production_move_the_plan():
    doc = capacity.load_product(capacity.capacity_yaml(plan(), "CLN"))
    hld = "## Performance\n- New traffic: yes: reminders\n- Added load: intake-api +20 requests/s; worker +10 events/s\n"
    ch = capacity.change(doc, hld)
    by = {c["repo"]: c for c in ch}
    assert by["cln-intake-api"]["max_from"] == 34 and by["cln-intake-api"]["max_to"] == 37
    assert by["cln-worker"]["max_to"] == 17
    new = capacity.apply_change(doc, ch)
    assert new["components"]["cln-intake-api"]["production"]["growth"]["max"] == 37
    # Three load tests replace the starting throughput with their median.
    for m in (80, 90, 120):
        new = capacity.calibrate(new, "cln-intake-api", m)
    assert new["components"]["cln-intake-api"]["measured_per_pod"] == 90
    # Production's review: the pilot runs hot on 6 days of the fortnight -> propose the growth stage.
    found = capacity.review(doc, {"cln-intake-api": {"busy": 40, "days_over": 6, "days_at_max": 0, "month_share": 0.8}})
    assert [(f["kind"], f.get("to")) for f in found] == [("step_up", "growth")]
    quiet = capacity.with_stage(doc, "scale")
    found = capacity.review(quiet, {"cln-intake-api": {"busy": 5, "days_over": 0, "days_at_max": 0, "month_share": 0.1}})
    assert [(f["kind"], f.get("to")) for f in found] == [("step_down", "growth")]
    assert capacity.stage_cost(doc, "pilot") == 30 and capacity.stage_cost(doc, "growth") == 120

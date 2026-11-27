"""A product's mesh rules: its security repo's `calls` become its components' allowedCallers."""

import hashlib
import hmac
import json

import yaml
from fastapi.testclient import TestClient

from valxel_sdlc import product_security as ps
from valxel_sdlc.api.app import create_app
from valxel_sdlc.repo_policy import component_name
from valxel_sdlc.web.auth import AuthConfig

SPEC = """\
product: farm
bff: {client_id: %(bff)s}
calls:
  - {from: %(bff)s, to: %(api)s, port: 8080}
  - {from: %(bff)s, to: %(api)s, port: 9090}
  - {from: %(api)s, to: ledger, port: 8080}
"""


def test_the_security_repo_sits_beside_the_governance_repo():
    assert ps.security_repo("acme-pay/pay-governance") == "acme-pay/pay-security"
    assert ps.security_repo("acme-pay/governance") == "acme-pay/security"


def test_calls_become_each_components_allowed_callers():
    spec = {"bff": {"client_id": "bff"},
            "calls": [{"from": "bff", "to": "api", "port": 9090}, {"from": "bff", "to": "api", "port": 8080},
                      {"from": "worker", "to": "api", "port": 8080}, {"from": "api", "to": "nope", "port": 1}]}
    params, problems = ps.mesh_parameters(spec, ["api", "bff", "worker"])
    assert params["api"] == {"allowedCallers": [{"component": "bff", "ports": [8080, 9090]},
                                                {"component": "worker", "ports": [8080]}], "fromIngress": False}
    assert params["bff"] == {"allowedCallers": [], "fromIngress": True}  # only the BFF is reachable from outside
    assert params["worker"] == {"allowedCallers": [], "fromIngress": False}  # every component gets a value: resets
    assert problems == ["call api -> nope:1: nope isn't one of the product's components"]


def _farm(services):
    product = services.snapshot("FARM").product
    names = sorted({component_name(product, r.name) for r in product.repos})
    return product, names


def _register(services, names):
    for n in names:
        services.openchoreo.register_component(component=n, project="farm", namespace="default", component_type="x",
                                               workflow="w", repo_url="", branch="main")


def test_apply_sets_the_parameters_and_keeps_the_others(services):
    _, names = _farm(services)
    bff, api = names[0], names[1]
    _register(services, names)
    services.openchoreo.components[api]["parameters"] = {"dataClass": "general"}
    services.github.files[("acme-farm/farm-security", "main", "security.yaml")] = SPEC % {"bff": bff, "api": api}
    out = ps.apply(services, "FARM")
    assert out["repo"] == "acme-farm/farm-security" and out["applied"][api] == "updated"
    assert out["problems"] == [f"call {api} -> ledger:8080: ledger isn't one of the product's components"]
    p = services.openchoreo.components[api]["parameters"]
    assert p["dataClass"] == "general" and p["allowedCallers"] == [{"component": bff, "ports": [8080, 9090]}]
    assert services.openchoreo.components[bff]["parameters"]["fromIngress"] is True
    assert ps.apply(services, "FARM")["applied"][api] == "unchanged"


def test_a_component_not_yet_registered_is_skipped_not_fatal(services):
    _, names = _farm(services)
    services.github.files[("acme-farm/farm-security", "main", "security.yaml")] = "calls: []\n"
    out = ps.apply(services, "FARM")
    assert all(v.startswith("skipped") for v in out["applied"].values()) and set(out["applied"]) == set(names)


def test_a_merge_to_the_security_repo_applies_its_rules(monkeypatch, engine, services):
    monkeypatch.setenv("VALXEL_GITHUB_WEBHOOK_SECRET", "s3cret")
    client = TestClient(create_app(engine=engine, auth=AuthConfig(mode="dev", secret="x" * 32), web_dist=None))
    _, names = _farm(services)
    _register(services, names)
    services.github.files[("acme-farm/farm-security", "main", "security.yaml")] = SPEC % {"bff": names[0],
                                                                                         "api": names[1]}

    def push(repo, ref="refs/heads/main"):
        body = json.dumps({"ref": ref, "repository": {"full_name": repo}}).encode()
        sig = "sha256=" + hmac.new(b"s3cret", body, hashlib.sha256).hexdigest()
        return client.post("/api/webhooks/github", content=body,
                           headers={"x-hub-signature-256": sig, "x-github-event": "push"}).json()

    assert "mesh" not in push("acme-farm/farm-security", ref="refs/heads/topic")  # only merges to main
    assert "mesh" not in push("acme-farm/farm-field-api")  # not a security repo
    assert push("acme-farm/farm-security")["mesh"]["applied"][names[1]] == "updated"


def test_the_product_gate_creates_only_the_security_repo_it_approved(engine):
    from valxel_sdlc.provision_queue import check

    class Engine:
        services = engine.services

        def _state(self, _):
            return {"brief": "", "gate_decisions": [{"gate": "product", "decision": "approve"}]}

    from valxel_sdlc.inception import formats

    real = formats.ids
    formats.ids = lambda _: {"product_id": "PAY", "governance_repo": "acme-pay/pay-governance",
                             "security_repo": "acme-pay/pay-security"}
    try:
        ok = {"kind": "security_repo", "payload": {"product_id": "PAY", "repo": "acme-pay/pay-security",
                                                   "inception_id": "INC-1"}}
        assert check(Engine(), ok) == ""
        bad = {**ok, "payload": {**ok["payload"], "repo": "acme-pay/other"}}
        assert "isn't the security repo INC-1 approved" in check(Engine(), bad)
    finally:
        formats.ids = real


def test_new_calls_are_appended_and_nothing_else_changes():
    text = "# who may call whom\nproduct: farm\ncalls:\n  - {from: a, to: b, port: 8080}\n\nbff: {client_id: a}\n"
    new = ps.missing_calls(yaml.safe_load(text), [("a", "b"), ("a", "c"), ("a", "c")])
    assert new == [{"from": "a", "to": "c", "port": 8080}]
    out = ps.with_calls(text, new)
    assert out == ("# who may call whom\nproduct: farm\ncalls:\n  - {from: a, to: b, port: 8080}\n"
                   "  - {from: a, to: c, port: 8080}\n\nbff: {client_id: a}\n")
    assert yaml.safe_load(ps.with_calls("calls: []\n", new))["calls"] == new
    assert yaml.safe_load(ps.with_calls("product: x\n", new))["calls"] == new


def test_g4_proposes_the_designs_calls_to_the_security_repo(services):
    product, _ = _farm(services)
    repos = [r.name for r in product.repos]
    services.github.files[("acme-farm/farm-security", "main", "security.yaml")] = "product: farm\ncalls: []\n"
    hld = "## Endpoints\n" + f"- {repos[0]}: external; calls {repos[1].split('/')[1]}, nothing-here\n"
    out = ps.propose(services, "FARM", "FEAT-7", hld)
    want = {"from": component_name(product, repos[0]), "to": component_name(product, repos[1]), "port": 8080}
    assert out["calls"] == [want] and out["url"]
    staged = services.github.files[("acme-farm/farm-security", "valxel/feat-7-calls", "security.yaml")]
    assert yaml.safe_load(staged)["calls"] == [want]
    services.github.files[("acme-farm/farm-security", "main", "security.yaml")] = staged
    assert ps.propose(services, "FARM", "FEAT-8", hld)["calls"] == []  # already allowed: no pull request


def test_approving_g4_proposes_the_calls(engine, services):
    services.github.files[("acme-farm/farm-security", "main", "security.yaml")] = "product: farm\ncalls: []\n"
    fid = engine.start(product_id="FARM", title="Offline crop log", description="Record observations offline")[
        "feature_id"]
    while gates := [p for p in engine.pending(fid) if p["type"] == "gate"]:  # approve until it waits on CI
        if any(p["type"] != "gate" for p in engine.pending(fid)):
            break
        engine.decide(fid, gates[0]["interrupt_id"], {"decision": "approve", "approver": "maria"})
    v = engine.view(fid)
    [e] = [e for e in v["events"] if e["node"] == "g4" and e["kind"].startswith("mesh_calls")]
    assert e["kind"] in ("mesh_calls_proposed", "mesh_calls_unchanged"), e
    if e["kind"] == "mesh_calls_proposed":
        assert any(k[0] == "acme-farm/farm-security" and k[1].endswith("-calls") for k in services.github.files)

"""Security S4: repos are created by the provisioning worker with its own GitHub App, and the
worker trusts governance, not the queue."""

from __future__ import annotations

import copy
import re
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from valxel_sdlc import signing, strict
from valxel_sdlc.config import load_settings
from valxel_sdlc.engine import FeatureEngine
from valxel_sdlc.provision_queue import (
    InMemoryProvisionQueue,
    QueuedProvisioner,
    check,
    run_one,
    split,
)
from valxel_sdlc.provisioning import RepoProvisioner
from valxel_sdlc.services import build_services

ROOT = Path(__file__).resolve().parents[1]
NEW = "acme-clinic/cln-patient-registration-service"


@pytest.fixture
def engine(tmp_path, monkeypatch):
    monkeypatch.setenv("VALXEL_PROVISIONING", "queue")
    root = tmp_path / "governance"
    shutil.copytree(ROOT / "examples" / "governance", root)
    path = root / "acme-clinic" / "cln-governance" / "product.yaml"
    path.write_text(re.sub(r"^repos:\n(?:- .*\n(?:  .*\n)*)+", "repos: []\nrepo_prefix: cln-\n", path.read_text(),
                           flags=re.MULTILINE))
    svc = build_services(load_settings(mode="stub", database_url="", artifact_dir=tmp_path / "a",
                                       governance_source="local", governance_root=root))
    assert isinstance(svc.provisioner, QueuedProvisioner)
    svc.provisioner.wait = 0  # don't wait for a worker that isn't running
    return FeatureEngine(svc, InMemorySaver())


def approve_gates(eng, fid):
    while True:
        p = [x for x in eng.pending(fid) if x["type"] == "gate"]
        if not p:
            return
        eng.decide(fid, p[0]["interrupt_id"], {"decision": "approve", "approver": "lead"})


def worker(eng):
    svc = eng.services
    return lambda: run_one(eng, svc.provision_queue, RepoProvisioner(svc.github, svc.openchoreo), svc.github)


def test_the_api_queues_the_repo_and_the_worker_creates_it(engine):
    eng, svc = engine, engine.services
    fid = eng.start(product_id="CLN", title="Patient registration")["feature_id"]
    approve_gates(eng, fid)
    [w] = [p for p in eng.pending(fid) if p["type"] == "external"]
    assert w["wait"] == "provisioning" and NEW in w["label"]
    assert NEW not in svc.github.repos  # the API process didn't create it
    assert eng.waits.check(w) is None

    ran = worker(eng)()
    assert ran["status"] == "done" and ran["result"]["status"] == "provisioned"
    assert NEW in svc.github.repos and worker(eng)() is None  # queue empty
    assert eng.waits.check(w)["status"] == "success"
    eng._execute(fid, eng._ready_wait(fid))
    approve_gates(eng, fid)
    assert eng.auto_drive(fid)["status"] == "done"
    assert any(pr["repo"] == NEW for pr in svc.github.prs.values())  # the build worked in it


def test_the_worker_refuses_what_governance_didnt_approve(engine):
    eng, svc = engine, engine.services
    fid = eng.start(product_id="CLN", title="Patient registration")["feature_id"]
    approve_gates(eng, fid)
    [req] = [r for r in svc.provision_queue._rows.values()]
    plan = req["payload"]["plan"]
    q = InMemoryProvisionQueue()

    def submit(**payload):
        return {"kind": "repo", "payload": {"product_id": "CLN", "feature_id": fid, "repo": NEW, "plan": plan,
                                            "governance_repo": "acme-clinic/cln-governance", **payload}}

    weaker = copy.deepcopy(plan)
    weaker["ruleset"]["rules"] = []  # a request that would drop branch protection
    assert "differs from what governance gives" in check(eng, submit(plan=weaker))
    assert "isn't among the repos G4 approved" in check(eng, submit(repo="acme-clinic/cln-backdoor"))
    assert "governance repo" in check(eng, submit(governance_repo="acme-clinic/other"))
    assert "no feature" in check(eng, submit(feature_id="F-nope"))
    assert check(eng, submit()) == ""
    q.submit(kind="repo", key="k", payload=submit(plan=weaker)["payload"])
    ran = run_one(eng, q, RepoProvisioner(svc.github, svc.openchoreo), svc.github)
    assert ran["status"] == "refused" and "cln-patient" not in str(svc.github.repos)


def test_without_an_approved_g4_nothing_is_provisioned(engine):
    eng = engine
    fid = eng.start(product_id="CLN", title="Patient registration")["feature_id"]
    req = {"kind": "repo", "payload": {"product_id": "CLN", "feature_id": fid, "repo": NEW, "plan": {},
                                       "governance_repo": "acme-clinic/cln-governance"}}
    assert "no approved G4" in check(eng, req)


def test_a_governance_repo_needs_its_inceptions_approved_product_gate():
    brief = "irrelevant"
    approved = {"gate_decisions": [{"gate": "product", "decision": "approve"}], "brief": brief}
    states = {"INC-1": approved, "INC-2": {"gate_decisions": [], "brief": brief}}
    eng = SimpleNamespace(services=None, _state=lambda tid: states.get(tid, {}))
    import valxel_sdlc.inception.setup as setup_mod

    real = setup_mod.ids
    setup_mod.ids = lambda b: {"product_id": "FARM", "governance_repo": "acme-farm/farm-governance"}
    try:
        def req(**p):
            return {"kind": "governance_repo", "payload": {"product_id": "FARM", "repo": "acme-farm/farm-governance",
                                                           "inception_id": "INC-1", **p}}
        assert check(eng, req()) == ""
        assert "no approved product gate" in check(eng, req(inception_id="INC-2"))
        assert "isn't the governance repo" in check(eng, req(repo="acme-farm/anything"))
        assert "no inception" in check(eng, req(inception_id="INC-9"))
    finally:
        setup_mod.ids = real


def test_failures_come_back_as_failed_steps():
    q = InMemoryProvisionQueue()
    prov = QueuedProvisioner(SimpleNamespace(provision_queue=q), wait=0)
    product = SimpleNamespace(product_id="CLN")
    first = prov.provision(product, {"repo": NEW}, feature_id="F-1", governance_repo="g", manifest_text="")
    assert first["status"] == "queued"
    q.finish(first["request_id"], status="refused", result={"why": "not approved"})
    again = prov.provision(product, {"repo": NEW}, feature_id="F-1", governance_repo="g", manifest_text="")
    assert again["status"] == "failed" and again["steps"][0]["error"] == "not approved"
    assert prov.done([first["request_id"]])


# ------------------------------------------------------------------ settings and strict mode

def test_the_two_apps_have_their_own_settings(monkeypatch):
    monkeypatch.setattr(signing, "KmsSigner", lambda key_id, region="": ("kms", key_id))
    env = {"VALXEL_GITHUB_APP_ID": "1", "VALXEL_GITHUB_APP_KMS_KEY_ID": "delivery-key",
           "VALXEL_GITHUB_PROVISIONER_APP_ID": "2", "VALXEL_GITHUB_PROVISIONER_APP_KMS_KEY_ID": "prov-key"}
    assert signing.github_app_signer(env) == ("kms", "delivery-key")
    assert signing.github_app_signer(env, app="provisioner") == ("kms", "prov-key")
    assert signing.github_app_configured(env, app="provisioner")
    assert not signing.github_app_configured({"VALXEL_GITHUB_APP_ID": "1"}, app="provisioner")
    assert signing.provisioner_key_reachable(env) == ["VALXEL_GITHUB_PROVISIONER_APP_KMS_KEY_ID"]
    assert signing.provisioner_key_reachable({"VALXEL_GITHUB_APP_KMS_KEY_ID": "delivery-key"}) == []


def test_strict_mode_keeps_administration_out_of_the_api_process(tmp_path):
    assert any("VALXEL_PROVISIONING is not queue" in p for p in strict.provisioning_split({"VALXEL_DELIVERY": "live"}))
    assert strict.provisioning_split({"VALXEL_DELIVERY": "live", "VALXEL_PROVISIONING": "queue"}) == []
    (tmp_path / "VALXEL_GITHUB_PROVISIONER_PRIVATE_KEY").write_text("pem")
    found = strict.provisioning_split({"VALXEL_DELIVERY": "live", "VALXEL_PROVISIONING": "queue",
                                       "VALXEL_SECRETS_DIR": str(tmp_path)})
    assert found and "VALXEL_GITHUB_PROVISIONER_PRIVATE_KEY" in found[0]
    assert split({"VALXEL_PROVISIONING": "queue"}) and not split({})


def test_a_refused_request_escalates_and_a_retry_asks_again(engine):
    eng, svc = engine, engine.services
    fid = eng.start(product_id="CLN", title="Patient registration")["feature_id"]
    approve_gates(eng, fid)
    assert [p["wait"] for p in eng.pending(fid) if p["type"] == "external"] == ["provisioning"]
    [req] = list(svc.provision_queue._rows.values())
    svc.provision_queue.claim()
    svc.provision_queue.finish(req["id"], status="refused", result={"why": "not what G4 approved"})
    eng._execute(fid, eng._ready_wait(fid))
    [esc] = [p for p in eng.pending(fid) if p["type"] == "escalation"]
    assert esc["phase"] == "provisioning" and "not what G4 approved" in esc["reason"]
    eng.decide(fid, esc["interrupt_id"], {"decision": "retry", "approver": "lead", "feedback": "try again"})
    keys = sorted(r["key"] for r in svc.provision_queue._rows.values())
    assert keys == [f"repo:{fid}:{NEW}:0", f"repo:{fid}:{NEW}:1"]
    assert worker(eng)()["status"] == "done" and NEW in svc.github.repos

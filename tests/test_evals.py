"""Evals of the tool's own agents: cases from real work, replayed under a candidate config."""

import hashlib
import hmac
import json
import time
from pathlib import Path

import yaml
from fastapi.testclient import TestClient

from valxel_sdlc import cli, evals
from valxel_sdlc.api.app import create_app
from valxel_sdlc.integrations import StubGitHub
from valxel_sdlc.web.auth import AuthConfig

from conftest import approve


def released_with_a_revision(engine):
    """A feature whose spec was sent back once at G2, then approved."""
    fid = engine.start(product_id="FARM", title="Offline crop log", description="Record observations offline")["feature_id"]
    approve(engine, fid, "g1")
    [g2] = [p for p in engine.pending(fid) if p.get("gate") == "g2"]
    engine.decide(fid, g2["interrupt_id"], {"decision": "revise", "approver": "pm", "feedback": "add the sync limit",
                                            "reason": "missing_requirement"})
    assert engine.auto_drive(fid)["status"] == "done"
    return fid


def test_document_phases_leave_cases_labelled_by_the_gates(engine, services):
    fid = released_with_a_revision(engine)
    cases = services.evals.cases(product_id="FARM", labeled=False)
    assert {c.phase for c in cases} == {"discovery", "spec", "design", "architecture", "plan"}
    spec = sorted((c for c in cases if c.phase == "spec"), key=lambda c: c.at)
    assert [c.id for c in spec] == [f"{fid}:spec:1", f"{fid}:spec:2"]
    assert spec[0].label["decision"] == "revise" and spec[0].label["reason"] == "missing_requirement"
    assert spec[0].label["first_time"] is False and spec[1].label["decision"] == "approve"
    assert spec[0].reference == spec[1].reference == spec[1].output  # the version people approved
    assert spec[0].ctx["parent_texts"] and "feedback" not in spec[0].ctx  # replayable, without the old run's feedback
    design = next(c for c in cases if c.phase == "design")
    assert design.label == {"decision": "approve", "reason": "", "feedback": "", "taken_over": False, "first_time": True}
    plan = next(c for c in cases if c.phase == "plan")  # no gate: replayed, but there's no person to agree with
    assert plan.label["decision"] == "no_gate" and plan.reference == plan.output and plan.config_version


def test_a_run_measures_documents_and_the_critic_against_people(engine, services):
    released_with_a_revision(engine)
    cases = services.evals.cases(product_id="FARM")
    res = evals.run(evals.services_for(services.settings), cases, pairwise=True)
    s = res["summary"]
    assert s["all"]["cases"] == len(cases) and s["all"]["pass_rate"] == 1.0 and s["all"]["errors"] == 0
    # The critic passed the spec people sent back: it disagrees with them there, agrees elsewhere.
    assert s["phases"]["spec"]["critic_agreement"] == 0.5 and s["phases"]["design"]["critic_agreement"] == 1.0
    assert s["phases"]["plan"]["critic_agreement"] is None
    assert s["phases"]["spec"]["pairwise"] == {"win": 0, "tie": 2, "loss": 0}
    assert services.ledger.records("FARM-FEAT-1")  # the feature's own ledger ...
    before = len(services.ledger.records())
    evals.run(evals.services_for(services.settings), cases)
    assert len(services.ledger.records()) == before  # ... is untouched by eval runs


def test_a_worse_config_is_a_regression(engine, services, monkeypatch):
    released_with_a_revision(engine)
    cases = services.evals.cases(product_id="FARM")
    base = evals.run(evals.services_for(services.settings), cases)
    from valxel_sdlc.evaluators import critic

    real = critic.stub_verdict
    monkeypatch.setattr(critic, "stub_verdict", lambda content, phase="", items=None: real("", phase, items))  # quotes nothing
    cand = evals.run(evals.services_for(services.settings), cases)
    cmp = evals.compare(base, cand)
    assert not cmp["ok"] and any("pass rate fell from 100% to 0%" in r for r in cmp["regressions"])
    assert evals.headline(cmp).startswith(f"{len(cmp['regressions'])} regressions:")
    assert "| all | 100% → 0% |" in evals.markdown(cmp, base, cand)
    assert evals.compare(base, base)["ok"]


def test_the_cli_exports_and_runs_from_a_file(engine, services, tmp_path, capsys):
    released_with_a_revision(engine)
    path = tmp_path / "cases.jsonl"
    evals.dump_jsonl(services.evals.cases(product_id="FARM"), path)
    out = tmp_path / "results.json"
    code = cli.main(["eval", "run", "--mode", "stub", "--cases", str(path), "--config", str(services.settings.config_dir),
                     "--out", str(out), "--limit", "1"])
    assert code == 0 and "no regression" in capsys.readouterr().out
    report = json.loads(out.read_text())
    assert report["comparison"]["ok"] and report["comparison"]["cases"] == 5  # the newest case of each phase
    assert cli.main(["eval", "export", "--mode", "stub", "--cases", str(path), "--data-class", "phi",
                     "--out", str(tmp_path / "phi.jsonl")]) == 0
    assert (tmp_path / "phi.jsonl").read_text() == ""  # FARM is general: nothing regulated leaves


def test_a_config_pull_request_gets_an_eval_status(engine, services, monkeypatch):
    released_with_a_revision(engine)
    gh = StubGitHub()
    repo = "valxel-tools/sdlc-config"
    models = yaml.safe_load((Path(services.settings.config_dir) / "models.yaml").read_text())
    gh.files[(repo, "head1", "config/models.yaml")] = yaml.safe_dump(models)
    gate = evals.ConfigGate(services, gh, config_repo=repo, public_url="https://sdlc.example", background=False)
    run = gate.start("head1", "", requested_by="ana")
    done = gate.runs[run["id"]]
    assert done["status"] == "passed" and done["comparison"]["ok"] and done["comparison"]["cases"] > 0
    states = [(s["state"], s["context"]) for s in gh.statuses if s["sha"] == "head1"]
    assert states == [("pending", "valxel / evals"), ("success", "valxel / evals")]
    assert gh.statuses[-1]["url"] == f"https://sdlc.example/api/evals/runs/{run['id']}"


def test_the_webhook_starts_a_run_and_the_api_shows_it(engine, services, monkeypatch):
    released_with_a_revision(engine)
    monkeypatch.setenv("VALXEL_CONFIG_REPO", "valxel-tools/sdlc-config")
    monkeypatch.setenv("VALXEL_GITHUB_WEBHOOK_SECRET", "s3cret")
    monkeypatch.setenv("VALXEL_EVAL_APPROVERS", "valxel-tools/platform")
    with TestClient(create_app(engine, auth=AuthConfig(mode="dev", secret="x" * 40), web_dist=None)) as c:
        body = json.dumps({"action": "synchronize", "repository": {"full_name": "valxel-tools/sdlc-config"},
                           "pull_request": {"head": {"sha": "abc123", "ref": "cheaper-critic"}, "base": {"sha": ""},
                                            "user": {"login": "ana"}}}).encode()
        sig = "sha256=" + hmac.new(b"s3cret", body, hashlib.sha256).hexdigest()
        r = c.post("/api/webhooks/github", content=body, headers={"x-hub-signature-256": sig, "x-github-event": "pull_request",
                                                                    "content-type": "application/json"})
        run_id = r.json()["eval_run"]
        c.post("/api/auth/dev-login", json={"login": "bob", "teams": ["acme-farm/product-owners"]})
        for _ in range(200):
            if c.get(f"/api/evals/runs/{run_id}").json()["status"] != "running":
                break
            time.sleep(0.05)
        assert c.get(f"/api/evals/runs/{run_id}").json()["status"] == "passed"
        assert c.get("/api/evals/runs").json()[0]["id"] == run_id
        assert c.post("/api/evals/runs", json={"ref": "abc123"}).status_code == 403
        c.post("/api/auth/dev-login", json={"login": "ana", "teams": ["valxel-tools/platform"]})
        assert c.post("/api/evals/runs", json={"ref": "abc123"}).status_code == 202


def test_langfuse_gets_datasets_runs_and_scores_with_content_only_for_general_cases(engine, services):
    released_with_a_revision(engine)
    cases = services.evals.cases(product_id="FARM", phases=("spec",))
    cases[1].data_class = "phi"  # as if it were a PHI product's case
    sent = []

    class Opener:
        def open(self, req, timeout=None):
            sent.append((req.full_url.split("/api/public/")[1], json.loads(req.data)))

            class R:
                def __enter__(self):
                    return self

                def __exit__(self, *a):
                    return False

                def read(self):
                    return b"{}"
            return R()

    results = [{"case_id": c.id, "trace_ids": ["t" + str(i)], "passed": True, "must_met": 1.0, "critic_agrees": None}
               for i, c in enumerate(cases)]
    lf = evals.Langfuse("https://lf.example", "pk", "sk", opener=Opener())
    assert lf.sync(cases, "run-1", results) == 2
    items = [b for p, b in sent if p == "dataset-items"]
    assert items[0]["input"]["parents"] and items[1]["input"] == {"case": cases[1].id}  # no PHI content leaves
    assert ("dataset-run-items", {"runName": "run-1", "datasetItemId": cases[0].id.replace(":", "-"), "traceId": "t0"}) in sent
    assert {b["name"] for p, b in sent if p == "scores"} == {"valxel.passed", "valxel.must_met"}

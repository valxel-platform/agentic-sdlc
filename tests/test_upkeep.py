"""Scan upkeep: security findings on a product's default branch after merge (valxel_sdlc.upkeep).

Alerts in (webhooks, the nightly sweep) -> the findings register -> triage by policy -> a fix
feature per repo: a hotfix now, or a fix held for the next release inside its deadline (else its
own G6 just before it). Secrets open incidents; Dependabot's routine updates merge when green.
"""

import hashlib
import hmac
import json
import shutil
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import InMemorySaver

from valxel_sdlc import upkeep
from valxel_sdlc.api.app import create_app
from valxel_sdlc.config import load_settings
from valxel_sdlc.engine import FeatureEngine
from valxel_sdlc.governance import GovernanceError
from valxel_sdlc.quality.sonar import SonarQube
from valxel_sdlc.services import build_services
from valxel_sdlc.upkeep import alerts
from valxel_sdlc.upkeep import service as service_mod
from valxel_sdlc.web.auth import AuthConfig

ROOT = Path(__file__).resolve().parents[1]
API = "acme-farm/farm-field-api"
TODAY = datetime.now(UTC).date()  # the register keeps UTC dates
SEC = "acme-farm/security"


@pytest.fixture
def gov_root(tmp_path):
    root = tmp_path / "governance"
    shutil.copytree(ROOT / "examples" / "governance", root)
    return root


def make(tmp_path, gov_root):
    s = load_settings(mode="stub", database_url="", artifact_dir=tmp_path / "a",
                      governance_source="local", governance_root=gov_root)
    svc = build_services(s)
    return FeatureEngine(svc, InMemorySaver()), svc


def code_alert(number=7, severity="high", state="open"):
    return {"number": number, "state": state, "html_url": f"https://github.com/{API}/security/code-scanning/{number}",
            "tool": {"name": "CodeQL"}, "rule": {"id": "java/sql-injection", "security_severity_level": severity,
                                                  "description": "SQL built from user input"},
            "most_recent_instance": {"location": {"path": "src/main/java/Crops.java"}}}


def dep_alert(number=3, severity="medium", pkg="jackson-databind", epss=None, state="open"):
    adv = {"ghsa_id": f"GHSA-{number}", "summary": "Deserialization of untrusted data", "severity": severity}
    if epss is not None:
        adv["epss"] = [{"percentage": epss, "percentile": 0.9}]
    return {"number": number, "state": state, "html_url": f"https://github.com/{API}/security/dependabot/{number}",
            "security_advisory": adv,
            "security_vulnerability": {"severity": severity, "package": {"name": pkg},
                                       "first_patched_version": {"identifier": "2.17.1"}},
            "dependency": {"package": {"name": pkg}, "manifest_path": "build.gradle"}}


def event(kind, action, alert, repo=API, ref=None):
    body = {"action": action, "alert": alert, "repository": {"full_name": repo, "default_branch": "main"}}
    if ref is not None:
        body["ref"] = ref
    return kind, body


def run_bg(eng):
    """Fix features start in the background: wait for them."""
    eng._pool.shutdown(wait=True)
    from concurrent.futures import ThreadPoolExecutor

    eng._pool = ThreadPoolExecutor(max_workers=4)


# --------------------------------------------------------------------------- policy


def test_policy_tightens_by_data_class_and_products_may_only_tighten():
    plat = upkeep.load({"floors": {"payments": {"fix_within_days": {"high": 14}, "hotfix": {"severity": "high"}}}})
    general = upkeep.effective(plat, "general", None)
    assert general["fix_within_days"]["high"] == 30 and general["hotfix"]["severity"] == "critical"
    pay = upkeep.effective(plat, "payments", None)
    assert pay["fix_within_days"]["high"] == 14 and pay["hotfix"]["severity"] == "high"
    problems = []
    upkeep.effective(plat, "payments", {"fix_within_days": {"high": 60}, "hotfix": {"secrets": False, "severity": "critical"},
                                        "dependabot": {"auto_merge": ["major"]}, "release_lead_days": 0, "nope": 1}, problems)
    text = "; ".join(problems)
    for bit in ("high 60 is longer", "secrets can't be turned off", "severity critical is looser",
                "may only narrow", "release_lead_days 0 is looser", "unknown keys ['nope']"):
        assert bit in text
    tighter = upkeep.effective(plat, "general", {"fix_within_days": {"medium": 30}, "max_open_fixes": 2,
                                                 "dependabot": {"auto_merge": ["patch"]}})
    assert tighter["fix_within_days"]["medium"] == 30 and tighter["max_open_fixes"] == 2
    assert tighter["dependabot"]["auto_merge"] == ["patch"]


def test_the_policy_decides_hotfix_scheduled_or_backlog():
    pol = upkeep.effective(upkeep.load(None), "general", None)
    assert upkeep.decide(pol, {"kind": "code", "severity": "critical"})[0] == "hotfix"
    assert upkeep.decide(pol, {"kind": "secret", "severity": "critical"}) == ("hotfix", "an exposed secret")
    assert upkeep.decide(pol, {"kind": "dependency", "severity": "high", "epss": 0.4})[0] == "hotfix"
    assert upkeep.decide(pol, {"kind": "dependency", "severity": "high", "epss": 0.01})[0] == "scheduled"
    assert upkeep.decide(pol, {"kind": "dependency", "severity": "medium", "epss": 0.9})[0] == "scheduled"
    assert upkeep.decide(pol, {"kind": "code", "severity": "info"})[0] == "backlog"
    assert upkeep.deadline(pol, "high", "2026-11-01T10:00:00+00:00") == "2026-12-01"
    assert upkeep.release_by(pol, "2026-12-01") == "2026-11-29"
    assert [upkeep.update_type(a, b) for a, b in (("4.17.20", "4.17.21"), ("1.2.3", "1.3.0"), ("1.9", "2.0"),
                                                  ("0.3.1", "0.4.0"), ("v2.0.0-rc1", "v2.0.0"), ("x", "1"))] == [
        "patch", "minor", "major", "major", "patch", ""]


def test_a_product_cant_loosen_the_policy(tmp_path, gov_root):
    gov = gov_root / "acme-farm" / "farm-governance" / "product.yaml"
    gov.write_text(gov.read_text() + "upkeep:\n  fix_within_days: {critical: 30}\n")
    _, svc = make(tmp_path, gov_root)
    with pytest.raises(GovernanceError, match="critical 30 is longer than the platform's 7"):
        svc.snapshot("FARM")


def test_payments_products_get_the_tighter_floor(tmp_path, gov_root):
    _, svc = make(tmp_path, gov_root)
    pol = svc.snapshot("SHOP").product.upkeep
    assert pol["fix_within_days"]["high"] == 14 and pol["hotfix"]["severity"] == "high"
    assert svc.snapshot("FARM").product.upkeep["fix_within_days"]["high"] == 30


# --------------------------------------------------------------------------- alerts


def test_alerts_from_webhooks_and_lists_share_the_quality_gates_fingerprints():
    f = alerts.from_event(*event("code_scanning_alert", "created", code_alert()))
    assert (f["id"], f["severity"], f["state"], f["path"]) == (f"{API}:code-scanning:7", "high", "open",
                                                              "src/main/java/Crops.java")
    # An alert on a pull request's branch is the feature's business, not upkeep's.
    assert alerts.from_event(*event("code_scanning_alert", "created", code_alert(), ref="refs/heads/feature/x")) is None
    assert alerts.from_event(*event("code_scanning_alert", "fixed", code_alert()))["state"] == "fixed"
    d = alerts.from_event(*event("dependabot_alert", "created", dep_alert(epss=0.2)))
    assert (d["id"], d["package"], d["patched"], d["epss"]) == (f"{API}:GHSA-3:jackson-databind", "jackson-databind",
                                                               "2.17.1", 0.2)
    s = alerts.from_event(*event("secret_scanning_alert", "created", {"number": 2, "secret_type": "aws_access_key_id",
                                                                     "secret_type_display_name": "AWS Access Key ID",
                                                                     "validity": "active"}))
    assert (s["id"], s["kind"], s["severity"], s["validity"]) == (f"{API}:secret:2", "secret", "critical", "active")
    assert alerts.from_event("push", {"alert": {}}) is None
    assert alerts.from_event(*event("dependabot_alert", "assigned", dep_alert())) is None


# --------------------------------------------------------------------------- triage and fixes


def test_a_critical_finding_starts_a_security_fix_at_planning(tmp_path, gov_root):
    eng, svc = make(tmp_path, gov_root)
    rec = eng.upkeep.receive(*event("code_scanning_alert", "created", code_alert(severity="critical")))
    assert (rec["status"], rec["outcome"], rec["deadline"]) == ("planned", "hotfix",
                                                               (TODAY + timedelta(days=7)).isoformat())
    [fid] = eng.upkeep.start_fixes("FARM")
    run_bg(eng)
    rec = svc.findings.get(rec["id"])
    assert (rec["status"], rec["fix_feature"], rec["attempts"]) == ("fixing", fid, 1)
    s = eng._state(fid)
    assert s["track"] == "hotfix" and s["hotfix"]["source"] == "finding" and not s["hotfix"]["hold"]
    assert "Security fix spec" in eng.artifact(fid, "PRD")["content"]
    assert "GitHub closes the alert" in eng.artifact(fid, "PRD")["content"]
    seen = {}
    for _ in range(10):
        p = [x for x in eng.pending(fid) if x["type"] == "gate"]
        if not p:
            break
        seen[p[0]["gate"]] = p[0]["approvers"]
        eng.decide(fid, p[0]["interrupt_id"], {"decision": "approve", "approver": "someone"})
    # G6 is the release approvers' as usual; nothing waits for a release.
    assert seen.get("g6") == ["acme-farm/release-managers"]
    assert eng.view(fid)["status"] == "done"
    # A fix that's released while GitHub still has the alert open is tried again.
    eng.upkeep.follow("FARM", TODAY)
    assert svc.findings.get(rec["id"])["status"] == "open"


def test_security_fixes_take_g5_from_the_security_approvers(tmp_path, gov_root):
    from valxel_sdlc.graph.pipeline import hotfix_approvers

    _, svc = make(tmp_path, gov_root)
    lane = svc.snapshot("CLN").product
    fix = {"track": "hotfix", "hotfix": {"source": "finding"}}
    assert hotfix_approvers(fix, lane, "g5") == ["acme-clinic/security"]
    assert hotfix_approvers(fix, lane, "g6") == ["acme-clinic/release-managers"]
    assert hotfix_approvers({"track": "hotfix", "hotfix": {"incident_id": "x"}}, lane, "g5") == list(
        lane.approvers.get("oncall") or lane.approvers["g5"])


def test_findings_in_one_repo_share_a_fix_and_the_cap_holds_the_rest(tmp_path, gov_root):
    eng, svc = make(tmp_path, gov_root)
    gov = gov_root / "acme-farm" / "farm-governance" / "product.yaml"
    gov.write_text(gov.read_text() + "upkeep:\n  max_open_fixes: 1\n  dependabot_grace_hours: 0\n")
    for n in (1, 2):
        eng.upkeep.receive(*event("code_scanning_alert", "created", code_alert(number=n, severity="high")))
    eng.upkeep.receive(*event("dependabot_alert", "created", dep_alert(severity="critical"),
                              repo="acme-farm/farm-mobile"))
    started = eng.upkeep.start_fixes("FARM")
    run_bg(eng)
    assert len(started) == 1  # one slot: the hotfix goes first
    assert eng._state(started[0])["hotfix"]["repo"] == "acme-farm/farm-mobile"
    waiting = [r for r in svc.findings.list("FARM") if r["status"] == "planned"]
    assert {r["repo"] for r in waiting} == {API} and len(waiting) == 2


def test_dependency_findings_give_dependabot_a_day_first(tmp_path, gov_root):
    eng, svc = make(tmp_path, gov_root)
    eng.upkeep.receive(*event("dependabot_alert", "created", dep_alert(severity="high")))
    assert eng.upkeep.start_fixes("FARM") == []  # within dependabot_grace_hours
    rec = svc.findings.list("FARM")[0]
    svc.findings.update(rec["id"], first_seen=(TODAY - timedelta(days=2)).isoformat() + "T00:00:00+00:00")
    assert len(eng.upkeep.start_fixes("FARM")) == 1
    run_bg(eng)


def test_a_scheduled_fix_rides_with_the_next_release_inside_its_deadline(tmp_path, gov_root):
    eng, svc = make(tmp_path, gov_root)
    a = eng.create(product_id="FARM", title="Offline crop log")
    rid = eng.create_release(product_id="FARM", title="Next train", model="train", features=[a],
                             target_date=(TODAY + timedelta(days=5)).isoformat())
    rec = eng.upkeep.receive(*event("code_scanning_alert", "created", code_alert(severity="high")))
    assert rec["outcome"] == "scheduled"
    [fid] = eng.upkeep.start_fixes("FARM")
    run_bg(eng)
    assert eng._state(fid)["hotfix"]["hold"] is True
    eng.auto_drive(fid)  # its fix window finds the train at once, so it waits for that
    assert svc.findings.release_of(fid) == rid and svc.findings.get(rec["id"])["release_id"] == rid
    assert [p.get("wait") for p in eng.pending(fid)] == ["release_train"]
    assert eng.release_for(fid)["id"] == rid
    eng.auto_drive(a)
    g = next(p for p in eng.pending(rid) if p.get("gate") == "release")
    assert set(g["features"]) == {a, fid}  # the fix rides along
    eng.decide(rid, g["interrupt_id"], {"decision": "approve", "approver": "rm"})
    for f in (a, fid):
        assert eng.view(f)["status"] == "done"


def test_with_no_release_in_time_a_held_fix_goes_to_its_own_g6_on_its_date(tmp_path, gov_root, monkeypatch):
    eng, svc = make(tmp_path, gov_root)
    rec = eng.upkeep.receive(*event("code_scanning_alert", "created", code_alert(severity="high")))
    [fid] = eng.upkeep.start_fixes("FARM")
    run_bg(eng)
    eng.auto_drive(fid)
    assert not eng.poke(fid)  # no release, not due yet: keeps waiting
    later = date.fromisoformat(svc.findings.get(rec["id"])["release_by"])
    monkeypatch.setattr(service_mod, "_today", lambda: later)
    assert eng.poke(fid)
    run_bg(eng)
    g = next(p for p in eng.pending(fid) if p.get("gate") == "g6")
    assert g["approvers"] == ["acme-farm/release-managers"]


def test_a_rider_whose_release_runs_late_leaves_it_for_its_own_g6(tmp_path, gov_root, monkeypatch):
    eng, svc = make(tmp_path, gov_root)
    a = eng.create(product_id="FARM", title="Offline crop log")
    rid = eng.create_release(product_id="FARM", title="Next train", model="train", features=[a],
                             target_date=(TODAY + timedelta(days=5)).isoformat())
    eng.upkeep.receive(*event("code_scanning_alert", "created", code_alert(severity="high")))
    [fid] = eng.upkeep.start_fixes("FARM")
    run_bg(eng)
    eng.auto_drive(fid)
    assert svc.findings.release_of(fid) == rid
    # The train's still waiting for its feature on the fix's release-by date: the fix leaves.
    by = date.fromisoformat(eng._state(fid)["hotfix"]["release_by"])
    monkeypatch.setattr(service_mod, "_today", lambda: by)
    assert eng.poke(fid)
    run_bg(eng)
    assert next(p for p in eng.pending(fid) if p.get("gate") == "g6")
    assert svc.findings.release_of(fid) is None


# --------------------------------------------------------------------------- secrets, deadlines, people


def test_an_exposed_secret_opens_an_incident_that_resolves_when_the_alert_closes(tmp_path, gov_root):
    eng, svc = make(tmp_path, gov_root)
    leak = {"number": 4, "secret_type": "aws_access_key_id", "secret_type_display_name": "AWS Access Key ID",
            "validity": "active", "html_url": f"https://github.com/{API}/security/secret-scanning/4"}
    rec = eng.upkeep.receive(*event("secret_scanning_alert", "created", leak))
    run_bg(eng)
    assert rec["status"] == "incident" and rec["incident_id"]
    v = eng.view(rec["incident_id"])
    assert v["severity"] == "sev1" and "Rotate it now" in json.dumps(svc.signals.for_incident(rec["incident_id"]))
    assert eng.upkeep.start_fixes("FARM") == []  # on-call decides the code fix in the incident
    eng.upkeep.receive(*event("secret_scanning_alert", "resolved", leak))
    run_bg(eng)
    assert svc.findings.get(rec["id"])["status"] == "fixed"
    assert svc.signals.for_incident(rec["incident_id"])[-1]["status"] == "resolved"


def test_an_overdue_finding_asks_a_person_once(tmp_path, gov_root):
    eng, svc = make(tmp_path, gov_root)
    rec = eng.upkeep.receive(*event("code_scanning_alert", "created", code_alert(severity="high")))
    assert eng.upkeep.deadlines("FARM", TODAY) == []
    late = date.fromisoformat(rec["deadline"]) + timedelta(days=1)
    assert eng.upkeep.deadlines("FARM", late) == [rec["id"]]
    run_bg(eng)
    rec = svc.findings.get(rec["id"])
    assert rec["raised"] and rec["incident_id"]
    assert eng.upkeep.deadlines("FARM", late) == []  # once
    # Accepting the risk resolves it and moves the deadline.
    acc = eng.upkeep.accept(rec["id"], approver="sec-1", reason="behind the WAF until the rewrite", days=30)
    run_bg(eng)
    assert acc["status"] == "accepted" and acc["accepted"]["expires"] == (TODAY + timedelta(days=30)).isoformat()
    with pytest.raises(ValueError, match="1 to 90 days"):
        eng.upkeep.accept(rec["id"], approver="sec-1", reason="x", days=365)
    with pytest.raises(ValueError, match="Say why"):
        eng.upkeep.accept(rec["id"], approver="sec-1", reason=" ", days=5)
    # It comes back when the acceptance expires, with a fresh deadline.
    assert eng.upkeep.expire("FARM", TODAY + timedelta(days=30)) == [rec["id"]]
    assert svc.findings.get(rec["id"])["status"] == "open"


def test_a_risk_accepted_in_a_feature_holds_on_main(tmp_path, gov_root, monkeypatch):
    eng, _ = make(tmp_path, gov_root)
    fp = f"{API}:code-scanning:7"
    monkeypatch.setattr(eng.upkeep, "feature_waivers", lambda pid: {fp: {
        "fingerprint": fp, "approver": "sec-1", "reason": "test-only path", "expires": "2099-01-01",
        "feature_id": "FARM-FEAT-1"}})
    rec = eng.upkeep.receive(*event("code_scanning_alert", "created", code_alert()))
    assert rec["status"] == "accepted" and rec["accepted"]["approver"] == "sec-1"


def test_the_sweep_finds_missed_alerts_and_closes_fixed_ones(tmp_path, gov_root):
    eng, svc = make(tmp_path, gov_root)
    svc.github.code_alerts[API] = [code_alert(number=1), code_alert(number=2, severity="low")]
    svc.github.dependabot[API] = [dep_alert()]
    out = eng.upkeep.sweep("FARM")
    assert out["alerts"] == 3 and out["closed"] == 0
    assert {r["id"] for r in svc.findings.list("FARM")} == {
        f"{API}:code-scanning:1", f"{API}:code-scanning:2", f"{API}:GHSA-3:jackson-databind"}
    svc.github.code_alerts[API] = [code_alert(number=1)]  # #2 was fixed while no webhook reached us
    assert eng.upkeep.sweep("FARM")["closed"] == 1
    assert svc.findings.get(f"{API}:code-scanning:2")["status"] == "fixed"


# --------------------------------------------------------------------------- Dependabot


def dependabot_pr(svc, title, body=""):
    pr = svc.github.open_pr(repo=API, branch=f"dependabot/gradle/{abs(hash(title))}", title=title, body=body,
                            files=[], labels=[])
    svc.github.prs[f"{API}#{pr['number']}"]["author"] = "dependabot[bot]"
    return pr


def test_green_patch_and_minor_dependabot_updates_merge_by_themselves(tmp_path, gov_root):
    eng, svc = make(tmp_path, gov_root)
    patch = dependabot_pr(svc, "Bump com.google.guava:guava from 33.2.0 to 33.2.1")
    major = dependabot_pr(svc, "Bump org.flywaydb:flyway-core from 10.4.0 to 11.0.0")
    group = dependabot_pr(svc, "Bump the spring-boot group with 2 updates",
                          "Updates `org.springframework.boot` from 3.3.1 to 3.3.4\nUpdates `x` from 1.0.0 to 1.1.0")
    merged = eng.upkeep.dependabot("FARM")
    assert sorted(merged) == sorted([f"{API}#{patch['number']}", f"{API}#{group['number']}"])
    assert svc.github.prs[f"{API}#{major['number']}"]["state"] == "open"  # majors go to people
    gates = [s for s in svc.github.statuses if s["context"] == "valxel/gates"]
    assert {s["sha"] for s in gates} == {patch["head_sha"], group["head_sha"]}


def test_dependabot_waits_for_red_checks_and_leaves_security_updates_to_their_fix(tmp_path, gov_root):
    eng, svc = make(tmp_path, gov_root)
    red = dependabot_pr(svc, "Bump com.google.guava:guava from 33.2.0 to 33.2.1")
    svc.github.ci[(API, red["head_sha"])] = "failure"
    eng.upkeep.receive(*event("dependabot_alert", "created", dep_alert(pkg="jackson-databind")))
    sec = dependabot_pr(svc, "Bump jackson-databind from 2.17.0 to 2.17.1")
    assert eng.upkeep.dependabot("FARM") == []
    assert svc.github.prs[f"{API}#{sec['number']}"]["state"] == "open"


# --------------------------------------------------------------------------- API


def test_alert_webhooks_reach_the_register_and_security_approvers_accept_risk(monkeypatch, tmp_path, gov_root):
    monkeypatch.setenv("VALXEL_GITHUB_WEBHOOK_SECRET", "s3cret")
    eng, _ = make(tmp_path, gov_root)
    client = TestClient(create_app(engine=eng, auth=AuthConfig(mode="dev", secret="x" * 32), web_dist=None))
    kind, payload = event("code_scanning_alert", "created", code_alert())
    body = json.dumps(payload).encode()
    sig = "sha256=" + hmac.new(b"s3cret", body, hashlib.sha256).hexdigest()
    r = client.post("/api/webhooks/github", content=body, headers={"x-hub-signature-256": sig, "x-github-event": kind})
    assert r.status_code == 202 and r.json()["finding"] == f"{API}:code-scanning:7"
    client.post("/api/auth/dev-login", json={"login": "dev-1", "teams": ["acme-farm/product-owners"], "orgs": ["acme-farm"]})
    rows = client.get("/api/findings", params={"product_id": "FARM"}).json()
    assert [(x["id"], x["status"], x["outcome"]) for x in rows] == [(f"{API}:code-scanning:7", "planned", "scheduled")]
    url = f"/api/findings/{API}:code-scanning:7/accept"
    assert client.post(url, json={"reason": "fixed upstream next week", "days": 10}).status_code == 403
    client.post("/api/auth/dev-login", json={"login": "sec-1", "teams": [SEC], "orgs": ["acme-farm"]})
    acc = client.post(url, json={"reason": "fixed upstream next week", "days": 10})
    assert acc.status_code == 200 and acc.json()["status"] == "accepted"
    assert client.post(url, json={"reason": "", "days": 10}).status_code == 400


# --------------------------------------------------------------------------- SonarQube


class FakeSonar:
    def __init__(self, gate, issues=()):
        self.gate, self.issues, self.urls = gate, list(issues), []

    def __call__(self, req):
        import io

        self.urls.append(req.full_url)
        assert req.headers["Authorization"] == "Bearer tok"
        body = ({"projectStatus": self.gate} if "qualitygates" in req.full_url else {"issues": self.issues})

        class R(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        return R(json.dumps(body).encode())


SPEC = {"kind": "quality", "stage": "pr", "source": "sonarqube", "block_on": "high",
        "match": {"host": "https://sonarcloud.io", "project_key": "{owner}_{name}"}}
TARGET = [{"repo": API, "number": 12, "sha": "abc"}]


def test_a_failed_sonarqube_gate_blocks_with_its_issues_for_the_coder():
    from valxel_sdlc.quality import evaluate

    fake = FakeSonar({"status": "ERROR", "conditions": [
        {"status": "ERROR", "metricKey": "new_coverage", "actualValue": "41.0", "errorThreshold": "80"}]},
        [{"key": "AX1", "rule": "java:S3776", "severity": "CRITICAL", "message": "Cognitive complexity 31",
          "component": "acme-farm_farm-field-api:src/main/java/Crops.java", "line": 88}])
    [r] = SonarQube(None, opener=fake, token="tok").collect("code-quality", SPEC, TARGET, "pr")
    assert r.state == "failed" and "projectKey=acme-farm_farm-field-api" in fake.urls[0] and "pullRequest=12" in fake.urls[0]
    titles = [f.title for f in r.findings]
    assert titles[0].startswith("Quality gate failed: new_coverage 41.0 (limit 80)")
    assert "Cognitive complexity 31 (src/main/java/Crops.java:88)" in titles[1] and r.findings[1].severity == "high"
    out = evaluate("pr", {"code-quality": SPEC}, [r], [])
    assert out["status"] == "blocked" and len(out["blocking"]) == 2


def test_a_passing_or_unanalysed_sonarqube_gate():
    [ok] = SonarQube(None, opener=FakeSonar({"status": "OK"}), token="tok").collect("code-quality", SPEC, TARGET, "pr")
    assert ok.state == "passed" and ok.findings == []
    [wait] = SonarQube(None, opener=FakeSonar({"status": "NONE"}), token="tok").collect("code-quality", SPEC, TARGET, "pr")
    assert wait.state == "pending"
    [none] = SonarQube(None, opener=FakeSonar({}), token="").collect("code-quality", SPEC, TARGET, "pr")
    assert none.state == "missing" and "VALXEL_SONAR_TOKEN" in none.message


def test_the_platform_policy_registers_semgrep_sonarqube_and_gitleaks(settings):
    q = settings.quality
    assert "Semgrep*" in q.suites["sast"]["match"]["tools"]
    # Coverage of new code is built in (diff-cover, no vendor); SonarQube is optional.
    cq = q.suites["code-quality"]
    assert cq["source"] == "github-checks" and cq["match"]["checks"] == ["code-quality*"]
    assert cq["required"] and cq["min_coverage"] == 80
    assert q.suites["sonarqube"]["source"] == "sonarqube" and not q.suites["sonarqube"]["required"]
    assert q.sources["sonarqube"].endswith(":SonarQube")
    assert q.suites["secrets-history"]["match"]["checks"] == ["gitleaks*"]
    assert q.upkeep["fix_within_days"]["critical"] == 7 and q.upkeep["floors"]["phi"]["fix_within_days"]["high"] == 14

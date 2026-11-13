"""Design knowledge: the architecture record, contracts and catalog reach agents, the design check
compares an HLD with them before G4, and released decisions are proposed back to the record."""

import json
import shutil
from pathlib import Path

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from valxel_sdlc.config import load_settings
from valxel_sdlc.engine import FeatureEngine
from valxel_sdlc.governance.sources import GitHubSource
from valxel_sdlc.knowledge import design_review, render
from valxel_sdlc.knowledge.contracts import parse_openapi
from valxel_sdlc.knowledge.record import mark_superseded, parse_adr, render_adr
from valxel_sdlc.services import build_services

ROOT = Path(__file__).resolve().parents[1]
API = "acme-clinic/cln-intake-api"
OPENAPI = """openapi: 3.1.0
info: {title: CLN intake API, version: 1.4.0}
paths:
  /fhir/Patient: {get: {}, post: {}}
  /fhir/Patient/{id}: {get: {}}
"""


@pytest.fixture
def gov_root(tmp_path):
    root = tmp_path / "governance"
    shutil.copytree(ROOT / "examples" / "governance", root)
    return root


def make(tmp_path, gov_root, openapi=OPENAPI):
    s = load_settings(mode="stub", database_url="", artifact_dir=tmp_path / "a",
                      governance_source="local", governance_root=gov_root)
    svc = build_services(s)
    if openapi:
        svc.github.files[(API, "main", "api/openapi.yaml")] = openapi
    return FeatureEngine(svc, InMemorySaver()), svc


def until_gate(eng, fid, gate):
    for _ in range(20):
        p = [x for x in eng.pending(fid) if x["type"] == "gate"]
        if not p:
            return None
        if p[0]["gate"] == gate:
            return p[0]
        eng.decide(fid, p[0]["interrupt_id"], {"decision": "approve", "approver": "maria"})
    return None


def pack(adrs=(), apis=None, in_design=()):
    return {"architecture": {"repo": "acme-clinic/cln-governance", "sha": "abc1234", "adrs": list(adrs), "overview": ""},
            "contracts": {"apis": apis or {}}, "capabilities": {"live": [], "in_design": list(in_design)}, "errors": {}}


def adr(i, status="accepted", repos=(API,), superseded_by=""):
    return {"id": i, "title": f"Decision {i}", "status": status, "repos": list(repos), "supersedes": [],
            "superseded_by": superseded_by, "decision": "", "lane": "CLN", "path": f"architecture/decisions/{i}.md"}


HLD = """# HLD
## Builds on
- ADR-CLN-0001: FHIR
## ADRs
- ADR-CLN-FEAT-9-1: Add consents to the intake API
## Affected repos
- acme-clinic/cln-intake-api (provider)
## Contracts
- acme-clinic/cln-intake-api: 1.4.0 -> 1.5.0, adds GET /fhir/Consent
"""


# --------------------------------------------------------------------------- record


def test_adrs_round_trip_and_supersede():
    text = render_adr({"id": "ADR-CLN-0009", "title": "Consents are FHIR Consent resources", "status": "accepted",
                       "date": "2026-11-03", "repos": [API], "feature": "CLN-FEAT-9"},
                      decision="Store consents as FHIR Consent.")
    a = parse_adr(text, "architecture/decisions/ADR-CLN-0009.md")
    assert a["id"] == "ADR-CLN-0009" and a["status"] == "accepted" and a["repos"] == [API]
    assert a["decision"] == "Store consents as FHIR Consent." and a["feature"] == "CLN-FEAT-9"
    old = parse_adr(mark_superseded(text, "ADR-CLN-0010"))
    assert old["status"] == "superseded" and old["superseded_by"] == "ADR-CLN-0010"
    assert "Store consents as FHIR Consent." in mark_superseded(text, "ADR-CLN-0010")
    assert parse_adr("no front matter") is None


def test_openapi_contract():
    c = parse_openapi(OPENAPI)
    assert c["version"] == "1.4.0" and "POST /fhir/Patient" in c["operations"]
    with pytest.raises(ValueError):
        parse_openapi("just: yaml")


def test_github_source_lists_a_directory():
    class Resp:
        def __init__(self, body):
            self.body = body

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return self.body.encode()

    class Opener:
        def open(self, req, timeout=0):
            assert "/contents/architecture/decisions?ref=sha1" in req.full_url
            return Resp(json.dumps([{"type": "file", "path": "architecture/decisions/ADR-1.md"},
                                    {"type": "dir", "path": "architecture/decisions/old"}]))

    tokens = type("T", (), {"token": lambda self, org: "t"})()
    src = GitHubSource(tokens, opener=Opener())
    assert src.list("o", "r", "architecture/decisions", "sha1") == ["architecture/decisions/ADR-1.md"]


# --------------------------------------------------------------------------- what agents see


def test_agents_get_the_record_contracts_and_catalog(tmp_path, gov_root):
    _, svc = make(tmp_path, gov_root)
    snap = svc.snapshot("CLN")
    p = svc.knowledge.collect(snap)
    ids = [a["id"] for a in p["architecture"]["adrs"]]
    assert ids == ["ADR-CLN-0001", "ADR-CLN-0002", "ADR-CLN-0003", "ADR-CLN-0004"]
    assert p["contracts"]["apis"][API]["version"] == "1.4.0"
    assert p["errors"] == {}
    text = render(p)
    assert "ADR-CLN-0001 (accepted): FHIR R4 at every system boundary" in text
    assert f"{API}: CLN intake API 1.4.0" in text and "GET /fhir/Patient" in text
    assert "## Architecture overview" in text and "Capabilities" not in text  # nothing invented
    # Bounded, decisions first.
    short = render(p, max_chars=400)
    assert len(short) <= 401 and "ADR-CLN-0001" in short


def test_the_architecture_agent_prompt_carries_the_existing_design(tmp_path, gov_root):
    eng, svc = make(tmp_path, gov_root)
    seen = []
    real = svc.gateway.complete

    def spy(tags, layers, **kw):
        seen.append((tags.phase, layers.product_context))
        return real(tags, layers, **kw)

    svc.gateway.complete = spy
    fid = eng.start(product_id="CLN", title="Consent capture at intake")["feature_id"]
    until_gate(eng, fid, "g4")
    arch = [ctx for phase, ctx in seen if phase == "architecture"]
    assert arch and "ADR-CLN-0003" in arch[0] and "CLN intake API 1.4.0" in arch[0]


# --------------------------------------------------------------------------- design check


def test_a_design_that_builds_on_current_decisions_and_real_versions_passes():
    r = design_review(HLD, pack([adr("ADR-CLN-0001")], {API: {"version": "1.4.0", "operations": []}}), "CLN-FEAT-9")
    assert r["findings"] == []
    assert r["builds_on"][0]["id"] == "ADR-CLN-0001"
    assert r["contracts"] == [{"repo": API, "current": "1.4.0", "next": "1.5.0", "breaking": False,
                               "note": f"{API}: 1.4.0 -> 1.5.0, adds GET /fhir/Consent"}]


@pytest.mark.parametrize("hld, adrs, version, expected", [
    (HLD.replace("## Builds on\n- ADR-CLN-0001: FHIR\n", ""), [adr("ADR-CLN-0001")], "1.4.0",
     "HLD has no 'Builds on' section"),
    (HLD, [adr("ADR-CLN-0001", "superseded", superseded_by="ADR-CLN-0007")], "1.4.0",
     "Builds on ADR-CLN-0001, which was superseded by ADR-CLN-0007"),
    (HLD.replace("ADR-CLN-0001", "ADR-CLN-0042"), [adr("ADR-CLN-0001")], "1.4.0",
     "Builds on ADR-CLN-0042, which is not in the architecture record"),
    (HLD, [adr("ADR-CLN-0001")], "1.6.2", "HLD assumes acme-clinic/cln-intake-api is at 1.4.0, but its OpenAPI"),
    (HLD.replace("1.4.0 -> 1.5.0", "1.4.0 -> 2.0.0"), [adr("ADR-CLN-0001")], "1.4.0",
     "1.4.0 -> 2.0.0 is a breaking change: add a Migration section"),
    (HLD.replace("1.4.0 -> 1.5.0, adds", "adds"), [adr("ADR-CLN-0001")], "1.4.0", "must state the versions: '1.4.0 -> <next>'"),
    (HLD.replace("## Contracts\n- acme-clinic/cln-intake-api: 1.4.0 -> 1.5.0, adds GET /fhir/Consent\n",
                 "## Contracts\n- acme-clinic/cln-web: none\n"), [adr("ADR-CLN-0001")], "1.4.0",
     "Contracts section doesn't say what happens to its API (currently 1.4.0)"),
    (HLD.replace("Add consents to the intake API", "Consents by phone; Supersedes ADR-CLN-0005"),
     [adr("ADR-CLN-0001")], "1.4.0", "supersedes ADR-CLN-0005, which is not in the architecture record"),
])
def test_design_check_findings(hld, adrs, version, expected):
    r = design_review(hld, pack(adrs, {API: {"version": version, "operations": []}}), "CLN-FEAT-9")
    assert any(expected in f for f in r["findings"]), r["findings"]


def test_breaking_change_with_a_migration_and_explicit_supersession():
    hld = (HLD.replace("1.4.0 -> 1.5.0", "1.4.0 -> 2.0.0")
           .replace("Add consents to the intake API", "Consent is required before intake; Supersedes ADR-CLN-0005")
           + "## Migration\n- Expand with /v2, move cln-web, then remove /v1\n")
    r = design_review(hld, pack([adr("ADR-CLN-0001"), adr("ADR-CLN-0005")], {API: {"version": "1.4.0", "operations": []}}))
    assert r["findings"] == []
    assert r["contracts"][0]["breaking"] is True
    assert r["supersedes"] == [{"adr": "ADR-CLN-FEAT-9-1", "supersedes": "ADR-CLN-0005", "title": "Decision ADR-CLN-0005"}]


def test_reviewers_see_related_decisions_and_designs_in_flight():
    other = {"id": "CAP-CLN-X", "name": "Referrals", "feature": "CLN-FEAT-3", "status": "in_design",
             "contracts": [{"repo": API, "current": "1.4.0", "next": "1.5.0"}]}
    r = design_review(HLD, pack([adr("ADR-CLN-0001"), adr("ADR-CLN-0003")], {API: {"version": "1.4.0", "operations": []}},
                                in_design=[other]), "CLN-FEAT-9")
    assert [x["id"] for x in r["related"]] == ["ADR-CLN-0003"]  # on an affected repo, not mentioned
    assert r["in_flight"][0]["feature"] == "CLN-FEAT-3" and r["in_flight"][0]["repos"] == [API]


def test_a_repo_without_an_openapi_document_is_reported_not_failed():
    r = design_review(HLD, pack([adr("ADR-CLN-0001")], {API: {"repo": API, "missing": True}}))
    assert r["findings"] == [] and r["contracts"][0]["note"] == "no OpenAPI document"


# --------------------------------------------------------------------------- the pipeline


def test_g4_shows_the_review_and_approved_designs_are_visible_to_other_features(tmp_path, gov_root):
    eng, svc = make(tmp_path, gov_root)
    first = eng.start(product_id="CLN", title="Consent capture at intake")["feature_id"]
    g4 = until_gate(eng, first, "g4")
    review = g4["design_review"]
    assert {b["id"] for b in review["builds_on"]} >= {"ADR-CLN-0001", "ADR-CLN-0003"}
    assert review["contracts"][0] == {"repo": API, "current": "1.4.0", "next": "1.5.0", "breaking": False,
                                      "note": review["contracts"][0]["note"]}
    assert review["record"]["repo"] == "acme-clinic/cln-governance"
    eng.decide(first, g4["interrupt_id"], {"decision": "approve", "approver": "arch"})
    [entry] = [c for c in svc.catalog.lookup("CLN") if c.get("feature") == first]
    assert entry["status"] == "in_design" and entry["contracts"][0]["next"] == "1.5.0"

    second = eng.start(product_id="CLN", title="Referral triage")["feature_id"]
    g4b = until_gate(eng, second, "g4")
    assert g4b["design_review"]["in_flight"][0]["feature"] == first


def test_release_proposes_the_decisions_to_the_architecture_record(tmp_path, gov_root):
    eng, svc = make(tmp_path, gov_root)
    fid = eng.start(product_id="CLN", title="Consent capture at intake")["feature_id"]
    v = eng.auto_drive(fid)
    assert v["status"] == "done"
    [entry] = [c for c in svc.catalog.lookup("CLN") if c.get("feature") == fid]
    assert entry["status"] == "live" and entry["adrs"][0]["id"] == f"ADR-{fid}-1"
    [pr] = [p for p in svc.github.prs.values() if p["repo"] == "acme-clinic/cln-governance" and "architecture" in p["branch"]]
    assert f"ADR-{fid}-1" in pr["body"] and "1.4.0 -> 1.5.0" in pr["body"]
    branch = pr["branch"]
    adr_text = svc.github.files[("acme-clinic/cln-governance", branch, f"architecture/decisions/ADR-{fid}-1.md")]
    a = parse_adr(adr_text)
    assert a["status"] == "accepted" and a["feature"] == fid and API in a["repos"]
    overview = svc.github.files[("acme-clinic/cln-governance", branch, "architecture/overview.md")]
    assert f"{fid}: Consent capture at intake" in overview and overview.startswith("# Clinic records")
    assert any(e["kind"] == "architecture_record_pr" for e in v["events"])


def test_products_without_a_record_still_work(tmp_path, gov_root):
    eng, svc = make(tmp_path, gov_root, openapi=None)
    fid = eng.start(product_id="FARM", title="Offline crop log")["feature_id"]
    g4 = until_gate(eng, fid, "g4")
    assert g4["design_review"]["findings"] == [] and g4["design_review"]["builds_on"] == []


# --------------------------------------------------------------------------- features the product has


def test_the_tool_knows_every_feature_it_ran_and_where_it_got_to(tmp_path, gov_root):
    eng, svc = make(tmp_path, gov_root)
    done = eng.start(product_id="CLN", title="Consent capture at intake")["feature_id"]
    eng.auto_drive(done)
    stopped = eng.start(product_id="CLN", title="Referral triage")["feature_id"]
    p = eng.pending(stopped)[0]
    eng.decide(stopped, p["interrupt_id"], {"decision": "abort", "approver": "po", "feedback": "not now"})
    waiting = eng.start(product_id="CLN", title="Lab results viewer")["feature_id"]
    feats = {f["feature_id"]: f for f in eng.feature_summaries("CLN")}
    assert feats[done]["status"] == "released"
    assert feats[stopped]["status"] == "stopped"
    assert feats[waiting]["status"] == "in_progress" and feats[waiting]["stage"] == "G1"
    text = render(svc.knowledge.collect(svc.snapshot("CLN")))
    assert f"- {done} (released): Consent capture at intake" in text
    assert f"- {waiting} (in progress, at G1): Lab results viewer" in text
    caps = svc.catalog.lookup("CLN")
    assert [c["status"] for c in caps if c.get("feature") == done] == ["live"]


def test_duplicates_are_caught_whatever_state_the_original_is_in(tmp_path, gov_root):
    from valxel_sdlc.duplicates import DuplicateIntent

    eng, _ = make(tmp_path, gov_root)
    first = eng.start(product_id="CLN", title="Consent capture at intake",
                      description="Capture patient consent during intake")["feature_id"]
    with pytest.raises(DuplicateIntent) as exc:  # in progress
        eng.create(product_id="CLN", title="Capture consent when patients register",
                   description="Record patient consent at registration")
    assert exc.value.matches[0]["id"] == first
    eng.auto_drive(first)
    with pytest.raises(DuplicateIntent, match="already released"):  # released
        eng.create(product_id="CLN", title="Consent capture at intake")
    other = eng.create(product_id="CLN", title="Consent capture at intake",
                       not_duplicate_reason="Paper consent forms scanned at the front desk")
    assert eng.view(other)["pending"][0]["related_features"][0]["id"] == first
    assert eng.create(product_id="CLN", title="Referral triage")  # unrelated: no friction

"""A new product starts with no repos: its first feature's HLD proposes them, G4 approves them,
the provision step creates them, and the rest of the pipeline works on them."""

import re
import shutil
from pathlib import Path

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from valxel_sdlc.config import load_settings
from valxel_sdlc.engine import FeatureEngine
from valxel_sdlc.governance import GovernanceError
from valxel_sdlc.services import build_services

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def gov_root(tmp_path):
    root = tmp_path / "governance"
    shutil.copytree(ROOT / "examples" / "governance", root)
    return root


def set_repos(gov_root, block: str):
    path = gov_root / "acme-clinic" / "cln-governance" / "product.yaml"
    text = re.sub(r"^repos:\n(?:- .*\n(?:  .*\n)*)+", block, path.read_text(), flags=re.M)
    path.write_text(text)


def make(tmp_path, gov_root):
    s = load_settings(mode="stub", database_url="", artifact_dir=tmp_path / "a",
                      governance_source="local", governance_root=gov_root)
    svc = build_services(s)
    return FeatureEngine(svc, InMemorySaver()), svc


def test_first_feature_proposes_creates_and_builds_the_first_repo(tmp_path, gov_root):
    set_repos(gov_root, "repos: []\nrepo_prefix: cln-\n")
    eng, svc = make(tmp_path, gov_root)
    assert svc.snapshot("CLN").product.repos == ()

    seen = {}
    fid = eng.start(product_id="CLN", title="Patient registration")["feature_id"]
    while True:
        p = [x for x in eng.pending(fid) if x["type"] == "gate"]
        if not p:
            break
        seen[p[0]["gate"]] = p[0]
        eng.decide(fid, p[0]["interrupt_id"], {"decision": "approve", "approver": "lead"})
    v = eng.auto_drive(fid)
    assert v["status"] == "done"

    new = "acme-clinic/cln-patient-registration-service"
    [plan] = seen["g4"]["new_repos"]
    assert plan["repo"] == new and plan["template"] == "valxel-tools/template-service"
    assert new in svc.github.repos  # created on the golden path
    assert any(pr["repo"] == new for pr in svc.github.prs.values())  # the build worked in it
    gov_pr = [pr for pr in svc.github.prs.values() if pr["repo"] == "acme-clinic/cln-governance" and "repos" in pr["branch"]]
    assert gov_pr and new in gov_pr[0]["body"]


def test_no_repos_needs_a_prefix(tmp_path, gov_root):
    set_repos(gov_root, "repos: []\n")
    _, svc = make(tmp_path, gov_root)
    with pytest.raises(GovernanceError, match="set repo_prefix"):
        svc.snapshot("CLN")


def test_prefix_must_be_a_name_prefix(tmp_path, gov_root):
    set_repos(gov_root, "repos: []\nrepo_prefix: CLN_\n")
    _, svc = make(tmp_path, gov_root)
    with pytest.raises(GovernanceError, match="repo_prefix 'CLN_'"):
        svc.snapshot("CLN")

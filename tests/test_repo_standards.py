"""Every managed repo stays on the standards in config/repos.yaml: `vrixel repos check` reports
drift, `vrixel repos apply` fixes what can be fixed."""

import json
import re
import shutil
from io import BytesIO
from pathlib import Path
from urllib.error import HTTPError

import pytest

from vrixel_sdlc import repo_standards as rs
from vrixel_sdlc.cli import main
from vrixel_sdlc.config import load_settings
from vrixel_sdlc.integrations.github import GitHubRest, StubGitHub, ruleset_body
from vrixel_sdlc.services import build_services

ROOT = Path(__file__).resolve().parents[1]
FILES = (".github/CODEOWNERS", ".github/dependabot.yml", "SECURITY.md")


@pytest.fixture
def svc(tmp_path):
    root = tmp_path / "governance"
    shutil.copytree(ROOT / "examples" / "governance", root)
    return build_services(load_settings(mode="stub", database_url="", artifact_dir=tmp_path / "a",
                                        governance_source="local", governance_root=root))


def existing(gh: StubGitHub, repo: str, *, ghas: bool = True, files: bool = True, **state) -> None:
    """A repo as someone set it up by hand: GitHub's defaults, nothing from the standards."""
    gh.repos[repo] = {"state": {"allow_squash_merge": True, "allow_merge_commit": True, "allow_rebase_merge": True,
                                "delete_branch_on_merge": False, "default_branch": "main", "has_wiki": True,
                                "has_projects": True, "has_discussions": False, "is_template": False,
                                "available": {"code_scanning": ghas, "secret_scanning": ghas}, **state}}
    if files:
        for f in FILES:
            gh.files[(repo, "main", f)] = "x"


def target(svc, repo: str) -> rs.Target:
    [t] = rs.targets(svc, [repo])
    return t


def test_managed_repos_come_from_governance_the_registry_and_the_platform_list(svc):
    ts = {t.repo: t for t in rs.targets(svc)}
    assert ts["vrixel-emr/emr-governance"].kind == "governance"
    assert ts["vrixel-emr/emr-intake-api"].kind == "lane"
    assert ts["vrixel-tools/template-service"].kind == "template" and ts["vrixel-tools/template-service"].plan["is_template"]
    assert ts["vrixel-tools/agentic-sdlc"].plan["ruleset"]["required_checks"] == ["test", "web"]
    assert ts["vrixel-tools/template-agent"].plan["ruleset"]["required_checks"] == ["unit", "evals", "CodeQL"]
    gov = ts["vrixel-emr/emr-governance"].plan
    assert gov["ruleset"]["required_checks"] == [] and gov["tag_ruleset"] == {}
    assert ts["vrixel-emr/emr-intake-api"].plan["ruleset"]["required_approvals"] == 2  # PHI floor


def test_apply_brings_a_hand_made_repo_on_standard(svc):
    gh = StubGitHub()
    repo = "vrixel-emr/emr-intake-api"
    existing(gh, repo)
    t = target(svc, repo)
    before = rs.reconcile(gh, t, apply=False)
    assert not before.ok
    items = {(d.area, d.item) for d in before.drift}
    assert ("settings", "delete_branch_on_merge") in items and ("settings", "allow_merge_commit") in items
    assert ("settings", "has_wiki") in items and ("ruleset", "vrixel-default-branch") in items
    assert ("tags", "vrixel-release-tags") in items and ("security", "secret_scanning") in items
    assert ("properties", "vrixel_data_class") in items

    after = rs.reconcile(gh, t, apply=True)
    assert after.ok and after.drift == [] and after.errors == []
    assert set(after.applied) >= {"settings", "security", "ruleset", "tags", "topics", "properties"}
    state = gh.repos[repo]["state"]
    assert state["delete_branch_on_merge"] is True and state["allow_auto_merge"] is True
    assert state["allow_merge_commit"] is False and state["has_wiki"] is False
    branch = state["rulesets"]["vrixel-default-branch"]
    rules = {r["type"]: r.get("parameters") for r in branch["rules"]}
    assert rules["pull_request"]["required_approving_review_count"] == 2
    assert [c["context"] for c in rules["required_status_checks"]["required_status_checks"]] == ["unit", "contract", "CodeQL"]
    assert {"actor_id": 5, "actor_type": "RepositoryRole", "bypass_mode": "pull_request"} in branch["bypass_actors"]
    assert gh.org_properties["vrixel-emr"] == ["vrixel_data_class", "vrixel_managed", "vrixel_product"]
    assert rs.reconcile(gh, t, apply=True).applied == []  # nothing left to do: safe to repeat


def test_without_advanced_security_codeql_is_not_required(svc):
    gh = StubGitHub()
    repo = "vrixel-tools/template-service"
    existing(gh, repo, ghas=False)
    rep = rs.reconcile(gh, target(svc, repo), apply=True)
    assert rep.ok and rep.errors == []
    unavailable = {d.item for d in rep.drift if d.fix == "unavailable"}
    assert unavailable == {"secret_scanning", "push_protection", "code_scanning_default_setup"}
    rules = {r["type"]: r.get("parameters") for r in gh.repos[repo]["state"]["rulesets"]["vrixel-default-branch"]["rules"]}
    assert [c["context"] for c in rules["required_status_checks"]["required_status_checks"]] == ["unit", "contract"]
    assert "code_scanning" not in rules
    assert gh.repos[repo]["state"]["is_template"] is True


def test_drift_after_someone_changes_a_setting_is_reported_and_fixed(svc):
    gh = StubGitHub()
    repo = "vrixel-emr/emr-governance"
    existing(gh, repo, files=False)
    t = target(svc, repo)
    rs.reconcile(gh, t, apply=True)
    gh.repos[repo]["state"]["delete_branch_on_merge"] = False  # someone turned it off
    rules = gh.repos[repo]["state"]["rulesets"]["vrixel-default-branch"]["rules"]
    next(r for r in rules if r["type"] == "pull_request")["parameters"]["require_code_owner_review"] = False
    rep = rs.reconcile(gh, t, apply=False)
    assert {(d.area, d.item, str(d.want)) for d in rep.drift if d.fix == "apply"} == {
        ("settings", "delete_branch_on_merge", "True"),
        ("ruleset", "vrixel-default-branch", "pull_request.require_code_owner_review")}
    # Missing files are reported, never written: they come with a pull request.
    assert {d.item for d in rep.drift if d.fix == "manual"} == {".github/CODEOWNERS", "SECURITY.md"}
    assert rs.reconcile(gh, t, apply=True).ok


def test_what_is_never_changed_automatically(svc):
    gh = StubGitHub()
    existing(gh, "vrixel-emr/emr-web", visibility="public")
    rep = rs.reconcile(gh, target(svc, "vrixel-emr/emr-web"), apply=True)
    assert ("visibility", "manual") in {(d.area, d.fix) for d in rep.drift}
    missing = rs.reconcile(gh, target(svc, "vrixel-emr/emr-intake-api"), apply=True)
    assert not missing.exists and not missing.ok and missing.applied == []
    existing(gh, "vrixel-apx/apx-governance", archived=True)
    assert rs.reconcile(gh, target(svc, "vrixel-apx/apx-governance"), apply=False).drift == []


def test_the_cli_reports_and_exits_nonzero_on_drift(svc, capsys, monkeypatch):
    gh = StubGitHub()
    existing(gh, "vrixel-tools/sdlc-governance")
    monkeypatch.setattr("vrixel_sdlc.cli.build_services", lambda settings: svc)
    assert main(["repos", "check", "--repo", "vrixel-tools/sdlc-governance"], github=gh) == 1
    assert "settings.delete_branch_on_merge" in capsys.readouterr().out
    assert main(["repos", "apply", "--repo", "vrixel-tools/sdlc-governance", "--json"], github=gh) == 0
    [r] = json.loads(capsys.readouterr().out)
    assert r["ok"] and "ruleset" in r["applied"]
    monkeypatch.delenv("VRIXEL_GITHUB_APP_ID", raising=False)
    with pytest.raises(SystemExit, match="GitHub App"):
        main(["repos", "check"])


# --------------------------------------------------------------------------- the GitHub API


class Resp(BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeApi:
    """GitHub's REST API as repo_state reads it: GitHub-shaped rulesets with ids and extra fields."""

    def __init__(self, *, ghas: bool):
        self.ghas, self.calls = ghas, []

    def open(self, req, timeout=None):
        m, path = req.get_method(), req.full_url.split("https://api.github.com", 1)[1]
        self.calls.append((m, path))
        repo = "/repos/o/r"
        if path == repo:
            sa = {"dependabot_security_updates": {"status": "enabled"}, "secret_scanning": {"status": "disabled"}}
            if self.ghas:
                sa["advanced_security"] = {"status": "enabled"}
            return Resp(json.dumps({"visibility": "private", "private": True, "allow_squash_merge": True,
                                    "delete_branch_on_merge": True, "default_branch": "main",
                                    "security_and_analysis": sa}).encode())
        if path == f"{repo}/vulnerability-alerts":
            return Resp(b"")
        if path == f"{repo}/code-scanning/default-setup":
            if not self.ghas:
                raise HTTPError(req.full_url, 403, "f", {}, BytesIO(b'{"message": "Advanced Security must be enabled"}'))
            return Resp(b'{"state": "configured"}')
        if path == f"{repo}/rulesets":
            return Resp(b'[{"id": 7, "name": "vrixel-default-branch", "source_type": "Repository"},'
                        b' {"id": 8, "name": "org-wide", "source_type": "Organization"}]')
        if path == f"{repo}/rulesets/7":
            body = ruleset_body({"required_checks": ["unit"], "admin_bypass": "pull_request"})
            body["id"] = 7
            body["rules"][2]["parameters"]["automatic_copilot_code_review_enabled"] = False  # extra field
            return Resp(json.dumps(body).encode())
        if path == f"{repo}/teams":
            return Resp(b'[{"slug": "tech-leads", "permission": "maintain"}]')
        if path == f"{repo}/properties/values":
            return Resp(b'[{"property_name": "vrixel_managed", "value": "true"}]')
        if path == f"{repo}/topics":
            return Resp(b'{"names": ["vrixel"]}')
        if path.startswith(f"{repo}/contents/"):
            if "SECURITY" in path:
                raise HTTPError(req.full_url, 404, "nf", {}, BytesIO(b"{}"))
            return Resp(json.dumps({"content": "eA=="}).encode())
        if re.fullmatch(r"/orgs/o/properties/schema", path):
            return Resp(b'[{"property_name": "vrixel_managed"}]')
        return Resp(b"{}")


class Tokens:
    def token(self, org):
        return "t"


def test_repo_state_reads_github():
    gh = GitHubRest(Tokens(), opener=FakeApi(ghas=True))
    st = gh.repo_state(repo="o/r")
    assert st["visibility"] == "private" and st["settings"]["delete_branch_on_merge"] is True
    assert st["security"]["dependabot_alerts"] and st["security"]["dependabot_security_updates"]
    assert st["security"]["code_scanning_default_setup"] and not st["security"]["secret_scanning"]
    assert st["available"] == {"code_scanning": True, "secret_scanning": True}
    assert list(st["rulesets"]) == ["vrixel-default-branch"]  # org rulesets are the org's business
    assert st["teams"] == {"tech-leads": "maintain"} and st["topics"] == ["vrixel"]
    assert st["files"]["SECURITY.md"] is None and st["files"][".github/CODEOWNERS"] == "x"
    # GitHub's extra fields in a ruleset are not drift; what the standards set is compared.
    want = ruleset_body({"required_checks": ["unit"], "admin_bypass": "pull_request"})
    assert rs._rules_differ(want, st["rulesets"]["vrixel-default-branch"]) == []
    assert rs._rules_differ(ruleset_body({"required_checks": ["unit", "contract"]}),
                            st["rulesets"]["vrixel-default-branch"]) == [
        "required_status_checks.required_status_checks", "bypass"]


def test_repo_state_without_advanced_security_and_org_properties():
    api = FakeApi(ghas=False)
    gh = GitHubRest(Tokens(), opener=api)
    st = gh.repo_state(repo="o/r")
    assert st["available"] == {"code_scanning": False, "secret_scanning": False}
    assert st["security"]["code_scanning_default_setup"] is False
    gh.ensure_org_properties(org="o", names=["vrixel_managed", "vrixel_product"])
    assert ("PATCH", "/orgs/o/properties/schema") in api.calls
    gh.enable_security(repo="o/r", secret_scanning=False, push_protection=False, code_scanning_default_setup=False,
                       dependabot_security_updates=True)
    assert ("PATCH", "/repos/o/r/code-scanning/default-setup") not in api.calls

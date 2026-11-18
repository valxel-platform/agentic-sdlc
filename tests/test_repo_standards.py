"""Every managed repo stays on the standards in config/repos.yaml: `valxel repos check` reports
drift, `valxel repos apply` fixes what can be fixed."""

import json
import re
import shutil
from io import BytesIO
from pathlib import Path
from urllib.error import HTTPError

import pytest

from valxel_sdlc import repo_standards as rs
from valxel_sdlc.cli import main
from valxel_sdlc.config import load_settings
from valxel_sdlc.integrations.github import GitHubRest, StubGitHub, ruleset_body
from valxel_sdlc.services import build_services

ROOT = Path(__file__).resolve().parents[1]
FILES = (".github/CODEOWNERS", ".github/dependabot.yml", "SECURITY.md")


@pytest.fixture
def svc(tmp_path):
    root = tmp_path / "governance"
    shutil.copytree(ROOT / "examples" / "governance", root)
    return build_services(load_settings(mode="stub", database_url="", artifact_dir=tmp_path / "a",
                                        governance_source="local", governance_root=root))


def existing(gh: StubGitHub, repo: str, *, ghas: bool = True, files: bool = True, workflows: bool = True,
             **state) -> None:
    """A repo as someone set it up by hand: GitHub's defaults, nothing from the standards (but the
    platform's managed workflows, unless `workflows` is False: they have tests of their own)."""
    if workflows:
        for path, name in {".github/workflows/security.yml": "security.yml",
                           ".github/workflows/secrets.yml": "governance-secrets.yml",
                           ".github/workflows/datasets.yml": "governance-datasets.yml",
                           ".github/scripts/datasets.py": "datasets.py",
                           ".github/scripts/odcs-json-schema-v3.2.0.json": "odcs-json-schema-v3.2.0.json",
                           ".github/scripts/report.py": "report.py",
                           ".github/scripts/conformance.py": "conformance.py",
                           "contracts/events/valxel.events.v1.schema.json": "contracts/valxel.events.v1.schema.json",
                           "contracts/events/valxel.flag_cohorts.v1.schema.json":
                               "contracts/valxel.flag_cohorts.v1.schema.json"}.items():
            gh.files[(repo, "main", path)] = rs.standard_file(name)
    gh.repos[repo] = {"state": {"visibility": "public", "allow_squash_merge": True, "allow_merge_commit": True, "allow_rebase_merge": True,
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
    assert ts["acme-clinic/cln-governance"].kind == "governance"
    assert ts["acme-clinic/cln-intake-api"].kind == "lane"
    assert ts["valxel-platform/template-service"].kind == "template" and ts["valxel-platform/template-service"].plan["is_template"]
    assert ts["valxel-platform/agentic-sdlc"].plan["ruleset"]["required_checks"] == ["test", "web"]
    assert ts["valxel-platform/agentic-sdlc"].plan["visibility"] == "public"
    assert all(t.plan["visibility"] == "public" for t in ts.values())  # every repo is public
    assert ts["valxel-platform/template-agent"].plan["ruleset"]["required_checks"] == ["unit", "evals", "CodeQL"]
    gov = ts["acme-clinic/cln-governance"].plan
    assert gov["ruleset"]["required_checks"] == [] and gov["tag_ruleset"] == {}
    # No GitHub review: people approve in the pipeline's gates, and pull requests merge on checks.
    rules = ts["acme-clinic/cln-intake-api"].plan["ruleset"]
    assert rules["required_approvals"] == 0 and not rules["code_owner_review"] and not rules["require_last_push_approval"]


def test_apply_brings_a_hand_made_repo_on_standard(svc):
    gh = StubGitHub()
    repo = "acme-clinic/cln-intake-api"
    existing(gh, repo)
    t = target(svc, repo)
    before = rs.reconcile(gh, t, apply=False)
    assert not before.ok
    items = {(d.area, d.item) for d in before.drift}
    assert ("settings", "delete_branch_on_merge") in items and ("settings", "allow_merge_commit") in items
    assert ("settings", "has_wiki") in items and ("ruleset", "valxel-default-branch") in items
    assert ("tags", "valxel-release-tags") in items and ("security", "secret_scanning") in items
    assert ("properties", "valxel_data_class") in items

    after = rs.reconcile(gh, t, apply=True)
    assert after.ok and after.drift == [] and after.errors == []
    assert set(after.applied) >= {"settings", "security", "ruleset", "tags", "topics", "properties"}
    state = gh.repos[repo]["state"]
    assert state["delete_branch_on_merge"] is True and state["allow_auto_merge"] is True
    assert state["allow_merge_commit"] is False and state["has_wiki"] is False
    branch = state["rulesets"]["valxel-default-branch"]
    rules = {r["type"]: r.get("parameters") for r in branch["rules"]}
    assert rules["pull_request"]["required_approving_review_count"] == 0  # pull requests only; checks decide
    assert [c["context"] for c in rules["required_status_checks"]["required_status_checks"]] == ["unit", "contract", "CodeQL", "valxel/gates"]
    assert {"actor_id": 5, "actor_type": "RepositoryRole", "bypass_mode": "pull_request"} in branch["bypass_actors"]
    assert gh.org_properties["acme-clinic"] == ["valxel_data_class", "valxel_managed", "valxel_product"]
    assert rs.reconcile(gh, t, apply=True).applied == []  # nothing left to do: safe to repeat


def test_without_advanced_security_codeql_is_not_required(svc):
    gh = StubGitHub()
    repo = "valxel-platform/template-service"
    existing(gh, repo, ghas=False)
    rep = rs.reconcile(gh, target(svc, repo), apply=True)
    assert rep.ok and rep.errors == []
    unavailable = {d.item for d in rep.drift if d.fix == "unavailable"}
    assert unavailable == {"secret_scanning", "push_protection", "code_scanning_default_setup"}
    rules = {r["type"]: r.get("parameters") for r in gh.repos[repo]["state"]["rulesets"]["valxel-default-branch"]["rules"]}
    assert [c["context"] for c in rules["required_status_checks"]["required_status_checks"]] == ["unit", "contract"]
    assert "code_scanning" not in rules
    assert gh.repos[repo]["state"]["is_template"] is True


def test_drift_after_someone_changes_a_setting_is_reported_and_fixed(svc):
    gh = StubGitHub()
    repo = "acme-clinic/cln-governance"
    existing(gh, repo, files=False)
    t = target(svc, repo)
    rs.reconcile(gh, t, apply=True)
    gh.repos[repo]["state"]["delete_branch_on_merge"] = False  # someone turned it off
    rules = gh.repos[repo]["state"]["rulesets"]["valxel-default-branch"]["rules"]
    next(r for r in rules if r["type"] == "pull_request")["parameters"]["required_approving_review_count"] = 1
    rep = rs.reconcile(gh, t, apply=False)
    assert {(d.area, d.item, str(d.want)) for d in rep.drift if d.fix == "apply"} == {
        ("settings", "delete_branch_on_merge", "True"),
        ("ruleset", "valxel-default-branch", "pull_request.required_approving_review_count")}
    # Missing files are reported, never written: they come with a pull request.
    assert {d.item for d in rep.drift if d.fix == "manual"} == {".github/CODEOWNERS", "SECURITY.md"}
    assert rs.reconcile(gh, t, apply=True).ok


def test_what_is_never_changed_automatically(svc):
    gh = StubGitHub()
    existing(gh, "acme-clinic/cln-web", visibility="private")
    rep = rs.reconcile(gh, target(svc, "acme-clinic/cln-web"), apply=True)
    assert ("visibility", "manual") in {(d.area, d.fix) for d in rep.drift}  # never flipped automatically
    missing = rs.reconcile(gh, target(svc, "acme-clinic/cln-intake-api"), apply=True)
    assert not missing.exists and not missing.ok and missing.applied == []
    existing(gh, "acme-pay/pay-governance", archived=True)
    assert rs.reconcile(gh, target(svc, "acme-pay/pay-governance"), apply=False).drift == []


def test_the_cli_reports_and_exits_nonzero_on_drift(svc, capsys, monkeypatch):
    gh = StubGitHub()
    existing(gh, "valxel-platform/sdlc-governance")
    monkeypatch.setattr("valxel_sdlc.cli.build_services", lambda settings: svc)
    assert main(["repos", "check", "--repo", "valxel-platform/sdlc-governance"], github=gh) == 1
    assert "settings.delete_branch_on_merge" in capsys.readouterr().out
    assert main(["repos", "apply", "--repo", "valxel-platform/sdlc-governance", "--json"], github=gh) == 0
    [r] = json.loads(capsys.readouterr().out)
    assert r["ok"] and "ruleset" in r["applied"]
    monkeypatch.delenv("VALXEL_GITHUB_APP_ID", raising=False)
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
        if path == f"{repo}/private-vulnerability-reporting":
            return Resp(b'{"enabled": true}')
        if path == f"{repo}/automated-security-fixes":
            return Resp(b'{"enabled": true, "paused": false}')
        if path == f"{repo}/code-scanning/default-setup":
            if not self.ghas:
                raise HTTPError(req.full_url, 403, "f", {}, BytesIO(b'{"message": "Advanced Security must be enabled"}'))
            return Resp(b'{"state": "configured"}')
        if path == f"{repo}/rulesets":
            return Resp(b'[{"id": 7, "name": "valxel-default-branch", "source_type": "Repository"},'
                        b' {"id": 8, "name": "org-wide", "source_type": "Organization"}]')
        if path == f"{repo}/rulesets/7":
            body = ruleset_body({"required_checks": ["unit"], "admin_bypass": "pull_request"})
            body["id"] = 7
            body["rules"][2]["parameters"]["automatic_copilot_code_review_enabled"] = False  # extra field
            return Resp(json.dumps(body).encode())
        if path == f"{repo}/teams":
            return Resp(b'[{"slug": "tech-leads", "permission": "maintain"}]')
        if path == f"{repo}/properties/values":
            return Resp(b'[{"property_name": "valxel_managed", "value": "true"}]')
        if path == f"{repo}/topics":
            return Resp(b'{"names": ["valxel"]}')
        if path.startswith(f"{repo}/contents/"):
            if "SECURITY" in path:
                raise HTTPError(req.full_url, 404, "nf", {}, BytesIO(b"{}"))
            return Resp(json.dumps({"content": "eA=="}).encode())
        if re.fullmatch(r"/orgs/o/properties/schema", path):
            return Resp(b'[{"property_name": "valxel_managed"}]')
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
    assert st["available"] == {"code_scanning": True, "secret_scanning": True, "rulesets": True}
    assert list(st["rulesets"]) == ["valxel-default-branch"]  # org rulesets are the org's business
    assert st["teams"] == {"tech-leads": "maintain"} and st["topics"] == ["valxel"]
    assert st["files"]["SECURITY.md"] is None and st["files"][".github/CODEOWNERS"] == "x"
    # GitHub's extra fields in a ruleset are not drift; what the standards set is compared.
    want = ruleset_body({"required_checks": ["unit"], "admin_bypass": "pull_request"})
    assert rs._rules_differ(want, st["rulesets"]["valxel-default-branch"]) == []
    assert rs._rules_differ(ruleset_body({"required_checks": ["unit", "contract"]}),
                            st["rulesets"]["valxel-default-branch"]) == [
        "required_status_checks.required_status_checks", "bypass"]


def test_repo_state_without_advanced_security_and_org_properties():
    api = FakeApi(ghas=False)
    gh = GitHubRest(Tokens(), opener=api)
    st = gh.repo_state(repo="o/r")
    assert st["available"] == {"code_scanning": False, "secret_scanning": False, "rulesets": True}
    assert st["security"]["code_scanning_default_setup"] is False
    gh.ensure_org_properties(org="o", names=["valxel_managed", "valxel_product"])
    assert ("PATCH", "/orgs/o/properties/schema") in api.calls
    gh.enable_security(repo="o/r", secret_scanning=False, push_protection=False, code_scanning_default_setup=False,
                       dependabot_security_updates=True)
    assert ("PATCH", "/repos/o/r/code-scanning/default-setup") not in api.calls
    assert ("PUT", "/repos/o/r/automated-security-fixes") in api.calls


def test_the_platform_app_keeps_its_merge_train_bypass_on_lane_repos(svc, monkeypatch):
    monkeypatch.setenv("VALXEL_GITHUB_APP_ID", "424242")
    gh = StubGitHub()
    existing(gh, "acme-clinic/cln-intake-api")
    existing(gh, "acme-clinic/cln-governance")
    for repo in ("acme-clinic/cln-intake-api", "acme-clinic/cln-governance"):
        rs.reconcile(gh, target(svc, repo), apply=True)
    lane = gh.repos["acme-clinic/cln-intake-api"]["state"]["rulesets"]["valxel-default-branch"]["bypass_actors"]
    gov = gh.repos["acme-clinic/cln-governance"]["state"]["rulesets"]["valxel-default-branch"]["bypass_actors"]
    assert {"actor_id": 424242, "actor_type": "Integration", "bypass_mode": "pull_request"} in lane
    assert all(a["actor_type"] != "Integration" for a in gov)  # governance changes are people's to merge


def test_missing_app_permissions_are_named():
    from valxel_sdlc.governance.sources import missing_permissions

    from valxel_sdlc.integrations.github import PROVISIONING_PERMISSIONS

    granted = {"administration": "write", "contents": "write", "repository_custom_properties": "read",
               "metadata": "read"}
    assert missing_permissions({"administration": "write", "repository_custom_properties": "write",
                                "organization_custom_properties": "admin", "metadata": "read"}, granted) == [
        "organization_custom_properties: admin", "repository_custom_properties: write"]
    # GitHub's names for the permissions (as an installation reports them).
    assert {"repository_custom_properties", "organization_custom_properties", "members"} <= set(PROVISIONING_PERMISSIONS)


def test_a_repo_that_fails_doesnt_stop_the_run(svc):
    class Broken(StubGitHub):
        def repo_state(self, *, repo):
            if repo.endswith("cln-governance"):
                raise RuntimeError("App lacks members: read")
            return super().repo_state(repo=repo)

    gh = Broken()
    reports = {r.repo: r for r in rs.run(svc, gh, apply=False)}
    assert reports["acme-clinic/cln-governance"].errors == ["RuntimeError: App lacks members: read"]
    assert len(reports) > 20  # the rest were still checked


def test_private_repos_on_github_free_have_no_rulesets(svc):
    gh = StubGitHub()
    repo = "acme-clinic/cln-governance"
    existing(gh, repo, visibility="private",
             available={"code_scanning": False, "secret_scanning": False, "rulesets": False})
    rep = rs.reconcile(gh, target(svc, repo), apply=True)
    assert rep.ok and rep.errors == [] and "ruleset" not in rep.applied and "settings" in rep.applied
    [d] = [d for d in rep.drift if d.area == "ruleset"]
    assert d.fix == "unavailable" and "GitHub Team" in d.line()
    # GitHub ignores auto-merge on a private repo without a paid plan: it's reported, not retried.
    gh.repos[repo]["state"]["allow_auto_merge"] = False
    again = rs.reconcile(gh, target(svc, repo), apply=False)
    assert again.ok and [d.fix for d in again.drift if d.item == "allow_auto_merge"] == ["unavailable"]


def test_codeowners_at_the_root_counts_and_public_repos_take_vulnerability_reports(svc):
    gh = StubGitHub()
    repo = "acme-clinic/cln-governance"
    existing(gh, repo, files=False)
    gh.files[(repo, "main", "CODEOWNERS")] = "* @acme-clinic/product-owners"
    gh.files[(repo, "main", "SECURITY.md")] = "x"
    rep = rs.reconcile(gh, target(svc, repo), apply=True)
    assert rep.ok and rep.drift == []
    assert gh.repos[repo]["state"]["security"]["private_vulnerability_reporting"] is True
    existing(gh, "acme-clinic/cln-web", visibility="private")
    rep = rs.reconcile(gh, target(svc, "acme-clinic/cln-web"), apply=False)
    assert [d.fix for d in rep.drift if d.item == "private_vulnerability_reporting"] == ["unavailable"]


def test_lane_repos_require_the_gates_status_from_the_platform_app_only(svc, monkeypatch):
    monkeypatch.setenv("VALXEL_GITHUB_APP_ID", "4242")
    ts = {t.repo: t for t in rs.targets(svc)}
    lane = ts["acme-clinic/cln-intake-api"].plan["ruleset"]
    assert lane["gates_check"] == "valxel/gates"
    rules = {r["type"]: r.get("parameters") for r in ruleset_body(lane)["rules"]}
    assert {"context": "valxel/gates", "integration_id": 4242} in rules["required_status_checks"]["required_status_checks"]
    # The pipeline doesn't deliver to governance, templates or the tool, so nothing would post it there.
    for repo in ("acme-clinic/cln-governance", "valxel-platform/template-service", "valxel-platform/agentic-sdlc"):
        assert not ts[repo].plan["ruleset"]["gates_check"]

    # A ruleset whose gates check anyone could set is drift.
    gh = StubGitHub()
    repo = "acme-clinic/cln-intake-api"
    existing(gh, repo)
    t = ts[repo]
    rs.reconcile(gh, t, apply=True)
    rules = gh.repos[repo]["state"]["rulesets"]["valxel-default-branch"]["rules"]
    checks = next(r for r in rules if r["type"] == "required_status_checks")["parameters"]["required_status_checks"]
    next(c for c in checks if c["context"] == "valxel/gates").pop("integration_id")
    rep = rs.reconcile(gh, t, apply=False)
    assert ("ruleset", "valxel-default-branch") in {(d.area, d.item) for d in rep.drift}


def test_a_dependency_graph_that_is_off_is_reported_for_a_person(svc):
    """No API turns it on, and dependency review fails without it: tell someone exactly where."""
    ts = {t.repo: t for t in rs.targets(svc)}
    gh = StubGitHub()
    repo = "acme-clinic/cln-intake-api"
    existing(gh, repo)
    rs.reconcile(gh, ts[repo], apply=True)
    gh.repos[repo].setdefault("state", {}).setdefault("security", {})["dependency_graph"] = False
    rep = rs.reconcile(gh, ts[repo], apply=True)
    d = next(d for d in rep.drift if d.item == "dependency_graph")
    assert d.fix == "manual" and "Settings > Advanced Security > Dependency graph" in d.needs


def test_managed_workflows_that_differ_or_are_missing_come_back_by_pull_request(svc, monkeypatch):
    gh = StubGitHub()
    lane, gov = "acme-clinic/cln-intake-api", "acme-clinic/cln-governance"
    existing(gh, lane, workflows=False)
    existing(gh, gov, workflows=False)
    gh.files[(lane, "main", ".github/workflows/security.yml")] = "name: security\njobs: {}\n"  # someone trimmed it
    rep = rs.reconcile(gh, target(svc, lane), apply=False)
    assert [(d.area, d.item, d.have, d.fix) for d in rep.drift if d.area == "workflows"] == [
        ("workflows", ".github/scripts/conformance.py", "missing", "apply"),
        ("workflows", ".github/scripts/report.py", "missing", "apply"),
        ("workflows", ".github/workflows/security.yml", "differs", "apply"),
        ("workflows", "contracts/events/valxel.events.v1.schema.json", "missing", "apply"),
        ("workflows", "contracts/events/valxel.flag_cohorts.v1.schema.json", "missing", "apply")]
    # A governance repo has no code: the secrets scan and the data-contract check are managed there.
    grep = rs.reconcile(gh, target(svc, gov), apply=False)
    assert [(d.item, d.have) for d in grep.drift if d.area == "workflows"] == [
        (".github/scripts/datasets.py", "missing"), (".github/scripts/odcs-json-schema-v3.2.0.json", "missing"),
        (".github/workflows/datasets.yml", "missing"), (".github/workflows/secrets.yml", "missing")]
    # Its datasets.py is the evaluator's own module, so CI and G4 check contracts the same way.
    assert rs.standard_file("datasets.py") == (Path(rs.__file__).parent / "datasets.py").read_text()

    after = rs.reconcile(gh, target(svc, lane), apply=True)
    assert "managed files" in after.applied and after.ok
    pending = [d for d in after.drift if d.area == "workflows"]
    assert len(pending) == 5 and all(d.fix == "pending" and "pull request" in d.have for d in pending)
    pr = next(p for p in gh.prs.values() if p["repo"] == lane and p["branch"].startswith("valxel/managed-files-"))
    branch = pr["branch"]
    assert gh.files[(lane, branch, ".github/workflows/security.yml")] == rs.standard_file("security.yml")
    assert gh.files[(lane, branch, ".github/scripts/report.py")] == rs.standard_file("report.py")
    assert "agentic-sdlc" in pr["body"]
    # Once it merges, the repo is on standard again.
    pr["state"] = "merged"
    gh.files[(lane, "main", ".github/workflows/security.yml")] = rs.standard_file("security.yml")
    gh.files[(lane, "main", ".github/scripts/report.py")] = rs.standard_file("report.py")
    gh.files[(lane, "main", ".github/scripts/conformance.py")] = rs.standard_file("conformance.py")
    for name in ("valxel.events.v1", "valxel.flag_cohorts.v1"):
        gh.files[(lane, "main", f"contracts/events/{name}.schema.json")] = rs.standard_file(f"contracts/{name}.schema.json")
    assert not [d for d in rs.reconcile(gh, target(svc, lane), apply=False).drift if d.area == "workflows"]
    # The platform's copy changes later: a new branch and pull request, never the merged one again.
    original = rs.standard_file
    monkeypatch.setattr(rs, "standard_file", lambda name: original(name) + ("# v2\n" if name == "report.py" else ""))
    rs.reconcile(gh, target(svc, lane), apply=True)
    [newer] = [p for p in gh.prs.values() if p["repo"] == lane and p["state"] == "open"
               and p["branch"].startswith("valxel/managed-files-")]
    assert newer["branch"] != branch and gh.files[(lane, newer["branch"], ".github/scripts/report.py")].endswith("# v2\n")


def test_the_managed_workflows_pin_actions_by_commit():
    import re

    for name in ("security.yml", "governance-secrets.yml", "governance-datasets.yml"):
        text = rs.standard_file(name)
        uses = re.findall(r"uses:\s*(\S+)", text)
        assert uses and all(re.search(r"@[0-9a-f]{40}$", u) for u in uses), uses


def test_semgrep_runs_with_metrics_off_and_gitleaks_says_what_it_found():
    text = rs.standard_file("security.yml")
    [cmd] = [ln for ln in text.splitlines() if ln.strip().startswith("semgrep scan")]
    assert "--metrics off" in cmd and "--config auto" not in cmd  # auto needs metrics on
    assert "safe.directory" in text  # the semgrep container runs as another user
    for name in ("security.yml", "governance-secrets.yml"):
        assert "--verbose" in rs.standard_file(name)


def test_the_managed_workflows_are_prettier_clean():
    """Repos with `prettier --check .` (template-web, template-acceptance) check these files too:
    one space before an inline comment, as Prettier writes YAML."""
    import re

    for name in ("security.yml", "governance-secrets.yml", "governance-datasets.yml"):
        assert not [ln for ln in rs.standard_file(name).splitlines() if re.search(r"\S {2,}#", ln)], name



def _report_module():
    import importlib.util
    from importlib.resources import files

    path = files("valxel_sdlc") / "standard_files" / "report.py"
    spec = importlib.util.spec_from_file_location("valxel_report", str(path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_report_py_is_managed_and_the_tools_own_copy_matches():
    from pathlib import Path

    std = rs.load_standards(Path("config/repos.yaml")) if hasattr(rs, "load_standards") else None
    managed = (std or {}).get("managed_files") if isinstance(std, dict) else None
    import yaml

    managed = managed or yaml.safe_load(Path("config/repos.yaml").read_text())["defaults"]["managed_files"]
    assert managed[".github/scripts/report.py"] == "report.py"
    assert Path(".github/scripts/report.py").read_text() == rs.standard_file("report.py")


def test_new_code_coverage_names_the_untested_lines(tmp_path):
    import json

    report = _report_module()
    p = tmp_path / "new.json"
    p.write_text(json.dumps({"total_num_lines": 40, "total_num_violations": 10, "src_stats": {
        "src/app/orders.py": {"violation_lines": [12, 13, 14, 20]},
        "src/app/api.py": {"violation_lines": []},
        "src/main/java/acme/Pay.java": {"violation_lines": [3, 5, 6, 7, 8, 9]}}}))
    state, text, detail = report.new_code(str(p), 80)
    assert state == "failure" and text.startswith("Coverage on new code: 75.0% (at least 80%).")
    assert "10 of 40 changed lines untested: orders.py 12-14, 20; Pay.java 3, 5-9" in text
    assert "`src/app/orders.py`: lines 12-14, 20" in detail and "api.py" not in detail
    assert report.new_code(str(p), 75)[0] == "success"
    # What the quality gate reads from the status
    from valxel_sdlc.quality.sources import _summary

    assert _summary(text[:140])["coverage"] == 75.0
    empty = tmp_path / "none.json"
    empty.write_text(json.dumps({"total_num_lines": 0, "total_num_violations": 0, "src_stats": {}}))
    assert report.new_code(str(empty), 80)[0] == "success"  # docs or config only
    assert report.new_code(str(tmp_path / "missing.json"), 80)[0] == "failure"


def test_a_low_new_code_coverage_status_fails_the_code_quality_suite(settings):
    from valxel_sdlc.quality import SuiteResult, evaluate

    spec = settings.quality.suites["code-quality"]
    low = SuiteResult(suite="code-quality", kind="test", stage="pr", state="failed", repo="acme/web", coverage=62.0,
                      message="code-quality / new code")
    out = evaluate("pr", {"code-quality": spec}, [low], [])
    assert out["status"] == "failed" and any("62.0% is below 80%" in r for r in out["test_failures"])
    missing = evaluate("pr", {"code-quality": spec}, [], [])
    assert missing["status"] == "failed"  # required: a repo that doesn't report it can't merge


def test_report_py_reads_junit_jacoco_and_cobertura_and_refuses_xml_entities(tmp_path):
    report = _report_module()
    (tmp_path / "a.xml").write_text('<testsuites><testsuite tests="5" failures="1" errors="0" skipped="1"/>'
                                    '<testsuite tests="3" failures="0" errors="1" skipped="0"/></testsuites>')
    (tmp_path / "b.xml").write_text('<?xml version="1.0"?><testsuite tests="2" failures="0" skipped="0">'
                                    '<testsuite tests="9"/></testsuite>')  # a root suite counts once
    assert report.junit([str(tmp_path / "*.xml")]) == {"passed": 7, "failed": 2, "skipped": 1}
    jac = tmp_path / "jacoco.xml"
    jac.write_text('<?xml version="1.0"?><!DOCTYPE report PUBLIC "-//JACOCO//DTD Report 1.1//EN" "report.dtd">'
                   '<report name="t"><package name="a"><counter type="LINE" missed="50" covered="50"/></package>'
                   '<counter type="LINE" missed="1" covered="3"/></report>')
    assert report.jacoco(str(jac)) == 75.0  # the report-level counter, not a package's
    (tmp_path / "nolines.xml").write_text('<report name="t"><counter type="BRANCH" missed="1" covered="1"/></report>')
    assert report.jacoco(str(tmp_path / "nolines.xml")) is None
    cob = tmp_path / "coverage.xml"
    cob.write_text('<?xml version="1.0"?><coverage line-rate="0.835" branch-rate="0"><packages/></coverage>')
    assert round(report.cobertura(str(cob)), 1) == 83.5
    evil = tmp_path / "evil.xml"
    evil.write_text('<?xml version="1.0"?><!DOCTYPE r [<!ENTITY x SYSTEM "file:///etc/passwd">]><r>&x;</r>')
    with pytest.raises(ValueError, match="entities aren't allowed"):
        report.elements(str(evil))


def test_report_py_posts_only_over_https(tmp_path, monkeypatch):
    report = _report_module()
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    monkeypatch.setenv("GITHUB_API_URL", "file:///etc")
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(tmp_path / "s.md"))
    (tmp_path / "n.json").write_text('{"total_num_lines": 0}')
    monkeypatch.setattr("sys.argv", ["report.py", "--context", "c", "--new-code", str(tmp_path / "n.json"),
                                     "--repo", "o/r", "--sha", "abc"])
    with pytest.raises(SystemExit, match="must be an https URL"):
        report.main()


class _FakeHTTPS:
    calls: list = []
    status = 201

    def __init__(self, host, port, timeout):
        self.host, self.port = host, port

    def request(self, method, path, body, headers):
        _FakeHTTPS.calls.append({"host": self.host, "port": self.port, "method": method, "path": path,
                                 "body": __import__("json").loads(body), "auth": headers["Authorization"]})

    def getresponse(self):
        return type("R", (), {"status": _FakeHTTPS.status, "read": lambda self: b"{}"})()


def _run_report(monkeypatch, tmp_path, *argv, api="https://api.github.com"):
    report = _report_module()
    monkeypatch.setattr(report.http.client, "HTTPSConnection", _FakeHTTPS)
    monkeypatch.setenv("GITHUB_TOKEN", "tok")
    monkeypatch.setenv("GITHUB_API_URL", api)
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(tmp_path / "summary.md"))
    monkeypatch.setattr("sys.argv", ["report.py", "--repo", "o/r", "--sha", "abc", *argv])
    _FakeHTTPS.calls = []
    return report.main()


def test_report_py_posts_test_results_and_new_code_as_statuses(tmp_path, monkeypatch):
    import json

    (tmp_path / "junit.xml").write_text('<testsuite tests="10" failures="0" errors="0" skipped="1"/>')
    (tmp_path / "summary.json").write_text(json.dumps({"total": {"lines": {"pct": 91.25}}}))
    code = _run_report(monkeypatch, tmp_path, "--context", "unit / report", "--junit", str(tmp_path / "junit.xml"),
                       "--istanbul", str(tmp_path / "summary.json"))
    [call] = _FakeHTTPS.calls
    assert code == 0 and call["path"] == "/repos/o/r/statuses/abc" and call["auth"] == "Bearer tok"
    assert call["host"] == "api.github.com" and call["port"] == 443
    assert call["body"]["state"] == "success" and call["body"]["description"] == "9 passed, 0 failed, 1 skipped. Coverage: 91.2%"

    # A GitHub Enterprise Server API (a path and a port), and an eval run with a pass rate.
    (tmp_path / "evals.xml").write_text('<testsuite tests="20" failures="1" errors="0" skipped="0"/>')
    code = _run_report(monkeypatch, tmp_path, "--context", "evals / report", "--junit", str(tmp_path / "evals.xml"),
                       "--min-pass-rate", "0.9", api="https://ghe.example:8443/api/v3/")
    [call] = _FakeHTTPS.calls
    assert code == 0 and call["port"] == 8443 and call["path"] == "/api/v3/repos/o/r/statuses/abc"
    assert "Pass rate: 95.0% (at least 90%)." in call["body"]["description"]

    (tmp_path / "new.json").write_text(json.dumps({"total_num_lines": 10, "total_num_violations": 5,
                                                   "src_stats": {"src/a.py": {"violation_lines": [1, 2, 3, 7, 9]}}}))
    code = _run_report(monkeypatch, tmp_path, "--context", "code-quality / new code", "--new-code", str(tmp_path / "new.json"))
    [call] = _FakeHTTPS.calls
    assert code == 1 and call["body"]["state"] == "failure" and "a.py 1-3, 7, 9" in call["body"]["description"]
    assert "- `src/a.py`: lines 1-3, 7, 9" in (tmp_path / "summary.md").read_text()

    _FakeHTTPS.status = 403
    try:
        with pytest.raises(SystemExit, match="HTTP 403"):
            _run_report(monkeypatch, tmp_path, "--context", "c", "--new-code", str(tmp_path / "new.json"))
    finally:
        _FakeHTTPS.status = 201


def test_report_py_without_a_token_only_prints(tmp_path, monkeypatch, capsys):
    report = _report_module()
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(tmp_path / "s.md"))
    monkeypatch.setattr("sys.argv", ["report.py", "--context", "unit / report", "--junit", str(tmp_path / "none*.xml")])
    assert report.main() == 1  # no tests ran
    assert "0 passed, 0 failed, 0 skipped." in capsys.readouterr().out
    for read in (report.jacoco, report.cobertura, report.istanbul):
        assert read(str(tmp_path / "missing.xml")) is None and read("") is None


def test_must_pass_cases_fail_the_status_whatever_the_pass_rate(tmp_path):
    """A knowledge repo's retrieval evals allow some questions to miss, never a leak."""
    import subprocess
    import sys
    from importlib.resources import files

    cases = [("recall: a", True)] * 18 + [("recall: b", False), ("leak: warfarin", True)]
    junit = tmp_path / "retrieval.xml"

    def write(leak_ok: bool) -> None:
        body = "".join(
            f'<testcase name="{n}">' + ("" if ok else '<failure message="x"/>') + "</testcase>"
            for n, ok in cases[:-1] + [("leak: warfarin", leak_ok)])
        junit.write_text(f'<testsuite tests="20" failures="{1 + (not leak_ok)}">{body}</testsuite>')

    script = str(files("valxel_sdlc") / "standard_files" / "report.py")

    def state(*extra: str) -> tuple[int, str]:
        env = {"PATH": "/usr/bin:/bin", "GITHUB_STEP_SUMMARY": str(tmp_path / "summary.md")}
        r = subprocess.run([sys.executable, script, "--context", "retrieval / report", "--junit", str(junit),
                            "--min-pass-rate", "0.85", *extra], capture_output=True, text=True, env=env)
        return r.returncode, r.stdout

    write(leak_ok=True)
    assert state("--must-pass", "leak:*")[0] == 0  # 19 of 20 pass: above the bar, and no leak
    write(leak_ok=False)
    assert state()[0] == 0  # 18 of 20 still clears 85%...
    code, out = state("--must-pass", "leak:*", "--must-pass", "deleted:*")
    assert code == 1 and "Must pass, failed: leak: warfarin." in out  # ...but a leak never does
    report = _report_module()
    assert report.failed_cases([str(junit)]) == ["recall: b", "leak: warfarin"]


def test_repos_never_checks_the_example_products_on_github(monkeypatch, tmp_path):
    """The tool's own registry lists fictional products: on GitHub, the deployment's config is required."""
    monkeypatch.delenv("VALXEL_CONFIG_DIR", raising=False)
    monkeypatch.setenv("VALXEL_GOVERNANCE_SOURCE", "github")
    with pytest.raises(SystemExit, match="VALXEL_CONFIG_DIR"):
        main(["repos", "check"], github=StubGitHub())


def test_a_deployment_config_keeps_only_what_it_changes(tmp_path):
    """Files a deployment doesn't carry come from the tool: no stale copies of the golden path."""
    import shutil

    from valxel_sdlc.config import _repo_root, config_file, load_settings

    for name in ("registry.yaml", "models.yaml"):
        shutil.copy(_repo_root() / "config" / name, tmp_path / name)
    settings = load_settings(mode="stub", database_url="", config_dir=tmp_path)
    assert config_file(tmp_path, "repos.yaml") == _repo_root() / "config" / "repos.yaml"
    assert config_file(tmp_path, "registry.yaml") == tmp_path / "registry.yaml"
    assert "channel" in settings.repos.roles and settings.config_dir == tmp_path


def test_a_contract_another_repo_owns_is_kept_in_step_with_that_repo(svc):
    gh = StubGitHub()
    lane, sink = "acme-clinic/cln-intake-api", "valxel-shared-services/ss-audit-sink"
    existing(gh, lane)
    owner_path = "src/audit_sink/valxel.audit.v1.schema.json"
    t = target(svc, lane)
    # Unreadable owner (not there yet): nothing reported, nothing touched.
    assert not [d for d in rs.managed_drift(gh, t) if "audit" in d.item]
    gh.files[(sink, "main", owner_path)] = '{"x-version": "1.0.0"}\n'
    [d] = [d for d in rs.managed_drift(gh, t) if "audit" in d.item]
    assert d.item == "contracts/audit/valxel.audit.v1.schema.json" and d.have == "missing"
    rs.reconcile(gh, t, apply=True)
    pr = next(p for p in gh.prs.values() if p["repo"] == lane and p["branch"].startswith("valxel/managed-files-"))
    assert gh.files[(lane, pr["branch"], "contracts/audit/valxel.audit.v1.schema.json")] == '{"x-version": "1.0.0"}\n'
    # The owner keeps its own file and gets no copy of it.
    assert rs.remote_source("github:valxel-shared-services/ss-audit-sink/" + owner_path) == (sink, owner_path)

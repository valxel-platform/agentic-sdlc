"""RC1 of the engineering review: one list of where a session may connect (finding 4)."""

from pathlib import Path

from valxel_sdlc import egress_proxy as ep
from valxel_sdlc import repo_standards, sandbox, template_contract

ROOT = Path(__file__).resolve().parents[1]


def test_the_shell_sandbox_and_the_egress_proxy_use_config_egress_yaml(tmp_path, monkeypatch):
    listed = sandbox.egress(str(ROOT / "config/egress.yaml"))
    assert {"downloads.gradle.org", "services.gradle.org"} <= set(listed)  # Gradle's wrapper needs both
    assert ep.allow_list({}, str(ROOT / "config/egress.yaml")) == tuple(listed)
    custom = tmp_path / "egress.yaml"
    custom.write_text("allowed:\n  - Artifacts.Acme.com\n")
    monkeypatch.setenv("VALXEL_EGRESS_CONFIG", str(custom))
    assert sandbox.egress() == ["artifacts.acme.com"]
    assert ep.allow_list({"VALXEL_EGRESS_ALLOW": "extra.acme.com"}) == ("artifacts.acme.com", "extra.acme.com")


def test_no_built_in_fallback_list(tmp_path, caplog):
    assert not hasattr(sandbox, "DEFAULT_EGRESS") and not hasattr(ep, "DEFAULT_ALLOW")
    assert sandbox.egress(str(tmp_path / "missing.yaml")) == []  # fails closed, and says so
    assert "sessions may reach nothing" in caplog.text


class GH:
    def __init__(self, files):
        self.files = files

    def get_file(self, *, repo, path, ref):
        return self.files.get((repo, path))


CONTRACT = {"shared_copies": [{"source": "valxel-platform/agentic-sdlc:config/egress.yaml",
                               "copy": "valxel-platform/platform-foundation:gitops/apps/agent-sessions/egress.yaml",
                               "says": "the cluster allows what the tool allows"}]}
SRC = ("valxel-platform/agentic-sdlc", "config/egress.yaml")
COPY = ("valxel-platform/platform-foundation", "gitops/apps/agent-sessions/egress.yaml")


def test_a_copy_that_drifts_is_reported():
    same = GH({SRC: "allowed: [a]\n", COPY: "allowed: [a]\n"})
    assert template_contract.shared_copy_check(same, CONTRACT) == []
    drifted = template_contract.shared_copy_check(GH({SRC: "allowed: [a, b]\n", COPY: "allowed: [a]\n"}), CONTRACT)
    assert [f["problem"] for f in drifted] == [
        "gitops/apps/agent-sessions/egress.yaml differs from valxel-platform/agentic-sdlc's config/egress.yaml: "
        "copy it from there"]
    missing = template_contract.shared_copy_check(GH({SRC: "x"}), CONTRACT)
    assert missing[0]["problem"] == "gitops/apps/agent-sessions/egress.yaml doesn't exist"


def test_repos_check_fails_on_a_drifted_copy(monkeypatch):
    monkeypatch.setattr(template_contract, "contract", lambda: CONTRACT)
    [rep] = repo_standards.copy_reports(GH({SRC: "allowed: [a, b]\n", COPY: "allowed: [a]\n"}))
    assert rep.repo == "valxel-platform/platform-foundation" and not rep.ok
    assert repo_standards.copy_reports(GH({SRC: "x", COPY: "x"})) == []
    assert repo_standards.copy_reports(GH({SRC: "y", COPY: "x"}), only=["valxel-platform/agentic-sdlc"]) == []


def test_the_contract_lists_the_egress_copy():
    c = template_contract.contract()
    assert {(e["source"], e["copy"]) for e in c["shared_copies"]} >= {
        ("valxel-platform/agentic-sdlc:config/egress.yaml",
         "valxel-platform/platform-foundation:gitops/apps/agent-sessions/egress.yaml")}

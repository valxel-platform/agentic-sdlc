"""The designer sees the product (attachments and the design record), and the architect reads the code."""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

from valxel_sdlc import design_record as dr
from valxel_sdlc import workspace
from valxel_sdlc.evaluators.checks import _code_read
from valxel_sdlc.graph.phase import ATTACHMENT_PHASES
from valxel_sdlc.runtime.base import AgentTask
from valxel_sdlc.runtime.claude_code import session_options

UX = """## Screens
- S1: Ready list (web: acme/web)
## Components
- DataTable: existing
- ReadyBadge: new: nothing shows check-in status today
- Toast: new
- Banner: existing
"""
RECORD = {"present": True, "components": {"DataTable": "tables (acme/web)", "Button": "actions (acme/web)"}}


def test_the_designer_reads_attachments():
    assert "design" in ATTACHMENT_PHASES


def test_a_design_uses_the_record_or_says_why_not():
    found = dr.check(UX, RECORD)
    assert "Component Toast is new: say why no existing component fits" in found
    assert any(f.startswith("Component Banner is marked existing but isn't in design/components.md") for f in found)
    assert not any("DataTable" in f or "ReadyBadge" in f for f in found)
    no_section = "## Screens\n- S1: Ready list (web: acme/web)\n"
    assert "No Components section" in dr.check(no_section, RECORD)[0]
    assert dr.check(no_section, {}) == []  # no record yet: nothing to hold it to


def test_the_record_is_rendered_and_updated_after_a_release():
    pack = {"present": True, "repo": "acme/gov", "sha": "abc1234",
            "files": {"tokens": "# Tokens\n- Accent: blue", "components": "# Components\n- DataTable: tables",
                      "screens": "", "patterns": ""}}
    text = dr.render(pack)
    assert "Design record (acme/gov@abc1234)" in text and "- DataTable: tables" in text
    assert "no design record yet" in dr.render({})
    screens = dr.with_screens("# Screens\n- None yet: ...\n", "CLN-FEAT-3", UX)
    assert screens == "# Screens\n- CLN-FEAT-3-S1: Ready list (web: acme/web) [CLN-FEAT-3]\n"
    assert dr.with_screens(screens, "CLN-FEAT-3", UX) == screens  # a rerun replaces its own lines
    comps = dr.with_components("# Components\n- DataTable: tables\n", "CLN-FEAT-3", UX)
    assert "- ReadyBadge: nothing shows check-in status today [CLN-FEAT-3]" in comps and "Toast" in comps


def test_a_new_product_starts_with_a_record(services):
    from valxel_sdlc.inception import Vision
    from valxel_sdlc.inception.setup import governance_files

    v = Vision(name="Aqua Watch", org="acme-water", vision="Farms lose crops.")
    from valxel_sdlc.inception.run import draft

    b = draft(services, v, "brief")
    files = governance_files(v, b.text, "", "", "sunil")
    assert {"design/tokens.md", "design/components.md", "design/screens.md", "design/patterns.md"} <= set(files)


def test_an_existing_product_gets_a_drafted_record_as_a_pull_request(services):
    out = dr.draft_for_existing(services, "FARM")
    assert out["files"] == ["design/components.md", "design/patterns.md", "design/screens.md", "design/tokens.md"]
    assert out["pr"]["url"] and out["read"] == {}  # stub mode: nothing checked out
    assert dr.split_files("=== tokens ===\n# T\n- a\n=== screens ===\n# S\n") == {"tokens": "# T\n- a\n", "screens": "# S\n"}


def test_a_checkout_is_read_only_and_released(tmp_path, monkeypatch):
    calls = []

    def run(cmd, **kw):
        calls.append(cmd)
        if cmd[1] == "clone":
            Path(cmd[-1]).mkdir(parents=True)
            (Path(cmd[-1]) / "app.py").write_text("print(1)")
        return SimpleNamespace(stdout="abcdef1234\n")

    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(workspace.shutil, "which", lambda _: "/usr/bin/git")
    gh = SimpleNamespace(tokens=SimpleNamespace(token=lambda org: "SECRET"),
                         _call=lambda m, repo, p: {"size": 10 if repo != "acme/huge" else 10**7})
    svc = SimpleNamespace(settings=SimpleNamespace(mode="live", artifact_dir=tmp_path), github=gh)
    co = workspace.checkout(svc, ["acme/api", "acme/huge"], key="F-1-architecture")
    assert co["repos"]["acme/api"] == {"sha": "abcdef1234"} and "larger than" in co["repos"]["acme/huge"]["skipped"]
    f = Path(co["root"]) / "api" / "app.py"
    assert not (f.stat().st_mode & 0o222)  # read-only
    assert any(c[:4] == ["git", "-C", str(Path(co["root"]) / "api"), "remote"] for c in calls)  # the token is dropped
    text = workspace.describe(co)
    assert "acme/api @ abcdef1" in text and "Not read: acme/huge" in text
    workspace.release(co)
    assert not Path(co["root"]).exists()
    assert workspace.checkout(SimpleNamespace(settings=SimpleNamespace(mode="stub")), ["a/b"], key="x") is None


def test_a_reading_session_may_only_read(services):
    from valxel_sdlc.gateway import CallTags, PromptLayers

    role = services.settings.role("architect")
    tags = CallTags(product_id="X", feature_id="X-1", phase="architecture", role="architect", data_class="general",
                    workspace="w", prompt_version="architecture@1")
    task = AgentTask(role="architect", phase="architecture", tags=tags, cwd="/tmp/co", read_only=True,
                     layers=PromptLayers(tools="", skills="", constitution="", product_context="", feature_context="",
                                         tail="t"))
    opts = session_options(task, role, "anthropic/claude", cwd=None, plugin_dirs=[])
    assert opts["cwd"] == "/tmp/co" and opts["allowed_tools"] == ["Read", "Grep", "Glob"]
    assert "Write" in opts["disallowed_tools"] and "Bash" in opts["disallowed_tools"]


def test_an_affected_repo_the_architect_didnt_read_must_say_why():
    repos = [{"name": "acme/api", "role": "provider"}, {"name": "acme/web", "role": "consumer"}]
    ctx = {"repos": repos, "code_read": {"repos": {"acme/api": {"sha": "abc"}, "acme/web": {"skipped": "too large"}}}}
    assert "acme/web is affected but wasn't read" in _code_read("## Agent notes\n", repos, ctx)[0]
    assert _code_read("## Agent notes\n- Not read: acme/web: too large\n", repos, ctx) == []
    assert _code_read("", repos, {"repos": repos}) == []  # no checkout: nothing to hold it to

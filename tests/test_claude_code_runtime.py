"""The Claude Code session keeps Claude Code's own prompt, reads settings from the repository
only, and loads the installed plugins."""

from vrixel_sdlc.config import RoleConfig
from vrixel_sdlc.gateway import PromptLayers
from vrixel_sdlc.runtime.base import AgentTask
from vrixel_sdlc.runtime.claude_code import (
    APPEND_LIMIT_BYTES,
    plugin_dirs_from,
    session_options,
)

ROLE = RoleConfig(runtime="claude_code", model="anthropic/claude-sonnet-5-5", max_turns=40, max_call_usd=10.0)


def task(feature="PRD and HLD", schema=None):
    layers = PromptLayers(tools="[]", skills="# Role: coder\n\nSKILL", constitution="VX-1",
                          product_context="product", feature_context=feature, tail="Implement T1")
    return AgentTask(role="coder", phase="build", tags=None, layers=layers, schema=schema, max_budget_usd=4.0)


def test_the_layers_are_appended_to_claude_codes_own_prompt():
    opts = session_options(task(), ROLE, "anthropic/claude-sonnet-5-5", cwd="/work/repo", plugin_dirs=[])
    sp = opts["system_prompt"]
    assert sp["type"] == "preset" and sp["preset"] == "claude_code" and sp["exclude_dynamic_sections"] is True
    assert sp["append"] == "# Role: coder\n\nSKILL\n\nVX-1\n\nproduct\n\nPRD and HLD"
    assert "[]" not in sp["append"]  # L1: Claude Code brings its own tools
    assert opts["model"] == "claude-sonnet-5-5" and opts["cwd"] == "/work/repo"
    assert opts["max_turns"] == 40 and opts["max_budget_usd"] == 4.0
    assert "output_format" not in opts


def test_settings_come_from_the_repository_only():
    assert session_options(task(), ROLE, "anthropic/x", cwd=None, plugin_dirs=[])["setting_sources"] == ["project"]


def test_a_schema_asks_for_structured_output():
    schema = {"type": "object"}
    opts = session_options(task(schema=schema), ROLE, "anthropic/x", cwd=None, plugin_dirs=[])
    assert opts["output_format"] == {"type": "json_schema", "schema": schema}


def test_installed_plugins_are_found_and_loaded(tmp_path):
    for name, part in [("engineering", "skills"), ("design", ".claude-plugin"), ("notes", None)]:
        (tmp_path / name).mkdir()
        if part:
            (tmp_path / name / part).mkdir()
    (tmp_path / "README.md").write_text("not a plugin")
    dirs = plugin_dirs_from(str(tmp_path))
    assert dirs == [str((tmp_path / "design").resolve()), str((tmp_path / "engineering").resolve())]
    opts = session_options(task(), ROLE, "anthropic/x", cwd=None, plugin_dirs=dirs)
    assert opts["plugins"] == [{"type": "local", "path": d} for d in dirs]
    assert plugin_dirs_from(None) == [] and plugin_dirs_from(str(tmp_path / "missing")) == []


def test_an_oversized_prompt_is_reported_before_the_session_fails(caplog):
    session_options(task(feature="x" * (APPEND_LIMIT_BYTES + 1)), ROLE, "anthropic/x", cwd=None, plugin_dirs=[])
    assert "argument limit" in caplog.text

"""The plugin skills agents are given exist: ours in plugins/vrixel, Anthropic's in the pinned
knowledge-work-plugins (checked when KNOWLEDGE_WORK_PLUGINS points at a checkout, as in CI and the
image build). Live mode refuses to start while one is missing."""

import os
import re
import shutil
from pathlib import Path

import pytest

from vrixel_sdlc.agents.context import load_skill, missing_skills, skill_path
from vrixel_sdlc.agents.roles import used_skills
from vrixel_sdlc.services import check_skills

ROOT = Path(__file__).resolve().parents[1]
OURS = ROOT / "plugins"


def test_every_skill_named_in_code_is_one_live_mode_checks():
    named = set()
    for path in (ROOT / "src").rglob("*.py"):
        for group in re.findall(r"skills=\(([^)]*)\)", path.read_text()):
            named |= set(re.findall(r'"([a-z-]+:[a-z0-9-]+)"', group))
    assert named <= used_skills(), sorted(named - used_skills())


def test_our_critic_skills_are_in_the_vrixel_plugin():
    ours = {n for n in used_skills() if n.startswith("vrixel:")}
    assert ours and missing_skills(ours, str(OURS)) == []
    for name in ours:
        text = skill_path(name, str(OURS)).read_text()
        assert re.search(rf"^name: {name.split(':')[1]}$", text, re.M) and "description:" in text


def test_a_command_style_skill_resolves(tmp_path):
    (tmp_path / "pm" / "commands").mkdir(parents=True)
    (tmp_path / "pm" / "commands" / "brainstorm.md").write_text("# Brainstorm")
    assert skill_path("pm:brainstorm", str(tmp_path)).name == "brainstorm.md"
    assert skill_path("pm:other", str(tmp_path)) is None


def test_live_mode_refuses_to_start_without_its_skills(tmp_path):
    with pytest.raises(RuntimeError, match="VRIXEL_SKILLS_DIR"):
        check_skills("")
    shutil.copytree(OURS / "vrixel", tmp_path / "vrixel")
    with pytest.raises(RuntimeError, match="engineering:code-review"):
        check_skills(str(tmp_path))


def test_a_missing_skill_is_a_placeholder_outside_live_mode(monkeypatch):
    monkeypatch.delenv("VRIXEL_SKILLS_DIR", raising=False)
    load_skill.cache_clear()
    assert "Placeholder" in load_skill("engineering:debug")
    load_skill.cache_clear()


@pytest.mark.skipif(not os.environ.get("KNOWLEDGE_WORK_PLUGINS"), reason="needs a knowledge-work-plugins checkout")
def test_the_pinned_plugins_have_every_skill_we_use(tmp_path):
    upstream = Path(os.environ["KNOWLEDGE_WORK_PLUGINS"])
    for plugin in ("product-management", "engineering", "design"):
        shutil.copytree(upstream / plugin, tmp_path / plugin)
    shutil.copytree(OURS / "vrixel", tmp_path / "vrixel")
    check_skills(str(tmp_path))

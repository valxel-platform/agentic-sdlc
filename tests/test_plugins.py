"""The plugin pin is config: it agrees with the image, a config that moves it gets its own copy,
`on_demand` decides who sees every installed skill, and skills loaded beyond a phase's are recorded."""

from __future__ import annotations

import io
import re
import tarfile
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from valxel_sdlc import plugins, strict
from valxel_sdlc.agents.context import load_skill
from valxel_sdlc.config import load_settings
from valxel_sdlc.gateway.ledger import UsageRecord
from valxel_sdlc.plugins import PluginsConfig, load_plugins
from valxel_sdlc.runtime import claude_code, codex
from valxel_sdlc.telemetry import sessions

ROOT = Path(__file__).resolve().parents[1]
SHA_A, SHA_B = "a" * 40, "b" * 40


def test_the_dockerfile_builds_the_plugins_the_config_pins():
    pinned = load_plugins(ROOT / "config" / "plugins.yaml").ref
    docker = re.search(r"^ARG KNOWLEDGE_WORK_PLUGINS_REF=(\S+)$", (ROOT / "Dockerfile").read_text(), re.M).group(1)
    assert pinned and pinned == docker


def test_a_pin_must_be_a_commit(tmp_path):
    f = tmp_path / "plugins.yaml"
    f.write_text("knowledge_work_plugins: {ref: main}\n")
    with pytest.raises(ValueError, match="full commit sha"):
        load_plugins(f)
    assert load_plugins(tmp_path / "missing.yaml") == PluginsConfig()


def test_settings_carry_the_pin_and_the_skills_folder(monkeypatch, tmp_path):
    monkeypatch.setenv("VALXEL_SKILLS_DIR", str(tmp_path))
    s = load_settings()
    assert s.plugins.ref == load_plugins(ROOT / "config" / "plugins.yaml").ref
    assert s.skills_dir == str(tmp_path)


def test_on_demand_by_role():
    cfg = PluginsConfig(on_demand_default=True, on_demand_roles={"critic": False})
    assert cfg.on_demand("pm") and not cfg.on_demand("critic")
    assert PluginsConfig(on_demand_default=False).on_demand("pm") is False


def _archive(*plugin_names: str) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for p in plugin_names:
            data = f"---\nname: s\n---\n{p} at B\n".encode()
            info = tarfile.TarInfo(f"knowledge-work-plugins-{SHA_B}/{p}/skills/s/SKILL.md")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        mcp = b'{"mcpServers": {"slack": {}}}'
        info = tarfile.TarInfo(f"knowledge-work-plugins-{SHA_B}/engineering/.mcp.json")
        info.size = len(mcp)
        tar.addfile(info, io.BytesIO(mcp))
        lic = b"Apache"
        info = tarfile.TarInfo(f"knowledge-work-plugins-{SHA_B}/LICENSE")
        info.size = len(lic)
        tar.addfile(info, io.BytesIO(lic))
    return buf.getvalue()


def test_a_config_that_moves_the_pin_gets_its_own_copy_once(tmp_path):
    installed = tmp_path / "installed"
    installed.mkdir()
    (installed / "REF").write_text(SHA_A + "\n")
    calls = []

    def fetch(repo, ref):
        calls.append((repo, ref))
        return _archive(*plugins.UPSTREAM)

    same = SimpleNamespace(plugins=PluginsConfig(ref=SHA_A), skills_dir=str(installed))
    assert plugins.skills_dir_for(same, fetch=fetch, cache_root=tmp_path / "cache") == str(installed)
    assert calls == []

    moved = SimpleNamespace(plugins=PluginsConfig(ref=SHA_B), skills_dir=str(installed))
    out = plugins.skills_dir_for(moved, fetch=fetch, cache_root=tmp_path / "cache")
    assert out != str(installed) and plugins.installed_ref(out) == SHA_B
    assert "engineering at B" in (Path(out) / "engineering" / "skills" / "s" / "SKILL.md").read_text()
    assert not list(Path(out).rglob(".mcp.json"))  # laid out like the image's: skills only
    assert (Path(out) / "valxel").is_dir()
    assert plugins.skills_dir_for(moved, fetch=fetch, cache_root=tmp_path / "cache") == out
    assert calls == [("anthropics/knowledge-work-plugins", SHA_B)]  # fetched once

    unpinned = SimpleNamespace(plugins=PluginsConfig(), skills_dir=str(installed))
    assert plugins.skills_dir_for(unpinned, fetch=fetch) == str(installed)


def test_a_commit_missing_a_plugin_is_refused(tmp_path):
    with pytest.raises(RuntimeError, match="no design plugin"):
        plugins.materialise(PluginsConfig(ref=SHA_B), cache_root=tmp_path,
                            fetch=lambda repo, ref: _archive("product-management", "engineering"))


def test_the_phase_skills_come_from_the_configs_folder(tmp_path):
    for ref in ("one", "two"):
        d = tmp_path / ref / "engineering" / "skills" / "code-review"
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(f"review the {ref} way")
    load_skill.cache_clear()
    try:
        assert load_skill("engineering:code-review", str(tmp_path / "one")) == "review the one way"
        assert load_skill("engineering:code-review", str(tmp_path / "two")) == "review the two way"
    finally:
        load_skill.cache_clear()


def test_claude_code_gives_a_role_without_on_demand_no_plugins(tmp_path):
    for p in ("engineering", "valxel"):
        (tmp_path / p / "skills").mkdir(parents=True)
    settings = SimpleNamespace(skills_dir=str(tmp_path),
                               plugins=PluginsConfig(on_demand_roles={"critic": False}))
    rt = claude_code.ClaudeCodeRuntime(SimpleNamespace(settings=settings))
    assert len(rt.plugins_for("pm")) == 2 and rt.plugins_for("critic") == []


def test_claude_code_records_the_skills_a_session_invoked():
    block = SimpleNamespace(name="Skill", input={"skill": "engineering:testing-strategy"})
    other = SimpleNamespace(name="Read", input={"file_path": "x"})
    assert claude_code.skills_used(SimpleNamespace(content=[block, other])) == {"engineering:testing-strategy"}
    assert plugins.extra({"engineering:testing-strategy", "engineering:code-review"},
                         ("engineering:code-review",)) == ("engineering:testing-strategy",)


def test_codex_sessions_record_skill_files_they_read():
    events = ('{"type":"item.completed","item":{"type":"command_execution","command":"cat /h/plugins/cache/'
              'valxel-plugins/design/1.0.0/skills/ux-copy/SKILL.md"}}\n'
              '{"command":"cat /opt/valxel/plugins/product-management/commands/brainstorm.md"}\n'
              '{"command":"cat /repo/docs/skills/other/SKILL.md"}')
    assert plugins.skills_in_paths(events, codex.PLUGIN_NAMES) == {"design:ux-copy", "product-management:brainstorm"}


def test_codex_leaves_the_template_plugins_out_when_off_or_pinned_elsewhere(tmp_path, monkeypatch):
    installed = tmp_path / "installed"
    installed.mkdir()
    monkeypatch.setenv("VALXEL_SKILLS_DIR", str(installed))

    def rt(skills_dir, roles=None):
        s = SimpleNamespace(skills_dir=skills_dir, plugins=PluginsConfig(on_demand_roles=roles or {}))
        return codex.CodexRuntime(SimpleNamespace(settings=s), home_template="")

    assert rt(str(installed)).on_demand("pm")
    assert not rt(str(installed), {"pm": False}).on_demand("pm")
    assert not rt(str(tmp_path / "other")).on_demand("pm")

    template = tmp_path / "tpl"
    (template / "plugins").mkdir(parents=True)
    (template / "config.toml").write_text("x")
    home = Path(codex.session_home(str(template), tmp_path / "home", with_plugins=False))
    assert (home / "config.toml").exists() and not (home / "plugins").exists()


def test_extra_skills_reach_the_feature_sessions():
    base = dict(ts="2026-11-10T00:00:00", product_id="P", feature_id="F", phase="hld", role="architect",
                runtime="claude_code", provider="anthropic", model="anthropic/m", workspace="w", input_tokens=1,
                cache_creation_input_tokens=0, cache_read_input_tokens=0, output_tokens=1, cache_ttl="5m",
                prompt_version="v", trace_id="t1")
    rows = [UsageRecord(**base, extra_skills="design:ux-copy"), UsageRecord(**base, extra_skills="")]
    assert sessions(rows)[0]["extra_skills"] == ["design:ux-copy"]


def test_strict_mode_warns_when_the_image_and_the_config_disagree(tmp_path, monkeypatch):
    (tmp_path / "REF").write_text(SHA_A)
    settings = replace(load_settings(), mode="live", skills_dir=str(tmp_path),
                       plugins=PluginsConfig(ref=SHA_B), registry={})
    monkeypatch.setenv("VALXEL_CONFIG_REPO", "o/c")
    gaps = strict.known_gaps(SimpleNamespace(settings=settings, telemetry=SimpleNamespace(enabled=True)))
    assert any("plugins.yaml pins" in g for g in gaps)
    settings = replace(settings, plugins=PluginsConfig(ref=SHA_A))
    assert not any("plugins.yaml" in g for g in
                   strict.known_gaps(SimpleNamespace(settings=settings, telemetry=SimpleNamespace(enabled=True))))

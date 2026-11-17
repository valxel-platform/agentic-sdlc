"""Files coding agents read as instructions (CLAUDE.md, .claude/, AGENTS.md and the like): the next
coder session follows them, so a change to one is a person's decision at G5, whatever the plan said."""

from valxel_sdlc.graph.phase import agent_instructions, risk_tier


class Lane:
    data_class, high_risk_paths = "general", ("src/auth/",)


def test_the_files_agents_read_as_instructions():
    hits = ["CLAUDE.md", "services/api/CLAUDE.md", "CLAUDE.local.md", "AGENTS.md", ".claude/settings.json",
            ".claude/skills/deploy/SKILL.md", ".mcp.json", ".cursorrules", ".cursor/rules/x.mdc",
            ".github/copilot-instructions.md", ".github/instructions/py.instructions.md", ".codex/config.toml"]
    misses = ["docs/claude.md", "src/agents.py", "README.md", ".github/workflows/ci.yml", "src/CLAUDE.mdx"]
    assert agent_instructions(hits + misses) == sorted(hits)


def test_a_change_to_one_makes_the_change_set_high_risk():
    plain = [{"repo": "acme/web", "files": ["src/app.ts"]}]
    assert risk_tier({}, Lane(), plain) == "low"
    assert risk_tier({}, Lane(), [{"repo": "acme/web", "files": ["CLAUDE.md"]}]) == "high"
    # Not in the plan, but in what the pull request changed.
    assert risk_tier({}, Lane(), plain, changed=["src/app.ts", ".claude/skills/release/SKILL.md"]) == "high"


def test_a_coder_that_edits_claude_md_sends_the_feature_to_g5(engine, monkeypatch):
    real = engine.services.github.pr_files

    def with_instructions(*, repo, number):
        return (real(repo=repo, number=number) or []) + [{"path": "CLAUDE.md", "status": "modified", "additions": 3}]

    monkeypatch.setattr(engine.services.github, "pr_files", with_instructions)
    fid = engine.start(product_id="FARM", title="Yield chart")["feature_id"]
    v = engine.auto_drive(fid)
    assert v["risk_tier"] == "high" and "g5" in [g["gate"] for g in v["gate_decisions"]]
    ev = [e for e in v["events"] if e.get("kind") == "agent_instructions_changed"]
    assert ev and "CLAUDE.md" in ev[0]["detail"]

"""An agent's tools: approved at G4, held to on the branch and set on every deploy (agent_tools.py, T-19)."""

import json

import pytest

from valxel_sdlc import agent_tools
from valxel_sdlc.graph.infra import _apply_capacity

REPO, MCP = "acme-emr/emr-triage", "acme-emr/emr-care-mcp"
REPOS = [{"name": REPO, "role": "agent"}, {"name": MCP, "role": "mcp-server"}]
HLD = f"## Agent tools\n- {REPO}: read care__find_patient, care__list_open_tasks; propose care__create_task\n"
YAML = """name: emr-triage
tools:
  read_only: [care__find_patient, care__list_open_tasks]
  approval: [care__create_task]
"""


def test_an_approved_line_passes_and_tells_the_coder():
    assert agent_tools.design_findings(HLD, REPOS) == []
    [line] = agent_tools.parse_agent_tools(HLD)
    assert line["read"] == ["care__find_patient", "care__list_open_tasks"] and line["propose"] == ["care__create_task"]
    said = agent_tools.coder_instruction(HLD, REPO)
    assert "care__find_patient" in said and "never a wildcard" in said
    assert agent_tools.coder_instruction(HLD, MCP) == ""


@pytest.mark.parametrize("line,says", [
    ("read care__*", "name each tool exactly"),
    ("read Find Patient", "not a tool name"),
    ("read care__a; propose care__a", "either read or propose"),
    ("write care__a", "say read <tools> or propose"),
    ("read none; propose none", "names no tools"),
])
def test_unsafe_or_vague_lines_are_refused(line, says):
    found = agent_tools.design_findings(f"## Agent tools\n- {REPO}: {line}\n", REPOS)
    assert any(says in f for f in found), found


def test_every_agent_repo_needs_a_line():
    [f] = agent_tools.design_findings("", REPOS)
    assert "say which tools it reads with" in f
    assert "isn't an agent repo" in agent_tools.design_findings(HLD.replace(REPO, MCP), REPOS)[0]
    assert agent_tools.design_findings(f"## Agent tools\n- {REPO}: propose care__*\n", REPOS) == []


def test_the_branch_may_not_allow_more_than_the_design():
    [line] = agent_tools.parse_agent_tools(HLD)
    assert agent_tools.config_findings(YAML, line) == []
    promoted = YAML.replace("read_only: [care__find_patient, care__list_open_tasks]",
                            "read_only: [care__find_patient, care__create_task]")
    assert "lets care__create_task run without a person; the HLD has it proposed" in \
        agent_tools.config_findings(promoted, line)[0]
    more = YAML.replace("approval: [care__create_task]", "approval: [care__create_task, billing__refund]")
    assert "allows billing__refund" in agent_tools.config_findings(more, line)[0]
    assert "missing" in agent_tools.config_findings("", line)[0]
    wild = agent_tools.parse_agent_tools(f"## Agent tools\n- {REPO}: propose care__*\n")[0]
    assert agent_tools.config_findings("tools: {approval: [care__close_task]}", wild) == []


def test_build_findings_and_deploy_settings(services):
    services.github.files[(REPO, "feature/F-1", "agent.yaml")] = YAML.replace(
        "approval: [care__create_task]", "approval: [care__create_task, billing__refund]")
    from valxel_sdlc.graph.phase import agent_tools_findings, agent_tools_instruction

    results = {"T1": {"repo": REPO, "role": "agent", "branch": "feature/F-1"}}
    [f] = agent_tools_findings(services, {"hld_text": HLD}, results)
    assert f.startswith("T1: ") and "billing__refund" in f
    assert agent_tools_instruction({"hld_text": HLD}, {"role": "agent", "repo": REPO})
    assert agent_tools.binding_configs(YAML) == {
        "allowedTools": json.dumps(["care__create_task", "care__find_patient", "care__list_open_tasks"]),
        "readOnlyTools": json.dumps(["care__find_patient", "care__list_open_tasks"])}
    # A wildcard never reaches the read list, and no file means no tools.
    assert json.loads(agent_tools.binding_configs("tools: {read_only: ['care__*']}")["readOnlyTools"]) == []
    assert agent_tools.binding_configs("") == {"allowedTools": "[]", "readOnlyTools": "[]"}
    services.github.files[(REPO, "abc", "agent.yaml")] = YAML
    target = {"component": "emr-triage", "project": "emr", "namespace": None, "pipeline": ("dev", "staging", "production")}
    got = _apply_capacity(services, type("Snap", (), {"capacity": {}})(), target, "dev", repo=REPO, role="agent", ref="abc")
    assert json.loads(got["settings"]["readOnlyTools"]) == ["care__find_patient", "care__list_open_tasks"]


def test_the_agent_component_type_sets_both_variables():
    from pathlib import Path

    import yaml

    f = Path(__file__).resolve().parents[1] / "examples/governance/valxel-platform/sdlc-governance/platform/openchoreo/component-types.yaml"
    agent = next(d for d in yaml.safe_load_all(f.read_text()) if d and d["metadata"]["name"] == "agent")
    env = agent["spec"]["resources"][0]["template"]["spec"]["template"]["spec"]["containers"][0]["env"]
    assert {e["name"]: e["value"] for e in env} == {"AGENT_ALLOWED_TOOLS": "${environmentConfigs.allowedTools}",
                                                    "AGENT_READ_ONLY_TOOLS": "${environmentConfigs.readOnlyTools}"}

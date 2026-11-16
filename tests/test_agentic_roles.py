"""Repos for agentic features: the agent and mcp-server roles, and the evals suite."""

from pathlib import Path

from valxel_sdlc.agents.parse import parse_affected_repos, parse_new_repos
from valxel_sdlc.agents.templates import _flowchart
from valxel_sdlc.provisioning import check_proposal, effective_standards, load_standards, plan
from valxel_sdlc.quality import SuiteResult, effective_policy, evaluate, load_quality
from valxel_sdlc.repo_roles import REPO_ORDER, ROLES

ROOT = Path(__file__).resolve().parents[1]
STD = load_standards(ROOT / "config" / "repos.yaml")


class Product:
    product_id = "CLN"
    org = "acme-clinic"
    data_class = "phi"
    approvers = {"g5": ["acme-clinic/tech-leads"], "security": ["acme-clinic/security"]}
    high_risk_paths = ["src/auth"]
    repos = [type("R", (), {"name": "acme-clinic/cln-intake-api", "role": "provider"})()]


def test_every_role_has_a_template_and_checks():
    assert set(STD.roles) == set(ROLES)
    for role, spec in STD.roles.items():
        assert spec["template"].startswith("valxel-platform/template-"), role
        assert "unit" in spec["required_checks"] and "CodeQL" in spec["required_checks"]


def test_dependencies_merge_first():
    assert REPO_ORDER["library"] < REPO_ORDER["provider"] < REPO_ORDER["mcp-server"] < REPO_ORDER["agent"]
    assert REPO_ORDER["agent"] < REPO_ORDER["consumer"]


def test_new_agent_and_mcp_server_repos_are_planned_with_their_checks():
    hld = """## ADRs
- ADR-CLN-FEAT-9-1: Create acme-clinic/cln-care-mcp-server: agents need care data as tools
- ADR-CLN-FEAT-9-2: Create acme-clinic/cln-intake-agent: referrals are triaged by an agent
## New repos
- acme-clinic/cln-care-mcp-server (mcp-server): Care data as MCP tools [ADR-CLN-FEAT-9-1]
- acme-clinic/cln-intake-agent (agent): Turns referrals into care tasks [ADR-CLN-FEAT-9-2]
## Affected repos
- acme-clinic/cln-intake-api (provider)
- acme-clinic/cln-intake-agent (agent)
"""
    proposals = parse_new_repos(hld)
    assert [(p["role"], bool(p["adr"])) for p in proposals] == [("mcp-server", True), ("agent", True)]
    assert [r["role"] for r in parse_affected_repos(hld)] == ["provider", "agent"]
    policy = {"org": "acme-clinic", "prefix": "cln-", "pattern": STD.naming["pattern"], "max_length": 50,
              "roles": sorted(STD.roles)}
    assert check_proposal(policy, proposals, set()) == []

    standards = effective_standards(STD, "phi", None)
    agent = plan(Product, proposals[1], standards)
    assert agent["template"] == "valxel-platform/template-agent"
    assert agent["ruleset"]["required_checks"] == ["unit", "evals", "CodeQL"]
    assert agent["ruleset"]["required_approvals"] == 0  # people approve at the gates, not in GitHub
    # Its own locked-down type, and gVisor for a PHI product (platform/openchoreo/component-types.yaml).
    assert agent["openchoreo"]["component_type"] == "deployment/agent"
    from valxel_sdlc.provisioning import component_parameters

    assert component_parameters(agent["openchoreo"], "phi") == {"isolation": "gvisor"}
    assert component_parameters(agent["openchoreo"], "payments") == {"isolation": "gvisor"}
    assert component_parameters(agent["openchoreo"], "general") == {"isolation": "standard"}
    assert component_parameters(plan(Product, proposals[0], standards)["openchoreo"], "phi") == {}
    assert "agent" in agent["topics"]
    mcp = plan(Product, proposals[0], standards)
    assert mcp["template"] == "valxel-platform/template-mcp-server"
    assert mcp["ruleset"]["required_checks"] == ["unit", "CodeQL"]


def test_architecture_view_shows_agents_tools_and_gateway():
    repos = [{"name": "acme-clinic/cln-intake-api", "role": "provider"},
             {"name": "acme-clinic/cln-care-mcp-server", "role": "mcp-server"},
             {"name": "acme-clinic/cln-intake-agent", "role": "agent"}]
    chart = _flowchart({"product_id": "CLN", "product_name": "CLN"}, repos)
    assert '|"MCP tools"|' in chart and '|"models"| llm_gateway' in chart
    assert chart.count('|"versioned API"|') == 1  # the MCP server calls the provider


def test_evals_suite_reads_the_evals_check_and_fails_the_pr_when_red():
    policy = effective_policy(load_quality(ROOT / "config" / "quality.yaml"), "phi", None)
    assert policy["evals"]["stage"] == "pr" and policy["evals"]["kind"] == "test"
    ok = [SuiteResult(suite="unit", kind="test", stage="pr", state="passed", repo="r", coverage=90.0)]
    # Repos without evals (not agents) are not held up.
    assert not any("evals" in r for r in evaluate("pr", policy, ok, [])["test_failures"])
    red = [*ok, SuiteResult(suite="evals", kind="test", stage="pr", state="failed", repo="r",
                            message="2 passed, 1 failed")]
    assert any(r.startswith("evals tests failed in r") for r in evaluate("pr", policy, red, [])["test_failures"])


KNOWLEDGE_HLD = """## ADRs
- ADR-CLN-FEAT-9-1: Create acme-clinic/cln-knowledge: agents answer from clinical notes
## New repos
- acme-clinic/cln-knowledge (knowledge): Search over notes and messages [ADR-CLN-FEAT-9-1]
## Affected repos
- acme-clinic/cln-intake-api (provider)
- acme-clinic/cln-knowledge (knowledge)
## Knowledge
- acme-clinic/cln-knowledge: model text-embedding-3-small via gateway (BAA); sources: notes (events emr.notes.v1), forms (files)
"""


def test_a_knowledge_repo_is_planned_between_the_worker_and_the_mcp_server():
    assert REPO_ORDER["event-worker"] < REPO_ORDER["knowledge"] < REPO_ORDER["mcp-server"]
    proposals = parse_new_repos(KNOWLEDGE_HLD)
    assert [p["role"] for p in proposals] == ["knowledge"]
    knowledge = plan(Product, proposals[0], effective_standards(STD, "phi", None))
    assert knowledge["template"] == "valxel-platform/template-knowledge"
    assert knowledge["ruleset"]["required_checks"] == ["unit", "retrieval", "CodeQL"]
    assert knowledge["openchoreo"]["component_type"] == "deployment/service"


def test_the_hld_says_which_model_and_sources_and_phi_embeds_under_the_baa():
    from valxel_sdlc import retrieval

    repos = parse_affected_repos(KNOWLEDGE_HLD)
    [k] = retrieval.parse_knowledge(KNOWLEDGE_HLD)
    assert k["model"] == "text-embedding-3-small" and k["via"] == "gateway (baa)"
    assert k["sources"] == [{"name": "notes", "kind": "events", "detail": "emr.notes.v1"},
                            {"name": "forms", "kind": "files", "detail": ""}]
    assert retrieval.check(KNOWLEDGE_HLD, repos, {"data_class": "phi"}) == []
    plain = KNOWLEDGE_HLD.replace("via gateway (BAA)", "via gateway")
    assert "under the BAA" in retrieval.check(plain, repos, {"data_class": "phi"})[0]
    assert retrieval.check(plain, repos, {"data_class": "general"}) == []
    missing = KNOWLEDGE_HLD.split("## Knowledge")[0]
    assert "say under '## Knowledge'" in retrieval.check(missing, repos, {})[0]
    odd = KNOWLEDGE_HLD.replace("via gateway (BAA)", "via openai").replace("notes (events emr.notes.v1), forms (files)", "?")
    problems = retrieval.check(odd, repos, {})
    assert any("not 'openai'" in p for p in problems) and any("at least one source" in p for p in problems)
    assert retrieval.check(KNOWLEDGE_HLD, [r for r in repos if r["role"] != "knowledge"], {}) == []


def test_coder_agents_write_the_config_and_golden_set_from_the_decision():
    from valxel_sdlc import retrieval

    ctx = {"parent_texts": {"HLD": KNOWLEDGE_HLD}}
    said = retrieval.instruction(ctx, {"role": "knowledge", "repo": "acme-clinic/cln-knowledge"})
    assert "knowledge.yaml" in said and "evals/questions.yaml" in said and "approve both at G5" in said
    assert "text-embedding-3-small via gateway (baa)" in said and "notes (events), forms (files)" in said
    assert retrieval.instruction(ctx, {"role": "agent", "repo": "acme-clinic/cln-intake-agent"}) == ""
    bare = retrieval.instruction({"parent_texts": {}}, {"role": "knowledge", "repo": "acme-clinic/x"})
    assert "decided at G4" not in bare and "knowledge.yaml" in bare


def test_the_architecture_view_shows_search_and_embeddings():
    repos = [{"name": "acme-clinic/cln-intake-api", "role": "provider"},
             {"name": "acme-clinic/cln-knowledge", "role": "knowledge"},
             {"name": "acme-clinic/cln-intake-agent", "role": "agent"}]
    chart = _flowchart({"product_id": "CLN", "product_name": "CLN"}, repos)
    assert '|"search tools"|' in chart and '|"embeddings"| llm_gateway' in chart
    assert '-->|"events (Kafka)"|' in chart


def test_g6_shows_a_knowledge_repos_golden_set_beside_the_last_release():
    from valxel_sdlc.graph.pipeline import agent_evals

    def suite(name, stage, passed, failed):
        return {"suite": name, "repo": "acme-clinic/cln-knowledge", "passed": passed, "failed": failed, "state": "success"}

    state = {"feature_id": "CLN-FEAT-9", "product_id": "CLN",
             "tasks": [{"repo": "acme-clinic/cln-knowledge", "role": "knowledge"}],
             "quality": {"pr": {"suites": [suite("retrieval", "pr", 18, 1)]},
                         "dev": {"suites": [suite("integration", "dev", 17, 2)]}}}
    previous = {**state, "quality": {"pr": {"suites": [suite("retrieval", "pr", 17, 2)]}}}

    class Services:
        def suspects(self, product, repo):
            return [{"feature_id": "CLN-FEAT-9"}, {"feature_id": "CLN-FEAT-4"}]

        def feature_state(self, fid):
            return previous

    [row] = agent_evals(Services(), state)
    assert row["role"] == "knowledge" and row["stages"]["pr"]["rate"] == round(18 / 19, 4)
    assert row["previous_feature"] == "CLN-FEAT-4" and row["change"]["pr"] == round(round(18 / 19, 4) - round(17 / 19, 4), 4)

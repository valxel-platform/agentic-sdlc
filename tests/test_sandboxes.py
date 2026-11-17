"""Code sandboxes: agents run their code there, nowhere else (sandboxes.py)."""

from valxel_sdlc import sandboxes
from valxel_sdlc.evaluators.checks import _endpoints

AGENT, BOX, API = "acme-emr/emr-intake-agent", "acme-emr/emr-code-sandbox", "acme-emr/emr-api"
REPOS = [{"name": AGENT, "role": "agent"}, {"name": BOX, "role": "code-sandbox"}, {"name": API, "role": "provider"}]
RUNS = "The intake agent runs generated Python over the uploaded table to total the visits.\n"


def endpoints(*lines: str) -> str:
    return "## Endpoints\n" + "".join(f"- {line}\n" for line in lines)


def test_agents_that_run_code_need_a_sandbox_they_call():
    ok = RUNS + endpoints(f"{AGENT}: project; calls emr-code-sandbox", f"{BOX}: project", f"{API}: project")
    assert sandboxes.design_findings(ok, REPOS) == []
    no_box = [r for r in REPOS if r["role"] != "code-sandbox"]
    assert "add a code sandbox" in sandboxes.design_findings(RUNS, no_box)[0]
    # The product already has one: the agent just has to call it.
    assert sandboxes.design_findings(ok, no_box, product_repos=REPOS) == []
    uncalled = RUNS + endpoints(f"{AGENT}: project", f"{BOX}: project")
    assert "no agent calls emr-code-sandbox" in sandboxes.design_findings(uncalled, REPOS)[0]
    assert sandboxes.design_findings("The agent drafts referral letters.", no_box) == []


def test_what_counts_as_running_code():
    for text in ("uses a code interpreter", "executes model-written code", "running its own python scripts",
                 "calls run_code", "Code execution happens in"):
        assert sandboxes.runs_code(text), text
    for text in ("reviews the code", "the code of conduct", "runs the job nightly"):
        assert not sandboxes.runs_code(text), text


def test_a_sandbox_is_project_visible_calls_nothing_and_only_agents_call_it():
    roles = {r["name"]: r["role"] for r in REPOS}
    hld = endpoints(f"{AGENT}: project; calls emr-code-sandbox", f"{BOX}: external; calls emr-api",
                    f"{API}: project; calls emr-code-sandbox")
    found = _endpoints(hld, REPOS, {"repos": REPOS})
    assert any("its visibility is project" in f for f in found)
    assert any("it calls nothing" in f for f in found)
    assert any(f.startswith(f"{API} calls the code sandbox") for f in found)
    assert not any(f.startswith(AGENT) for f in found)
    from valxel_sdlc.agents.parse import parse_endpoints

    good = {e["repo"]: e for e in parse_endpoints(endpoints(f"{BOX}: project"))}
    assert sandboxes.endpoint_findings(good, roles) == []


def test_the_coder_allows_exactly_the_agents_that_call_it():
    hld = endpoints(f"{AGENT}: project; calls emr-code-sandbox, emr-api", f"{BOX}: project")
    said = sandboxes.coder_instruction(hld, BOX)
    assert "SANDBOX_ALLOWED_CLIENTS" in said and ": emr-intake-agent." in said
    assert "refuse everyone" in sandboxes.coder_instruction(endpoints(f"{BOX}: project"), BOX)


def test_the_code_sandbox_role_is_registered_everywhere_roles_are_listed():
    from valxel_sdlc import acceptance, catalog_info, changeset_env, repo_roles, template_contract
    from valxel_sdlc.evaluators import checks
    from valxel_sdlc.graph.phase import AUDITED_ROLES, SECURITY, sandbox_instruction

    assert "code-sandbox" in repo_roles.ROLES and "code-sandbox" in changeset_env.RUNTIME_ROLES
    assert "code-sandbox" in acceptance.RUNTIME_ROLES and "code-sandbox" in checks.RUNTIME_ROLES
    assert catalog_info.TYPES["code-sandbox"] == "service" and "fresh sandbox" in SECURITY["code-sandbox"]
    assert "run_code" in SECURITY["agent"] and "code-sandbox" in AUDITED_ROLES
    assert "code-sandbox" in template_contract.roles_with("post-deploy")
    assert sandbox_instruction({"hld_text": ""}, {"role": "agent"}) == ""
    assert "SANDBOX_ALLOWED_CLIENTS" in sandbox_instruction({"hld_text": ""}, {"role": "code-sandbox", "repo": BOX})

"""VX-17: designs say who may reach each component; only the BFF faces the internet; coders and
reviewers are told the security model of their role; web apps are tested through the BFF."""

from valxel_sdlc.agents.parse import parse_endpoints
from valxel_sdlc.agents.templates import _endpoint_lines
from valxel_sdlc.evaluators.checks import _endpoints
from valxel_sdlc.graph.phase import security_instruction
from valxel_sdlc.repo_roles import REPO_ORDER

REPOS = [
    {"name": "acme-clinic/cln-intake-api", "role": "provider"},
    {"name": "acme-clinic/cln-web", "role": "consumer"},
    {"name": "acme-clinic/cln-bff", "role": "bff"},
]


def hld(endpoints: str, adrs: str = "- ADR-CLN-FEAT-1-1: Extend the intake API") -> str:
    return f"# HLD\n\n## ADRs\n{adrs}\n\n## Endpoints\n{endpoints}\n"


def test_the_endpoints_section_is_parsed() -> None:
    e = parse_endpoints(hld("- acme-clinic/cln-bff: external; calls cln-intake-api, cln-web\n"
                            "- acme-clinic/cln-intake-api: project, namespace\n"))
    assert e == [
        {"repo": "acme-clinic/cln-bff", "visibility": ["external"], "calls": ["cln-intake-api", "cln-web"]},
        {"repo": "acme-clinic/cln-intake-api", "visibility": ["project", "namespace"], "calls": []},
    ]


def test_only_the_bff_faces_the_internet() -> None:
    ok = hld("- acme-clinic/cln-bff: external; calls cln-intake-api, cln-web\n"
             "- acme-clinic/cln-intake-api: project\n- acme-clinic/cln-web: project\n")
    assert _endpoints(ok, REPOS, {"repos": REPOS}) == []
    web = ok.replace("cln-web: project", "cln-web: external")
    assert any("reaches the internet through the product's BFF" in f for f in _endpoints(web, REPOS, {}))
    api = ok.replace("cln-intake-api: project", "cln-intake-api: external")
    assert any("add an ADR approving the exposure" in f for f in _endpoints(api, REPOS, {}))
    approved = hld(api.split("## Endpoints\n")[1], adrs="- ADR-CLN-FEAT-1-2: Expose the intake API external to partners")
    assert not any("ADR" in f for f in _endpoints(approved, REPOS, {}))


def test_every_component_is_declared_with_known_calls() -> None:
    assert "no Endpoints section" in _endpoints("# HLD\n", REPOS, {})[0]
    partial = hld("- acme-clinic/cln-bff: external; calls cln-billing\n- acme-clinic/cln-intake-api: public\n")
    findings = _endpoints(partial, REPOS, {"repos": REPOS})
    assert "acme-clinic/cln-web has no line in Endpoints (VX-17)" in findings
    assert any("calls cln-billing" in f for f in findings)
    assert any("unknown visibility public" in f for f in findings)
    assert _endpoints("# HLD\n", [{"name": "o/lib", "role": "library"}], {}) == []


def test_the_stub_architect_writes_endpoints_that_pass() -> None:
    repos = REPOS + [{"name": "acme-clinic/cln-agent", "role": "agent"}, {"name": "acme-clinic/cln-care-mcp", "role": "mcp-server"},
                     {"name": "acme-clinic/cln-worker", "role": "event-worker"}, {"name": "acme-clinic/cln-lib", "role": "library"}]
    lines = _endpoint_lines(repos)
    assert "- acme-clinic/cln-bff: external; calls cln-intake-api, cln-web, cln-agent" in lines
    assert "- acme-clinic/cln-agent: project; calls cln-care-mcp" in lines
    assert "- acme-clinic/cln-worker: none" in lines
    assert not any("cln-lib" in line for line in lines)
    assert _endpoints(hld("\n".join(lines)), repos, {"repos": repos}) == []


def test_each_role_is_told_its_security_model() -> None:
    assert "@PreAuthorize" in security_instruction({}, {"role": "provider"})
    assert "never a fixed token" in security_instruction({}, {"role": "agent"})
    assert "never holds a token" in security_instruction({}, {"role": "consumer"})
    assert "TokenRelay" in security_instruction({}, {"role": "bff"})
    assert security_instruction({}, {"role": "library"}) == ""
    assert REPO_ORDER["consumer"] < REPO_ORDER["bff"] < REPO_ORDER["acceptance"]


def test_web_apps_are_tested_through_the_bff() -> None:
    from valxel_sdlc.waits import WaitChecker

    sent = []

    class GH:
        def dispatch(self, *, repo, event_type, payload):
            sent.append((repo, payload["base_url"]))

    class S:
        github = GH()

    w = WaitChecker(S())
    packet = {"wait": "staging", "feature_id": "CLN-FEAT-1", "since": "t", "quality": {"targets": [
        {"repo": "o/cln-web", "sha": "1", "role": "consumer"},
        {"repo": "o/cln-bff", "sha": "2", "role": "bff"},
        {"repo": "o/cln-intake-api", "sha": "3", "role": "provider"}]}}
    w._start_post_deploy(packet, [{"repo": "o/cln-web", "url": "http://cln-web.internal"},
                                  {"repo": "o/cln-bff", "url": "https://cln.example.com"},
                                  {"repo": "o/cln-intake-api", "url": "http://api.internal"}])
    assert dict(sent) == {"o/cln-web": "https://cln.example.com", "o/cln-bff": "https://cln.example.com",
                          "o/cln-intake-api": "http://api.internal"}

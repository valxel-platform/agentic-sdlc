"""VX-17: designs say who may reach each component; only the BFF faces the internet; coders and
reviewers are told the security model of their role; web apps are tested through the BFF."""

from vrixel_sdlc.agents.parse import parse_endpoints
from vrixel_sdlc.agents.templates import _endpoint_lines
from vrixel_sdlc.evaluators.checks import _endpoints
from vrixel_sdlc.graph.phase import security_instruction
from vrixel_sdlc.repo_roles import REPO_ORDER

REPOS = [
    {"name": "vrixel-emr/emr-intake-api", "role": "provider"},
    {"name": "vrixel-emr/emr-web", "role": "consumer"},
    {"name": "vrixel-emr/emr-bff", "role": "bff"},
]


def hld(endpoints: str, adrs: str = "- ADR-EMR-FEAT-1-1: Extend the intake API") -> str:
    return f"# HLD\n\n## ADRs\n{adrs}\n\n## Endpoints\n{endpoints}\n"


def test_the_endpoints_section_is_parsed() -> None:
    e = parse_endpoints(hld("- vrixel-emr/emr-bff: external; calls emr-intake-api, emr-web\n"
                            "- vrixel-emr/emr-intake-api: project, namespace\n"))
    assert e == [
        {"repo": "vrixel-emr/emr-bff", "visibility": ["external"], "calls": ["emr-intake-api", "emr-web"]},
        {"repo": "vrixel-emr/emr-intake-api", "visibility": ["project", "namespace"], "calls": []},
    ]


def test_only_the_bff_faces_the_internet() -> None:
    ok = hld("- vrixel-emr/emr-bff: external; calls emr-intake-api, emr-web\n"
             "- vrixel-emr/emr-intake-api: project\n- vrixel-emr/emr-web: project\n")
    assert _endpoints(ok, REPOS, {"repos": REPOS}) == []
    web = ok.replace("emr-web: project", "emr-web: external")
    assert any("reaches the internet through the product's BFF" in f for f in _endpoints(web, REPOS, {}))
    api = ok.replace("emr-intake-api: project", "emr-intake-api: external")
    assert any("add an ADR approving the exposure" in f for f in _endpoints(api, REPOS, {}))
    approved = hld(api.split("## Endpoints\n")[1], adrs="- ADR-EMR-FEAT-1-2: Expose the intake API external to partners")
    assert not any("ADR" in f for f in _endpoints(approved, REPOS, {}))


def test_every_component_is_declared_with_known_calls() -> None:
    assert "no Endpoints section" in _endpoints("# HLD\n", REPOS, {})[0]
    partial = hld("- vrixel-emr/emr-bff: external; calls emr-billing\n- vrixel-emr/emr-intake-api: public\n")
    findings = _endpoints(partial, REPOS, {"repos": REPOS})
    assert "vrixel-emr/emr-web has no line in Endpoints (VX-17)" in findings
    assert any("calls emr-billing" in f for f in findings)
    assert any("unknown visibility public" in f for f in findings)
    assert _endpoints("# HLD\n", [{"name": "o/lib", "role": "library"}], {}) == []


def test_the_stub_architect_writes_endpoints_that_pass() -> None:
    repos = REPOS + [{"name": "vrixel-emr/emr-agent", "role": "agent"}, {"name": "vrixel-emr/emr-care-mcp", "role": "mcp-server"},
                     {"name": "vrixel-emr/emr-worker", "role": "event-worker"}, {"name": "vrixel-emr/emr-lib", "role": "library"}]
    lines = _endpoint_lines(repos)
    assert "- vrixel-emr/emr-bff: external; calls emr-intake-api, emr-web, emr-agent" in lines
    assert "- vrixel-emr/emr-agent: project; calls emr-care-mcp" in lines
    assert "- vrixel-emr/emr-worker: none" in lines
    assert not any("emr-lib" in line for line in lines)
    assert _endpoints(hld("\n".join(lines)), repos, {"repos": repos}) == []


def test_each_role_is_told_its_security_model() -> None:
    assert "@PreAuthorize" in security_instruction({}, {"role": "provider"})
    assert "never a fixed token" in security_instruction({}, {"role": "agent"})
    assert "never holds a token" in security_instruction({}, {"role": "consumer"})
    assert "TokenRelay" in security_instruction({}, {"role": "bff"})
    assert security_instruction({}, {"role": "library"}) == ""
    assert REPO_ORDER["consumer"] < REPO_ORDER["bff"] < REPO_ORDER["acceptance"]


def test_web_apps_are_tested_through_the_bff() -> None:
    from vrixel_sdlc.waits import WaitChecker

    sent = []

    class GH:
        def dispatch(self, *, repo, event_type, payload):
            sent.append((repo, payload["base_url"]))

    class S:
        github = GH()

    w = WaitChecker(S())
    packet = {"wait": "staging", "feature_id": "EMR-FEAT-1", "since": "t", "quality": {"targets": [
        {"repo": "o/emr-web", "sha": "1", "role": "consumer"},
        {"repo": "o/emr-bff", "sha": "2", "role": "bff"},
        {"repo": "o/emr-intake-api", "sha": "3", "role": "provider"}]}}
    w._start_post_deploy(packet, [{"repo": "o/emr-web", "url": "http://emr-web.internal"},
                                  {"repo": "o/emr-bff", "url": "https://emr.example.com"},
                                  {"repo": "o/emr-intake-api", "url": "http://api.internal"}])
    assert dict(sent) == {"o/emr-web": "https://emr.example.com", "o/emr-bff": "https://emr.example.com",
                          "o/emr-intake-api": "http://api.internal"}

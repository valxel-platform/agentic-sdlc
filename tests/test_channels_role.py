"""Conversation channels: approved at G4, held to on the branch and on deploy (channels.py)."""

import pytest

from valxel_sdlc import channels
from valxel_sdlc.graph.infra import _apply_capacity

REPO, BFF = "acme-emr/emr-chat", "acme-emr/emr-bff"
REPOS = [{"name": REPO, "role": "channel"}, {"name": BFF, "role": "bff"}]
HLD = (f"## Channels\n- {REPO}: slack full baa; sms notice; email notice\n\n"
       f"## Endpoints\n- {REPO}: external\n- {BFF}: external; calls emr-chat\n")
YAML = """name: emr-chat
data_class: phi
channels:
  - { kind: slack, content: full, baa: true }
  - { kind: sms, content: notice }
"""


def test_an_approved_line_passes_and_tells_the_coder():
    assert channels.design_findings(HLD, REPOS, "phi") == []
    [line] = channels.parse_channels(HLD, "phi")
    assert line["channels"]["slack"] == {"content": "full", "baa": True}
    said = channels.coder_instruction(HLD, REPO, "phi")
    assert "slack (content full, baa true)" in said and "sms (content notice" in said
    assert channels.coder_instruction(HLD, BFF, "phi") == ""


@pytest.mark.parametrize("line,says", [
    ("sms full", "BAA"),
    ("fax full", "one of slack"),
    ("slack full; slack notice", "each channel once"),
    ("slack loud", "say full"),
    ("", "name its channels"),
])
def test_unsafe_or_vague_lines_are_refused(line, says):
    found = channels.design_findings(f"## Channels\n- {REPO}: {line or ';'}\n", REPOS, "phi")
    assert any(says in f for f in found), found


def test_every_channel_repo_needs_a_line_and_is_external():
    assert "say which channels" in channels.design_findings("", REPOS)[0]
    hidden = HLD.replace(f"- {REPO}: external", f"- {REPO}: project")
    assert any("external in Endpoints" in f for f in channels.design_findings(hidden, REPOS, "phi"))
    from valxel_sdlc.evaluators.checks import _endpoints

    assert _endpoints(HLD, REPOS, {"repos": REPOS}) == []  # external without an ADR


def test_the_branch_may_not_say_more_than_the_design():
    [line] = channels.parse_channels(HLD, "phi")
    assert channels.config_findings(YAML, line) == []
    more = YAML + "  - { kind: teams, content: full }\n"
    assert "teams" in channels.config_findings(more, line)[0]
    louder = YAML.replace("{ kind: sms, content: notice }", "{ kind: sms, content: full, baa: true }")
    found = " | ".join(channels.config_findings(louder, line))
    assert "full text on sms" in found and "BAA on sms" in found
    assert "missing" in channels.config_findings("", line)[0]


def test_build_findings_and_deploy_settings(services):
    services.github.files[(REPO, "feature/F-1", "channel.yaml")] = YAML + "  - { kind: teams, content: full }\n"
    from valxel_sdlc.graph.phase import channels_findings

    results = {"T1": {"repo": REPO, "role": "channel", "branch": "feature/F-1"}}
    [f] = channels_findings(services, {"hld_text": HLD, "data_class": "phi"}, results)
    assert f.startswith("T1: ") and "teams" in f
    assert channels.binding_configs(YAML) == {
        "webhookPaths": ["/channels/slack", "/channels/sms"], "egressHosts": ["api.twilio.com", "slack.com"],
        "callerRoles": ["bff"], "dataClass": "phi"}
    assert channels.binding_configs("a: [") == {}
    services.github.files[(REPO, "abc", "channel.yaml")] = YAML
    target = {"component": "emr-chat", "project": "emr", "namespace": None, "pipeline": ("dev", "staging", "production")}
    got = _apply_capacity(services, type("Snap", (), {"capacity": {}})(), target, "dev", repo=REPO, role="channel",
                          ref="abc")
    assert got["settings"]["webhookPaths"] == ["/channels/slack", "/channels/sms"]


def test_the_role_is_registered_everywhere_roles_are_listed():
    from valxel_sdlc import acceptance, catalog_info, changeset_env, repo_roles, template_contract
    from valxel_sdlc.evaluators import checks
    from valxel_sdlc.graph.phase import AUDITED_ROLES, SECURITY, channels_instruction

    for where in (repo_roles.ROLES, changeset_env.RUNTIME_ROLES, acceptance.RUNTIME_ROLES, checks.RUNTIME_ROLES,
                  AUDITED_ROLES, template_contract.roles_with("post-deploy")):
        assert "channel" in where
    assert catalog_info.TYPES["channel"] == "service" and "opted out" in SECURITY["channel"]
    assert channels_instruction({"hld_text": HLD, "data_class": "phi"}, {"role": "channel", "repo": REPO})
    from valxel_sdlc.channels import HOSTS

    assert HOSTS["sms"] == ("api.twilio.com",)

"""Connectors to third parties: approved at G4, held to it on the branch and on deploy (connectors.py)."""

import pytest

from valxel_sdlc import connectors
from valxel_sdlc.graph.infra import _apply_capacity

REPO = "acme-emr/emr-labs"
REPOS = [{"name": "acme-emr/emr-api", "role": "provider"}, {"name": REPO, "role": "connector"}]
LINE = (f"- {REPO}: example-labs; data phi; baa yes; webhooks lab-results; calls api.example-labs.test, "
        "auth.example-labs.test; auth oauth2-client-credentials\n")
HLD = "## Integrations\n" + LINE
YAML = """name: emr-labs
vendor: example-labs
data_class: phi
baa: true
inbound:
  webhooks:
    - name: lab-results
      path: /webhooks/lab-results
      signature: { type: hmac-sha256, secret: lab-results-signing }
outbound:
  base_url: https://api.example-labs.test
  allowed_hosts: [api.example-labs.test, auth.example-labs.test]
  auth: { type: oauth2-client-credentials, token_url: https://auth.example-labs.test/token,
          client_id: id, client_secret: secret }
"""


def test_an_approved_line_parses_and_tells_the_coder_what_to_write():
    assert connectors.design_findings(HLD, REPOS) == []
    [line] = connectors.parse_integrations(HLD)
    assert line["hosts"] == ["api.example-labs.test", "auth.example-labs.test"] and line["baa"]
    said = connectors.coder_instruction(HLD, REPO)
    assert "lab-results at /webhooks/lab-results" in said and "allowed_hosts exactly api.example-labs.test" in said
    assert connectors.coder_instruction(HLD, "acme-emr/emr-api") == ""


@pytest.mark.parametrize("line,says", [
    ("example-labs; data phi; baa no; webhooks x; calls a.test; auth api-key", "business associate agreement"),
    ("example-labs; data phi; webhooks x; calls a.test; auth api-key", "baa yes' or 'baa no"),
    ("example-labs; data secret; baa no; webhooks x; calls none; auth none", "data <general"),
    ("example-labs; data general; baa no; webhooks none; calls https://a.test/x; auth api-key", "no scheme"),
    ("example-labs; data general; baa no; webhooks none; calls *.a.test; auth api-key", "wildcards"),
    ("example-labs; data general; baa no; webhooks none; calls 10.0.0.4; auth api-key", "not an IP"),
    ("example-labs; data general; baa no; webhooks none; calls emr-api.svc; auth api-key", "inside the platform"),
    ("example-labs; data payments; baa no; webhooks x; calls a.test; auth none", "auth none can't carry payments"),
    ("example-labs; data general; baa no; webhooks x; calls none; auth api-key", "name the hosts it calls"),
    ("example-labs; data general; baa no; webhooks none; calls none; auth none", "nothing crosses"),
    ("Example Labs; data general; baa no; webhooks x; calls none; auth none", "name the vendor"),
    ("example-labs; data general; baa no; webhooks Bad_Name; calls none; auth none", "isn't a name"),
    ("example-labs; data general; baa no; webhooks x; calls none; auth basic", "auth <"),
])
def test_unsafe_or_vague_lines_are_refused_at_g4(line, says):
    findings = connectors.design_findings(f"## Integrations\n- {REPO}: {line}\n", REPOS)
    assert any(says in f for f in findings), findings


def test_every_connector_needs_a_line_and_only_connectors_get_one():
    assert "is a connector: say what crosses" in connectors.design_findings("", REPOS)[0]
    other = connectors.design_findings(HLD + "- acme-emr/emr-api: v; data general; baa no; webhooks none; "
                                       "calls a.test; auth api-key\n", REPOS)
    assert any("only connectors talk to third parties" in f for f in other)


def test_external_exactly_when_it_receives_webhooks():
    ext, none = {"visibility": ["external"], "calls": []}, {"visibility": ["none"], "calls": []}
    assert connectors.endpoint_findings(HLD, {REPO: ext}) == []
    assert "make it external" in connectors.endpoint_findings(HLD, {REPO: none})[0]
    outbound_only = HLD.replace("webhooks lab-results", "webhooks none")
    assert "receives no webhooks" in connectors.endpoint_findings(outbound_only, {REPO: ext})[0]


def test_g4_runs_the_connector_checks_and_lets_a_webhook_connector_be_external():
    from valxel_sdlc.evaluators.checks import _endpoints

    hld = HLD + f"\n## Endpoints\n- acme-emr/emr-api: project\n- {REPO}: external\n"
    assert _endpoints(hld, REPOS, {"repos": REPOS}) == []
    hld = HLD + f"\n## Endpoints\n- acme-emr/emr-api: project\n- {REPO}: project\n"
    assert any("make it external" in f for f in _endpoints(hld, REPOS, {"repos": REPOS}))


def test_the_branch_may_not_allow_more_than_the_design():
    [line] = connectors.parse_integrations(HLD)
    assert connectors.config_findings(YAML, line) == []
    wider = YAML.replace("auth.example-labs.test]", "auth.example-labs.test, exfil.test]")
    assert "exfil.test" in connectors.config_findings(wider, line)[0]
    more = YAML.replace("      signature: { type: hmac-sha256, secret: lab-results-signing }\n",
                        "      signature: { type: hmac-sha256, secret: s }\n    - name: admin\n      path: /webhooks/admin\n")
    assert any("admin" in f for f in connectors.config_findings(more, line))
    lower = YAML.replace("data_class: phi\nbaa: true", "data_class: general\nbaa: false")
    found = connectors.config_findings(lower, line)
    assert any("data_class 'general'" in f for f in found) and any("baa: true" in f for f in found)
    assert any("api-key" in f for f in connectors.config_findings(YAML.replace("oauth2-client-credentials", "api-key"), line))
    assert any("vendor" in f for f in connectors.config_findings(YAML.replace("vendor: example-labs", "vendor: x"), line))
    assert "missing" in connectors.config_findings("", line)[0]


def test_build_findings_read_integration_yaml_on_the_branch(services):
    services.github.files[(REPO, "feature/F-1", "integration.yaml")] = YAML.replace("auth.example-labs.test]",
                                                                                     "auth.example-labs.test, x.test]")
    results = {"T2": {"repo": REPO, "role": "connector", "branch": "feature/F-1"},
               "T1": {"repo": "acme-emr/emr-api", "role": "provider", "branch": "feature/F-1"}}
    [finding] = connectors.build_findings(services.github, HLD, results)
    assert finding.startswith("T2: ") and "x.test" in finding
    from valxel_sdlc.graph.phase import integration_findings

    assert integration_findings(services, {"hld_text": HLD}, results) == [finding]
    assert integration_findings(services, {"hld_text": HLD}, {"T1": results["T1"]}) == []


def test_a_deploy_exposes_only_the_webhooks_and_allows_only_the_vendor_hosts(services):
    assert connectors.binding_configs(YAML) == {
        "webhookPaths": ["/webhooks/lab-results"], "egressHosts": ["api.example-labs.test", "auth.example-labs.test"],
        "dataClass": "phi"}
    odd = YAML.replace("path: /webhooks/lab-results", "path: /admin").replace("auth.example-labs.test]", "10.1.1.1]")
    assert connectors.binding_configs(odd) == {"webhookPaths": [], "egressHosts": ["api.example-labs.test"],
                                               "dataClass": "phi"}
    assert connectors.binding_configs("a: [") == {}
    services.github.files[(REPO, "abc123", "integration.yaml")] = YAML
    target = {"component": "emr-labs", "project": "emr", "namespace": None, "pipeline": ("dev", "staging", "production")}
    got = _apply_capacity(services, type("Snap", (), {"capacity": {}})(), target, "staging", repo=REPO,
                          role="connector", ref="abc123")
    assert got["settings"]["webhookPaths"] == ["/webhooks/lab-results"]
    applied = services.openchoreo.binding_configs[("emr-labs", "staging")]["configs"]
    assert applied["egressHosts"] == ["api.example-labs.test", "auth.example-labs.test"]


def test_the_connector_role_is_registered_everywhere_roles_are_listed():
    from valxel_sdlc import acceptance, catalog_info, changeset_env, repo_roles, template_contract
    from valxel_sdlc.evaluators import checks
    from valxel_sdlc.graph.phase import AUDITED_ROLES, FLAGGED_ROLES, SECURITY

    assert "connector" in repo_roles.ROLES and "connector" in changeset_env.RUNTIME_ROLES
    assert "connector" in acceptance.RUNTIME_ROLES and "connector" in checks.RUNTIME_ROLES
    assert catalog_info.TYPES["connector"] == "service" and "/webhooks/<name>" in SECURITY["connector"]
    assert "connector" in AUDITED_ROLES and "connector" in FLAGGED_ROLES
    assert "connector" in template_contract.roles_with("post-deploy")


PULLS = "; pulls test-catalog:http-cursor, protocols:object-store:example-labs-drop"
PULL_HLD = "## Integrations\n" + LINE.rstrip("\n") + PULLS + "\n"
PULL_YAML = YAML + """pull:
  - name: test-catalog
    adapter: http-cursor
    request: { path: /v1/tests }
  - name: protocols
    adapter: object-store
    store: { kind: s3, bucket: example-labs-drop }
    deletions: full-listing
"""


def test_approved_pull_sources_parse_and_the_coder_is_told_exactly_which():
    assert connectors.design_findings(PULL_HLD, REPOS) == []
    [line] = connectors.parse_integrations(PULL_HLD)
    assert line["pulls"] == [{"name": "test-catalog", "adapter": "http-cursor", "bucket": ""},
                             {"name": "protocols", "adapter": "object-store", "bucket": "example-labs-drop"}]
    said = connectors.coder_instruction(PULL_HLD, REPO)
    assert ("pull: exactly these sources: test-catalog (http-cursor); protocols (object-store, bucket "
            "example-labs-drop)") in said
    assert "@pull handler each" in said and "contracts/" in said
    assert connectors.coder_instruction(HLD, REPO).endswith("No pull: section.")
    assert connectors.pull_sources(PULL_HLD) == {REPO: ["test-catalog", "protocols"]}
    assert connectors.pull_sources(HLD) == {}


@pytest.mark.parametrize("pulls,says", [
    ("pulls notes:graph-sharepoint", "isn't one the platform team has approved"),
    ("pulls notes", "adapter '?'"),
    ("pulls notes:object-store", "name the bucket it reads"),
    ("pulls notes:object-store:bad_bucket", "name the bucket it reads"),
    ("pulls notes:http-cursor:a-bucket", "names no bucket"),
    ("pulls no_tes:http-cursor", "name each source once"),
    ("pulls a:http-cursor, a:http-cursor", "name each source once"),
])
def test_unapproved_or_vague_pull_sources_are_refused_at_g4(pulls, says):
    findings = connectors.design_findings(f"## Integrations\n- {REPO}: {LINE.split(': ', 1)[1].rstrip()}; {pulls}\n",
                                          REPOS)
    assert any(says in f for f in findings), findings


def test_an_http_pull_needs_its_hosts_and_a_pull_only_connector_is_allowed():
    no_hosts = (f"## Integrations\n- {REPO}: example-labs; data general; baa no; webhooks none; calls none; "
                "auth none; pulls a:http-cursor\n")
    assert any("name them under 'calls'" in f for f in connectors.design_findings(no_hosts, REPOS))
    files_only = no_hosts.replace("a:http-cursor", "a:object-store:drop-bucket")
    assert connectors.design_findings(files_only, REPOS) == []


def test_the_branch_may_not_pull_more_than_the_design():
    [line] = connectors.parse_integrations(PULL_HLD)
    assert connectors.config_findings(PULL_YAML, line) == []
    extra = PULL_YAML + "  - name: everything\n    adapter: http-cursor\n    request: { path: /v1/all }\n"
    assert any("pulls 'everything', a source the HLD doesn't approve" in f
               for f in connectors.config_findings(extra, line))
    swapped = PULL_YAML.replace("adapter: http-cursor", "adapter: object-store")
    assert any("reads test-catalog with 'object-store'" in f for f in connectors.config_findings(swapped, line))
    other = PULL_YAML.replace("bucket: example-labs-drop", "bucket: someone-elses")
    assert any("from bucket 'someone-elses'" in f for f in connectors.config_findings(other, line))
    assert "pull must be a list" in connectors.config_findings(YAML + "pull: nope\n", line)[-1]
    [plain] = connectors.parse_integrations(HLD)
    assert any("a source the HLD doesn't approve" in f for f in connectors.config_findings(PULL_YAML, plain))

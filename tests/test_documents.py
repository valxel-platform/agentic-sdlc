"""Document intake: what people may upload is approved at G4 and held to on the branch (documents.py)."""

import pytest

from valxel_sdlc import documents

REPO, BFF = "acme-emr/emr-intake", "acme-emr/emr-bff"
REPOS = [{"name": REPO, "role": "document-intake"}, {"name": BFF, "role": "bff"}]
LINE = (f"- {REPO}: lab-report; formats pdf, png, jpeg; max 10 MB; keep 2555 days; dataset lab-report-extracted; "
        "fields patient_ref*, collected_on*, test_code\n")
RESOURCES = """## Resources
- intake-quarantine: s3-bucket; expire_days=1
  reads, writes, deletes: emr-intake
- intake-documents: s3-bucket; versioning=true
  reads, writes, deletes: emr-intake
"""
HLD = "## Documents\n" + LINE + "\n" + RESOURCES + f"\n## Endpoints\n- {REPO}: project\n- {BFF}: external; calls emr-intake\n"
YAML = """name: emr-intake
data_class: phi
documents:
  - type: lab-report
    formats: [pdf, png]
    max_mb: 10
    retention_days: 2555
    dataset: lab-report-extracted
    fields:
      - { name: patient_ref, required: true, min_confidence: 0.9 }
      - { name: collected_on, required: true }
"""


def test_an_approved_design_passes_and_tells_the_coder_what_to_write():
    assert documents.design_findings(HLD, REPOS) == []
    [line] = documents.parse_documents(HLD)
    assert line["required"] == ["patient_ref", "collected_on"] and line["max_mb"] == 10
    said = documents.coder_instruction(HLD, REPO)
    assert "lab-report: formats pdf, png, jpeg, max_mb 10, retention_days 2555" in said
    assert documents.coder_instruction(HLD, BFF) == ""


@pytest.mark.parametrize("line,says", [
    ("lab-report; formats pdf, exe; max 10 MB; keep 30 days; dataset d; fields a", "formats <"),
    ("lab-report; formats pdf; max 500 MB; keep 30 days; dataset d; fields a", "max <1-50> MB"),
    ("lab-report; formats pdf; max 10 MB; dataset d; fields a", "keep <N> days"),
    ("lab-report; formats pdf; max 10 MB; keep 30 days; fields a", "dataset <name>"),
    ("lab-report; formats pdf; max 10 MB; keep 30 days; dataset d", "fields <name>"),
    ("lab-report; formats pdf; max 10 MB; keep 30 days; dataset d; fields Bad-Name", "isn't a name"),
    ("Lab Report; formats pdf; max 10 MB; keep 30 days; dataset d; fields a", "name the document type"),
])
def test_vague_or_unsafe_lines_are_refused(line, says):
    hld = f"## Documents\n- {REPO}: {line}\n" + RESOURCES + f"\n## Endpoints\n- {REPO}: project\n"
    found = documents.design_findings(hld, REPOS)
    assert any(says in f for f in found), found


def test_buckets_visibility_and_lines_for_the_right_repos():
    assert "say what it takes" in documents.design_findings("", REPOS)[0]
    no_buckets = "## Documents\n" + LINE
    assert any("two object-buckets" in f for f in documents.design_findings(no_buckets, REPOS))
    kept = HLD.replace("expire_days=1", "expire_days=30")
    assert any("expire_days of at most 7" in f for f in documents.design_findings(kept, REPOS))
    exposed = HLD.replace(f"- {REPO}: project", f"- {REPO}: external")
    assert any("through the product's BFF" in f for f in documents.design_findings(exposed, REPOS))
    other = HLD.replace(LINE, LINE + f"- {BFF}: x; formats pdf; max 1 MB; keep 1 days; dataset d; fields a\n")
    assert any("isn't a document intake repo" in f for f in documents.design_findings(other, REPOS))


def test_the_branch_may_not_take_more_than_the_design():
    [line] = documents.parse_documents(HLD)
    assert documents.config_findings(YAML, [line]) == []
    more = (YAML.replace("[pdf, png]", "[pdf, tiff]").replace("max_mb: 10", "max_mb: 20")
            .replace("retention_days: 2555", "retention_days: 30").replace("dataset: lab-report-extracted", "dataset: x")
            .replace("{ name: collected_on, required: true }", "{ name: ssn }"))
    found = " | ".join(documents.config_findings(more, [line]))
    for says in ("tiff", "20 MB", "30 days", "'x'", "ssn"):
        assert says in found, says
    assert "doesn't approve" in documents.config_findings(YAML.replace("type: lab-report", "type: selfie"), [line])[0]
    assert "missing" in documents.config_findings("", [line])[0]


def test_build_findings_read_intake_yaml_on_the_branch(services):
    services.github.files[(REPO, "feature/F-1", "intake.yaml")] = YAML.replace("[pdf, png]", "[pdf, tiff]")
    results = {"T1": {"repo": REPO, "role": "document-intake", "branch": "feature/F-1"}}
    from valxel_sdlc.graph.phase import documents_findings

    [f] = documents_findings(services, {"hld_text": HLD}, results)
    assert f.startswith("T1: ") and "tiff" in f
    assert documents_findings(services, {"hld_text": HLD}, {}) == []


def test_g4_runs_the_documents_checks():
    from valxel_sdlc.evaluators.checks import _architecture

    found = _architecture("## Affected repos\n- acme-emr/emr-intake (document-intake)\n", {"repos": REPOS})
    assert any("is a document intake: say what it takes" in f for f in found)


def test_the_role_is_registered_everywhere_roles_are_listed():
    from valxel_sdlc import acceptance, catalog_info, changeset_env, repo_roles, resources, template_contract
    from valxel_sdlc.evaluators import checks
    from valxel_sdlc.graph.phase import AUDITED_ROLES, SECURITY, documents_instruction

    for where in (repo_roles.ROLES, changeset_env.RUNTIME_ROLES, acceptance.RUNTIME_ROLES, checks.RUNTIME_ROLES,
                  resources.AWS_ROLES, AUDITED_ROLES, template_contract.roles_with("dataset-counts")):
        assert "document-intake" in where
    assert catalog_info.TYPES["document-intake"] == "service" and "quarantine" in SECURITY["document-intake"]
    assert documents_instruction({"hld_text": HLD}, {"role": "document-intake", "repo": REPO}).startswith(" intake.yaml")

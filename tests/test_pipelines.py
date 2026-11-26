"""Pipelines (pipelines.py): a transform's line at G4, its generated sources.yml, what the coders are
told, and the branches held to the line."""

from __future__ import annotations

import pytest
import yaml

from valxel_sdlc import pipelines

TRANSFORM, REPORTING = "valxel-emr/emr-transform", "valxel-emr/emr-reporting"
REPOS = [{"name": TRANSFORM, "role": "transform"}, {"name": REPORTING, "role": "reporting"},
         {"name": "valxel-emr/emr-notes-api", "role": "provider"}]

CONTRACT = """
apiVersion: v3.2.0
kind: DataContract
id: emr.notes
name: Clinical notes
version: 1.3.0
status: active
schema:
  - name: notes
    physicalType: topic
    physicalName: emr.notes.v1
    logicalType: object
    properties:
      - { name: note_id, logicalType: string, required: true, primaryKey: true, classification: none }
      - { name: patient_id, logicalType: string, required: true, classification: phi }
      - { name: kind, logicalType: string, required: true, classification: none }
      - { name: body, logicalType: string, classification: phi }
      - { name: signed_at, logicalType: timestamp, classification: none }
      - { name: clinic_id, logicalType: string, required: true, classification: none }
customProperties:
  - { property: owner, value: valxel-emr/emr-notes-api }
  - { property: events, value: [valxel.note.saved.v1] }
  - { property: tenant, value: clinic_id }
  - property: consumers
    value:
      valxel-emr/emr-reporting: [note_id, patient_id, kind, signed_at, clinic_id]
      emr-transform: [note_id, patient_id, signed_at, clinic_id]
"""
LINE = ("- valxel-emr/emr-transform: reads emr.notes; warehouse postgres; "
        "marts notes_signed:none, notes_by_clinic_year:none; shares notes_by_clinic_year with research-partner")


def hld(line: str = LINE, contract: str = CONTRACT, extra: str = "") -> str:
    return f"## Datasets\n```yaml\n{contract}```\n\n## Pipelines\n{line}\n{extra}"


def test_an_approved_line_passes_g4_and_parses():
    assert pipelines.design_findings(hld(), REPOS) == []
    [p] = pipelines.parse_pipelines(hld())
    assert p["reads"] == [{"dataset": "emr.notes", "table": "notes"}] and p["warehouse"] == "postgres"
    assert p["marts"] == {"notes_signed": "none", "notes_by_clinic_year": "none"}
    assert p["shares"] == [{"mart": "notes_by_clinic_year", "destination": "research-partner"}]
    as_table = pipelines.parse_pipelines(hld(LINE.replace("reads emr.notes", "reads emr.notes as notes_raw")))
    assert as_table[0]["reads"][0]["table"] == "notes_raw"


@pytest.mark.parametrize("change,says", [
    (("reads emr.notes", "reads emr.visits"), "emr.visits, which isn't a contract under '## Datasets'"),
    (("warehouse postgres", "warehouse snowflake"), "warehouse must be postgres for now"),
    (("marts notes_signed:none", "marts notes_signed:secretish"), "give the mart's highest data class"),
    (("marts notes_signed:none, notes_by_clinic_year:none", "marts "), "name its marts"),
    (("reads emr.notes", "reads emr.notes as Bad Table"), "the table is a lowercase SQL name"),
    (("notes_by_clinic_year:none;", "notes_by_clinic_year:phi;"), "only a mart of class none leaves the product"),
    (("shares notes_by_clinic_year with", "shares nothing_here with"), "'<one of its marts> with <destination>'"),
])
def test_a_wrong_line_is_refused(change, says):
    found = pipelines.design_findings(hld(LINE.replace(*change)), REPOS)
    assert any(says in f for f in found), found


def test_every_transform_needs_its_line_and_only_transforms_have_one():
    assert any("emr-transform is a transform: say what it reads" in f
               for f in pipelines.design_findings(hld(line=""), REPOS))
    other = LINE.replace("valxel-emr/emr-transform", "valxel-emr/emr-notes-api")
    assert any("emr-notes-api isn't a transform repo" in f for f in pipelines.design_findings(hld(other), REPOS))


def test_a_mart_is_built_only_from_datasets_the_transform_may_read_and_reporting_loads():
    not_consumer = CONTRACT.replace("      emr-transform: [note_id, patient_id, signed_at, clinic_id]\n", "")
    assert any("doesn't list emr-transform under consumers" in f
               for f in pipelines.design_findings(hld(contract=not_consumer), REPOS))
    loader = "      valxel-emr/emr-reporting: [note_id, patient_id, kind, signed_at, clinic_id]\n"
    unloaded = CONTRACT.replace(loader, "")
    assert any("nothing loads emr.notes into the warehouse" in f
               for f in pipelines.design_findings(hld(contract=unloaded), REPOS))
    short = CONTRACT.replace("[note_id, patient_id, kind, signed_at, clinic_id]", "[note_id, kind, clinic_id]")
    assert any("reads patient_id, signed_at of emr.notes, which no reporting repo keeps" in f
               for f in pipelines.design_findings(hld(contract=short), REPOS))


def test_engines_the_platform_doesnt_run_are_flagged_not_improvised():
    found = pipelines.design_findings(hld(extra="\n## Flows\n- a nightly Spark job joins notes and visits\n"
                                                "- Airflow schedules it\n"), REPOS)
    assert any("uses Spark, which the platform doesn't run" in f for f in found)
    assert any("uses Airflow" in f for f in found)
    ruled_out = hld(extra="\n## Flows\n- dbt on the job harness, no Spark and no Airflow\n"
                          "\n## Non-goals\n- Flink streaming joins\n")
    assert pipelines.design_findings(ruled_out, REPOS) == []
    assert pipelines.scope_findings("## Problem\nThe EMR's notes\n") == []  # EMR the record, not the service


def test_sources_yml_is_generated_from_what_the_transform_may_read_with_each_class():
    text = pipelines.sources_yml(hld(), TRANSFORM)
    assert text.startswith("# Written by the Valxel SDLC")
    [table] = yaml.safe_load(text)["sources"][0]["tables"]
    assert table["name"] == "notes"
    assert table["meta"] == {"dataset": "emr.notes", "key": "note_id", "tenant": "clinic_id"}
    assert [(c["name"], c["meta"]["data_class"]) for c in table["columns"]] == [
        ("note_id", "none"), ("patient_id", "phi"), ("signed_at", "none"), ("clinic_id", "none"),
        ("_updated_at", "none")]  # body isn't the transform's to read, so it isn't there
    assert pipelines.sources_yml(hld(), "valxel-emr/other") == ""


def test_the_coders_are_told_their_part():
    said = pipelines.coder_instruction(hld(), TRANSFORM)
    assert "Write dbt/models/sources.yml exactly as below" in said and "warehouse is postgres" in said
    assert "notes_signed (no column above none)" in said and "notes_by_clinic_year goes to research-partner" in said
    assert "valxel_control_total" in said and "transform-check must pass" in said and "```yaml\n# Written" in said
    reporting = pipelines.coder_instruction(hld(), REPORTING)
    assert "name them exactly notes (from: emr.notes)" in reporting
    assert pipelines.coder_instruction(hld(), "valxel-emr/emr-notes-api") == ""
    assert pipelines.coder_instruction(hld(line=""), REPORTING) == ""


MARTS = """
version: 2
models:
  - name: notes_signed
    data_tests:
      - valxel_control_total: { arguments: { measure: count(*), against: "ref('stg_notes')" } }
    columns:
      - { name: note_id, meta: { data_class: none } }
      - { name: patient_token, meta: { data_class: none } }
  - name: notes_by_clinic_year
    data_tests:
      - valxel_control_total: { arguments: { measure: sum(notes), against: "ref('stg_notes')" } }
    columns:
      - { name: clinic_id, meta: { data_class: none } }
"""


FIRST_TOTAL = ("    data_tests:\n"
               "      - valxel_control_total: { arguments: { measure: count(*), against: \"ref('stg_notes')\" } }\n")


class GitHub:
    def __init__(self, files: dict[tuple[str, str], str]) -> None:
        self.files = files

    def get_file(self, *, repo, path, ref):
        return self.files.get((repo, path))


def branch(**override: str | None) -> GitHub:
    files = {
        (TRANSFORM, "dbt/models/sources.yml"): pipelines.sources_yml(hld(), TRANSFORM),
        (TRANSFORM, "transform.yaml"): "name: emr-transform\nwarehouse: postgres\n",
        (TRANSFORM, "dbt/models/marts/marts.yml"): MARTS,
        (TRANSFORM, "dbt/models/marts/notes_signed.sql"): "select 1",
        (TRANSFORM, "dbt/models/marts/notes_by_clinic_year.sql"): "select 1",
        (REPORTING, "reporting.yaml"): "tables:\n  notes: { from: emr.notes, key: note_id }\n",
        (TRANSFORM, "workload.yaml"): "dependencies:\n  resources:\n" + pipelines.login_snippet(TRANSFORM) + "\n",
    }
    for path, text in override.items():
        key = next(k for k in files if k[1].endswith(path.replace("__", "/").replace("_dot_", ".")))
        if text is None:
            files.pop(key)
        else:
            files[key] = text
    return GitHub(files)


RESULTS = {"T1": {"repo": TRANSFORM, "role": "transform", "branch": "f"},
           "T2": {"repo": REPORTING, "role": "reporting", "branch": "f"}}


def test_branches_that_keep_to_the_line_pass():
    assert pipelines.build_findings(branch(), hld(), RESULTS) == []


def test_a_transform_branch_is_held_to_its_line():
    def found(**kw):
        return pipelines.build_findings(branch(**kw), hld(), RESULTS)

    assert any("sources.yml differs" in f for f in found(**{"sources_dot_yml": "version: 2\nsources: []\n"}))
    assert any("says warehouse snowflake" in f for f in found(**{"transform_dot_yaml": "warehouse: snowflake\n"}))
    extra = MARTS + "  - name: leak\n    columns:\n      - { name: patient_id, meta: { data_class: phi } }\n"
    assert any("mart leak isn't in the HLD's Pipelines line" in f for f in found(**{"marts_dot_yml": extra}))
    assert any("notes_signed is approved but not built" in f for f in found(**{"notes_signed_dot_sql": None}))
    high = MARTS.replace("{ name: patient_token, meta: { data_class: none } }",
                         "{ name: patient_token, meta: { data_class: phi } }")
    assert any("declares patient_token (phi) above its approved class none" in f
               for f in found(**{"marts_dot_yml": high}))
    no_total = MARTS.replace(FIRST_TOTAL, "", 1)
    assert any("notes_signed has no valxel_control_total" in f for f in found(**{"marts_dot_yml": no_total}))


def test_a_reporting_branch_names_the_tables_a_transform_reads():
    renamed = "tables:\n  clinical_notes: { from: emr.notes }\n"
    found = pipelines.build_findings(branch(**{"reporting_dot_yaml": renamed}), hld(), RESULTS)
    assert found == ["T2: reporting.yaml needs table notes from emr.notes: a transform reads it"]
    assert pipelines.build_findings(branch(), hld(line=""), RESULTS) == []


def test_the_default_table_is_the_name_after_the_product():
    assert pipelines.table_for("emr.notes") == "notes" and pipelines.table_for("emr.lab-results") == "lab_results"


def test_the_transform_role_is_registered_and_scheduled_like_a_job(services):
    from valxel_sdlc import acceptance, catalog_info, changeset_env, jobs, repo_roles
    from valxel_sdlc.evaluators import checks
    from valxel_sdlc.graph.infra import _apply_capacity
    from valxel_sdlc.graph.phase import SECURITY, schedule_instruction

    assert "transform" in repo_roles.ROLES and "transform" in changeset_env.RUNTIME_ROLES
    assert "transform" in acceptance.RUNTIME_ROLES and "transform" in checks.RUNTIME_ROLES
    assert catalog_info.TYPES["transform"] == "job" and "transform-check" in SECURITY["transform"]
    # deployed after the reporting repos whose tables it reads
    assert repo_roles.REPO_ORDER["reporting"] < repo_roles.REPO_ORDER["transform"]
    sched = "## Schedules\n- valxel-emr/emr-transform: 45 4 * * * America/Chicago; builds the marts after the loads\n"
    assert jobs.design_findings(sched, REPOS) == []
    assert any("emr-transform is a job" in f for f in jobs.design_findings("", REPOS))
    task = {"role": "transform", "repo": TRANSFORM}
    assert '"45 4 * * *"' in schedule_instruction({"hld_text": sched}, task)
    services.github.files[(TRANSFORM, "abc123", "job.yaml")] = 'name: emr-transform\nschedule: "45 4 * * *"\n'
    target = {"component": "emr-transform", "project": "emr", "namespace": None,
              "pipeline": ("dev", "staging", "production")}
    got = _apply_capacity(services, type("Snap", (), {"capacity": {}})(), target, "production",
                          repo=TRANSFORM, role="transform", ref="abc123")
    assert got["settings"]["schedule"] == "45 4 * * *"


def test_the_template_contract_and_repos_know_the_role():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    repos = yaml.safe_load((root / "config/repos.yaml").read_text())
    contract = yaml.safe_load((root / "config/template-contract.yaml").read_text())
    assert repos["platform_repos"]["valxel-platform/template-transform"] == {"kind": "template", "role": "transform"}
    assert repos["roles"]["transform"]["openchoreo"]["component_type"] == "cronjob/scheduled-task"
    assert "transform" in contract["groups"]["deployed"]
    assert contract["capabilities"]["data-classes"]["roles"] == ["transform"]
    assert contract["audit_event"]["repos"]["valxel-platform/template-transform"] == "src/job/audit.py"


def test_lowering_a_data_class_or_dropping_a_control_total_is_weakening():
    from valxel_sdlc.evaluators.weakening import compare

    path = "dbt/models/marts/marts.yml"
    lowered = MARTS.replace("{ name: note_id, meta: { data_class: none } }", "{ name: note_id, meta: {} }")
    assert compare(path, MARTS, MARTS) == []
    raised = MARTS.replace("{ name: patient_token, meta: { data_class: none } }",
                           "{ name: patient_token, meta: { data_class: phi } }")
    assert compare(path, MARTS, raised) == []  # more cautious is fine
    assert compare(path, raised, MARTS) == [f"{path}: lowers notes_signed.patient_token's data class from phi to none"]
    assert compare(path, MARTS, lowered) == [f"{path}: lowers notes_signed.note_id's data class from none to nothing"]
    no_total = MARTS.replace(FIRST_TOTAL, "", 1)
    assert compare(path, MARTS, no_total) == [f"{path}: removes a control total from notes_signed"]
    assert compare("models/other.yml", raised, MARTS) == []  # only a transform's dbt models
    assert compare(path, MARTS, "models: [") == []  # unreadable: transform-check and the marts check see it


# A transform's own login (postgres-login), made at deploy.
from pathlib import Path  # noqa: E402

from valxel_sdlc import resources  # noqa: E402

CATALOG_TEXT = (Path(__file__).resolve().parents[1]
                / "examples/governance/valxel-platform/sdlc-governance/platform/catalog.yaml").read_text()
DB = "## Resources\n- emr-db: postgres; database=emr_reports\n  writes: emr-reporting\n"


def catalog(cloud: str = "gcp") -> dict:
    return resources.load_catalog(CATALOG_TEXT, cloud)


def test_a_postgres_transform_gets_a_login_with_only_its_columns():
    [login] = pipelines.logins(hld(extra=DB), catalog())
    assert login["component"] == "emr-transform" and login["name"] == "emr-transform-login"
    assert login["database_resource"] == "emr-db"
    assert login["parameters"] == {
        "database": "emr_reports", "login": "emr_transform", "createSchemas": True, "breakGlass": True,
        "reads": [{"schema": "public", "table": "notes",
                   "columns": ["note_id", "patient_id", "signed_at", "clinic_id", "_updated_at"]}]}
    assert pipelines.login_findings(hld(extra=DB), catalog()) == []


def test_the_login_is_planned_and_bound_with_the_variables_the_template_reads():
    planned = resources.plan(hld(extra=DB), catalog(), lambda c: ("emr", "dp-emr"), product_id="emr",
                             feature_id="emr-feat-1", data_class="phi")
    [login] = [r for r in planned if r["type"] == "postgres-login"]
    assert login["name"] == "emr-transform-login" and login["owner"] == "emr-transform"
    assert login["parameters"]["database"] == "emr_reports" and "tags" in login["parameters"]
    assert set(login["outputs"]) == set(pipelines.LOGIN_ENV)
    told = pipelines.coder_instruction(hld(extra=DB), TRANSFORM)
    assert "- ref: emr-transform-login" in told and "password: TRANSFORM_PG_PASSWORD" in told


def test_a_transform_without_one_database_or_without_postgres_login_fails_g4():
    assert any("found: none" in f for f in pipelines.login_findings(hld(), catalog()))
    two = DB + "- emr-db2: postgres; database=other\n  writes: emr-reporting\n"
    assert any("found: emr-db, emr-db2" in f for f in pipelines.login_findings(hld(extra=two), catalog()))
    [aws] = pipelines.login_findings(hld(extra=DB), catalog("aws"))
    assert "doesn't offer on AWS yet" in aws
    assert pipelines.logins(hld(extra=DB), catalog("aws")) == []


@pytest.mark.parametrize("workload,says", [
    ("dependencies:\n  resources:\n    - ref: emr-transform-login\n      envBindings:\n"
     + "".join(f"        {o}: {e}\n" for o, e in pipelines.LOGIN_ENV.items()), None),
    ("dependencies:\n  resources: []\n", "must bind emr-transform-login"),
    ("dependencies:\n  resources:\n    - ref: emr-transform-login\n      envBindings:\n        host: DB_HOST\n",
     "envBindings must be exactly"),
    ("dependencies:\n  resources:\n    - ref: emr-transform-login\n      envBindings:\n"
     + "".join(f"        {o}: {e}\n" for o, e in pipelines.LOGIN_ENV.items())
     + "    - ref: emr-db\n      envBindings:\n        password: DB_PASSWORD\n", "binds only its own login"),
])
def test_the_transforms_workload_binds_only_its_own_login(workload, says):
    found = pipelines.workload_findings(workload, TRANSFORM)
    assert (found == []) if says is None else any(says in f for f in found), found

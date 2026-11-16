"""Data contracts (ODCS v3 with Valxel's extensions): the profile, compatibility between versions,
the JSON Schema components validate against, the G4 check of the HLD, and the governance repo's
CI check. All data is synthetic."""

import copy
import subprocess
from pathlib import Path
from types import SimpleNamespace

import jsonschema
import pytest
import yaml

from valxel_sdlc import datasets as ds
from valxel_sdlc.evaluators.checks import _datasets
from valxel_sdlc.repo_standards import standard_file_path

ODCS = standard_file_path("odcs-json-schema-v3.2.0.json")

NOTES = """
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
      - { name: note_id, logicalType: string, required: true, primaryKey: true, classification: none,
          logicalTypeOptions: { format: uuid } }
      - { name: patient_id, logicalType: string, required: true, classification: phi }
      - { name: kind, logicalType: string, required: true, classification: none,
          enum: [{ value: progress }, { value: discharge }] }
      - { name: body, logicalType: string, classification: phi }
      - { name: signed_at, logicalType: timestamp, classification: none }
      - { name: clinic_id, logicalType: string, required: true, classification: none }
customProperties:
  - { property: owner, value: valxel-emr/emr-notes-api }
  - { property: events, value: [valxel.note.saved.v1, valxel.note.deleted.v1] }
  - { property: compacted, value: true }
  - { property: retention, value: 7y }
  - { property: tenant, value: clinic_id }
  - property: consumers
    value:
      valxel-emr/emr-notes-worker: [note_id, patient_id, kind, body, signed_at, clinic_id]
      valxel-emr/emr-knowledge: [note_id, patient_id, kind, body, clinic_id]
"""


def notes(**changes) -> dict:
    c = yaml.safe_load(NOTES)
    for path, value in changes.items():
        *keys, last = path.split(".")
        node = c
        for k in keys:
            node = node[int(k)] if k.isdigit() else node[k]
        if value is None:
            del node[last]
        else:
            node[last] = value
    return c


def prop(c: dict, name: str) -> dict:
    return next(p for p in c["schema"][0]["properties"] if p["name"] == name)


def test_a_contract_in_valxels_profile_is_also_valid_odcs():
    assert ds.problems(notes(), ODCS) == []
    assert ds.topic(notes()) == "emr.notes.v1"
    assert ds.events(notes()) == ["valxel.note.saved.v1", "valxel.note.deleted.v1"]
    assert ds.extensions(notes())["compacted"] is True


@pytest.mark.parametrize(("change", "message"), [
    ({"kind": "Contract"}, "kind must be DataContract"),
    ({"apiVersion": "v2.2.2"}, "ODCS v3"),
    ({"id": "Notes"}, "'<product>.<name>'"),
    ({"version": "1.3"}, "semantic"),
    ({"servers": [{"server": "k", "type": "kafka", "host": "b:9093"}]}, "leave out servers"),
    ({"schema.0.physicalType": "table"}, "physicalType must be topic"),
    ({"schema.0.physicalName": "notes"}, "'<prefix>.<entity>.v<n>'"),
    ({"schema.0.properties.1.classification": "restricted"}, "classification must be one of"),
    ({"schema.0.properties.1.classification": None}, "classification must be one of"),
    ({"schema.0.properties.1.logicalType": None}, "logicalType must be one of"),
    ({"schema.0.properties.1.name": "PatientId"}, "snake_case"),
    ({"schema.0.properties.0.primaryKey": False}, "record's key"),
])
def test_the_profile_says_what_is_wrong(change, message):
    found = ds.problems(notes(**change))
    assert any(message in f for f in found), found


def test_extensions_are_checked():
    c = notes()
    c["customProperties"] = [p for p in c["customProperties"] if p["property"] != "retention"]
    c["customProperties"][1]["value"] = ["note.saved"]
    c["customProperties"].append({"property": "retention", "value": "forever"})
    c["customProperties"][2]["value"] = "yes"
    c["customProperties"][3]["value"] = "clinic"
    cons = c["customProperties"][4]["value"]
    cons["emr-reporting"] = ["note_id"]
    cons["valxel-emr/emr-notes-worker"].append("ssn")
    cons["valxel-emr/emr-notes-api"] = ["note_id"]
    found = "\n".join(ds.problems(c) + ds.problems(notes(**{"customProperties.0.value": "emr-notes-api"})))
    for words in ("owner must be", "'valxel.<record>.<what happened>.v<n>'", "compacted must be true or false",
                  "retention must be", "tenant clinic isn't a property", "consumer emr-reporting must be",
                  "reads ssn, which the contract doesn't have", "owns the dataset; it isn't also a consumer"):
        assert words in found, words
    bare = notes()
    bare["customProperties"] = []
    assert {f.split(": ", 1)[1] for f in ds.problems(bare)} >= {
        "customProperties needs owner", "customProperties needs events", "customProperties needs consumers"}


def test_the_standards_own_schema_catches_what_odcs_forbids():
    c = notes()
    prop(c, "kind")["logicalTypeOptions"] = {"enum": ["progress"]}  # enum belongs on the property in ODCS
    assert any(f.startswith("emr.notes: ODCS: ") for f in ds.problems(c, ODCS))


def test_compatible_changes_need_a_higher_version_and_nothing_else():
    old, new = notes(), notes()
    new["schema"][0]["properties"].append({"name": "amended_at", "logicalType": "timestamp", "classification": "none"})
    prop(new, "kind")["enum"].append({"value": "referral"})
    prop(new, "signed_at")["classification"] = "pii"  # stricter is fine
    assert ds.breaking(old, new) == ["emr.notes: the schema changed, so raise its version above 1.3.0"]
    new["version"] = "1.4.0"
    assert ds.breaking(old, new) == []
    assert ds.breaking(old, copy.deepcopy(old)) == []


def test_breaking_changes_are_a_new_event_type_and_contract():
    old, new = notes(), notes(version="2.0.0")
    new["schema"][0]["physicalName"] = "emr.notes.v2"
    props = new["schema"][0]["properties"]
    props[:] = [p for p in props if p["name"] != "signed_at"]
    prop(new, "body")["required"] = True
    prop(new, "patient_id")["classification"] = "pii"
    prop(new, "clinic_id")["logicalType"] = "integer"
    prop(new, "kind")["enum"] = [{"value": "progress"}]
    prop(new, "note_id")["primaryKey"] = False
    props.append({"name": "author_id", "logicalType": "string", "required": True, "classification": "pii"})
    new["customProperties"][1]["value"] = ["valxel.note.saved.v1"]
    new["customProperties"][2]["value"] = False
    found = "\n".join(ds.breaking(old, new))
    for words in ("topic changed", "signed_at was removed", "body became required", "class was loosened",
                  "changed type from string to integer", "lost enum values discharge", "whether it is the key",
                  "new property author_id is required", "valxel.note.deleted.v1 were removed",
                  "no longer compacted"):
        assert words in found, words


def test_components_validate_against_the_generated_schema():
    schema = ds.json_schema(notes())
    assert schema["required"] == ["note_id", "patient_id", "kind", "clinic_id"]
    assert schema["properties"]["signed_at"] == {"type": "string", "format": "date-time", "x-classification": "none"}
    assert schema["properties"]["kind"]["enum"] == ["progress", "discharge"]
    v = jsonschema.Draft202012Validator(schema)
    good = {"note_id": "n-1", "patient_id": "p-1", "kind": "progress", "clinic_id": "c-1", "body": "synthetic"}
    assert list(v.iter_errors(good)) == []
    # A property the contract doesn't have (so has no class) is never published.
    assert [e.validator for e in v.iter_errors({**good, "ssn": "000-00-0000"})] == ["additionalProperties"]
    assert [e.validator for e in v.iter_errors({**good, "kind": "memo"})] == ["enum"]


def test_lock_and_fingerprint_follow_the_schema():
    c = notes()
    assert ds.fingerprint(c) == ds.fingerprint(copy.deepcopy(c))
    changed = notes(version="1.4.0")
    assert ds.fingerprint(changed) != ds.fingerprint(c)  # the version is in the schema's $id
    assert yaml.safe_load(ds.lock([c])) == {"emr.notes": {"version": "1.3.0", "sha": ds.fingerprint(c), "topic": "emr.notes.v1"}}
    other = notes(id="emr.forms")
    other["customProperties"][0]["value"] = "valxel-emr/emr-forms-api"
    other["customProperties"][5]["value"] = {}
    assert [x["id"] for x in ds.for_repo([c, other], "valxel-emr/emr-knowledge")] == ["emr.notes"]
    assert [x["id"] for x in ds.for_repo([c, other], "valxel-emr/emr-forms-api")] == ["emr.forms"]


def test_the_hld_carries_one_contract_per_yaml_block():
    hld = f"# HLD\n\n## Datasets\n```yaml\n{NOTES}```\n\n```yaml\n: [\n```\n\n```yaml\n- a list\n```\n## Coverage\n- AC-1: x\n"
    contracts, errors = ds.parse_hld(hld)
    assert [c["id"] for c in contracts] == ["emr.notes"]
    assert errors[0].startswith("Datasets block 2 isn't valid YAML") and errors[1] == "Datasets block 3 isn't a data contract"
    assert ds.parse_hld("# HLD\n## Coverage\n") == ([], [])


# --- G4 ------------------------------------------------------------------------------------------

REPOS = [{"name": "valxel-emr/emr-notes-api", "role": "provider"},
         {"name": "valxel-emr/emr-notes-worker", "role": "event-worker"},
         {"name": "valxel-emr/emr-knowledge", "role": "knowledge"}]


def hld(contract: str = NOTES, events: str = "- valxel.note.saved.v1: published by emr-notes-api, applied by emr-notes-worker",
        knowledge: str = "- valxel-emr/emr-knowledge: model m via gateway (BAA); sources: notes (events emr.notes.v1, compacted)") -> str:
    return (f"# HLD\n\n## Knowledge\n{knowledge}\n\n## Events\n{events}\n\n"
            + (f"## Datasets\n```yaml\n{contract}```\n\n" if contract else "") + "## Coverage\n- AC-1: x\n")


def test_a_design_whose_events_match_their_contracts_passes():
    assert _datasets(hld(), REPOS, {"repos": REPOS}) == []
    assert _datasets("# HLD\n## Coverage\n", REPOS, {}) == []


def test_every_event_and_knowledge_source_needs_a_contract():
    found = _datasets(hld(contract=""), REPOS, {"repos": REPOS})
    assert any("valxel.note.saved.v1 has no data contract" in f for f in found)
    assert any("reads emr.notes.v1, which no data contract describes" in f for f in found)
    # One already released in the governance repo counts.
    assert _datasets(hld(contract=""), REPOS, {"repos": REPOS, "datasets": [notes()]}) == []


def test_publishers_appliers_and_indexers_must_match_the_contract():
    found = "\n".join(_datasets(hld(
        events="- valxel.note.saved.v1: published by emr-intake-api, applied by emr-notes-worker, emr-audit-worker",
        knowledge="- valxel-emr/emr-knowledge: model m via gateway (BAA); sources: notes (events emr.notes.v1, compacted)",
    ), REPOS, {"repos": REPOS}))
    assert "published by emr-intake-api, but emr.notes's owner is valxel-emr/emr-notes-api" in found
    assert "emr-audit-worker applies valxel.note.saved.v1 but isn't listed under emr.notes's consumers" in found
    unlisted = NOTES.replace("      valxel-emr/emr-knowledge: [note_id, patient_id, kind, body, clinic_id]\n", "")
    loose = unlisted.replace("compacted, value: true", "compacted, value: false")
    found = "\n".join(_datasets(hld(contract=loose), REPOS, {"repos": REPOS}))
    assert "valxel-emr/emr-knowledge indexes emr.notes.v1 but isn't listed under emr.notes's consumers" in found
    assert "says emr.notes.v1 is compacted, but emr.notes says it isn't" in found


def test_contracts_name_known_repos_and_stay_compatible_with_the_released_version():
    stranger = NOTES.replace("valxel-emr/emr-notes-api", "valxel-emr/emr-ghost-api")
    found = "\n".join(_datasets(hld(contract=stranger, events="- valxel.note.saved.v1: published by emr-ghost-api, "
                                   "applied by emr-notes-worker"), REPOS, {"repos": REPOS}))
    assert "owner valxel-emr/emr-ghost-api isn't an affected or registered repo" in found
    released = notes()
    prop(released, "body")["required"] = False
    tightened = NOTES.replace("{ name: body, logicalType: string, classification: phi }",
                              "{ name: body, logicalType: string, required: true, classification: phi }")
    found = _datasets(hld(contract=tightened), REPOS, {"repos": REPOS, "datasets": [released]})
    assert "emr.notes: property body became required" in found


# --- release: the governance record ----------------------------------------------------------------

class Source:
    def __init__(self, files: dict[str, str]):
        self.files = files

    def resolve(self, org, repo, ref):
        return "sha"

    def list(self, org, repo, directory, sha):
        return sorted(p for p in self.files if p.startswith(directory + "/"))

    def read(self, org, repo, path, sha, optional=False):
        return self.files.get(path)


class GitHub:
    def __init__(self):
        self.files, self.prs = {}, []

    def put_file(self, *, repo, branch, path, content, message):
        self.files[path] = content

    def open_pr(self, **pr):
        self.prs.append(pr)
        return {"url": "https://github.com/acme/emr-governance/pull/1"}


def services(released: dict[str, str]):
    gov = SimpleNamespace(org="valxel-emr", repo="emr-governance", ref="main", full_name="valxel-emr/emr-governance")
    return SimpleNamespace(knowledge=SimpleNamespace(cfg=SimpleNamespace(settings={})),
                           settings=SimpleNamespace(entry=lambda pid: SimpleNamespace(governance=gov)),
                           governance=SimpleNamespace(source=Source(released)), github=GitHub())


def test_release_records_new_and_changed_contracts_in_the_governance_repo():
    from valxel_sdlc.knowledge.writeback import propose_record_update

    state = {"feature_id": "EMR-FEAT-9", "title": "Signed notes", "product_id": "EMR"}
    svc = services({})
    pr = propose_record_update(svc, state, hld(), {"adrs": [], "contracts": []})
    assert "datasets/emr.notes.yaml" in pr["files"]
    assert ds.load(svc.github.files["datasets/emr.notes.yaml"]) == notes()
    assert "emr.notes 1.3.0" in svc.github.prs[0]["body"]
    # Released unchanged: nothing to record.
    svc = services({"datasets/emr.notes.yaml": svc.github.files["datasets/emr.notes.yaml"]})
    assert propose_record_update(svc, state, hld(), {"adrs": [], "contracts": []}) is None


# --- the governance repo's CI ----------------------------------------------------------------------

def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def test_governance_ci_checks_every_contract_and_compatibility_with_main(tmp_path, monkeypatch, capsys):
    repo = tmp_path / "gov"
    (repo / "datasets").mkdir(parents=True)
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "t@example.com")
    git(repo, "config", "user.name", "t")
    (repo / "datasets" / "emr.notes.yaml").write_text(NOTES)
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "notes")
    monkeypatch.chdir(repo)
    assert ds.main(["check", "datasets", "--base", "HEAD"]) == 0
    removed = NOTES.replace("      - { name: body, logicalType: string, classification: phi }\n", "")
    (repo / "datasets" / "emr.notes.yaml").write_text(removed.replace("version: 1.3.0", "version: 1.4.0"))
    (repo / "datasets" / "emr.copy.yaml").write_text(NOTES)
    (repo / "datasets" / "emr.bad.yaml").write_text(": [")
    assert ds.main(["check", "datasets", "--base", "HEAD"]) == 1
    out = capsys.readouterr().out
    for words in ("emr.notes.yaml: emr.notes: property body was removed", "emr.copy.yaml: the file name must be",
                  "emr.notes.yaml: id emr.notes is also in emr.copy.yaml", "topic emr.notes.v1 is also in",
                  "emr.bad.yaml: not valid YAML"):
        assert words in out, words
    assert ds.main(["check", "missing-dir"]) == 0
    assert ds.main([]) == 2


# --- build: each repo's copy ------------------------------------------------------------------------

def test_the_tool_writes_each_repo_its_schemas_and_lock():
    files = ds.repo_files([notes()], "valxel-emr/emr-notes-worker")
    assert sorted(files) == ["contracts/emr.notes.json", "contracts/lock.yaml"]
    assert jsonschema.Draft202012Validator(__import__("json").loads(files["contracts/emr.notes.json"])).is_valid(
        {"note_id": "n", "patient_id": "p", "kind": "progress", "clinic_id": "c"})
    assert ds.repo_files([notes()], "valxel-emr/emr-web") == {}
    lock = files["contracts/lock.yaml"]
    assert ds.drift([notes()], "valxel-emr/emr-notes-worker", lock) == []
    assert "has no contracts/lock.yaml" in ds.drift([notes()], "valxel-emr/emr-notes-worker", None)[0]
    stale = ds.drift([notes(version="1.4.0")], "valxel-emr/emr-notes-worker", lock)
    assert stale and "contracts/emr.notes.json is missing or stale" in stale[0]
    extra = lock + "emr.forms: {sha: x, version: 1.0.0, topic: emr.forms.v1}\n"
    assert "lists emr.forms, which no approved contract gives it" in ds.drift([notes()], "valxel-emr/emr-notes-worker", extra)[0]
    assert ds.drift([notes()], "valxel-emr/emr-web", None) == []


def test_coders_are_told_to_validate_what_they_publish_and_read():
    owner = ds.instruction([notes()], "valxel-emr/emr-notes-api")
    assert "publishes emr.notes on emr.notes.v1" in owner and "dataschema" in owner and "publish nothing" in owner
    reader = ds.instruction([notes()], "valxel-emr/emr-knowledge")
    assert "may use only note_id, patient_id, kind, body, clinic_id" in reader and "never its value" in reader
    assert ds.instruction([notes()], "valxel-emr/emr-web") == ""

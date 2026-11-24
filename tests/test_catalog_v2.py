"""The neutral catalog (version 2, ADR 0010; tracker CP-301 and CP-309): one cloud's view of each
type, the version 1 AWS names as aliases, Google Cloud's services and prices at G4, and resources
made under an old name keeping it."""

from pathlib import Path

import pytest

from valxel_sdlc import resources
from valxel_sdlc.cloud.gcp.accounts import PROJECT

PLATFORM = Path(__file__).resolve().parents[1] / "examples/governance/valxel-platform/sdlc-governance/platform"
TEXT = (PLATFORM / "catalog.yaml").read_text()
AWS = resources.load_catalog(TEXT, "aws")
GCP = resources.load_catalog(TEXT, "gcp")
REPOS = [{"name": "o/farm-api", "role": "provider"}, {"name": "o/farm-worker", "role": "event-worker"}]
PROJECTS = {"region": "us-central1", "provider": "gcp",
            "accounts": {"dev": "farm-dev-1", "staging": "farm-staging-1", "production": "farm-prod-1"}}


def hld(body, adrs="- ADR-1: Firestore for field-state, Pub/Sub for readings"):
    return f"# HLD\n\n## ADRs\n{adrs}\n\n## Resources\n{body}\n"


def ctx(catalog, **over):
    return {"aws_catalog": catalog, "cloud": PROJECTS, "data_class": "general",
            "pipeline": ["dev", "staging", "production"], "repos": REPOS, **over}


def test_each_cloud_sees_its_own_block():
    assert AWS["types"]["key-value-table"]["service"] == "Amazon DynamoDB"
    assert GCP["types"]["key-value-table"]["service"].startswith("Firestore")
    assert GCP["types"]["queue"]["access"]["write"] == ["roles/pubsub.publisher"]
    assert GCP["types"]["event-stream"]["parameters"]["retention_hours"] == {
        "type": "integer", "default": 24, "min": 24, "max": 744}  # Pub/Sub's 31 days narrows the type's range
    assert AWS["types"]["event-stream"]["parameters"]["retention_hours"]["max"] == 8760
    assert GCP["types"]["postgres"]["service"].startswith("Cloud SQL")
    assert "paused" not in GCP["types"]["postgres"]["sizes"]
    assert "cache" in AWS["types"] and "cache" not in GCP["types"] and GCP["unavailable"] == ["cache"]
    assert GCP["regions"]["general"][0] == "us-central1" and GCP["cloud"] == "gcp"
    assert GCP["types"]["workload-identity"]["outputs"] == {"role": "GCP_SERVICE_ACCOUNT"}
    assert AWS["aliases"]["dynamodb-table"] == "key-value-table"


def test_the_old_names_still_read():
    body = "- field-state: dynamodb-table; partition_key=field_id\n  reads, writes: farm-api"
    assert resources.check(hld(body, "- ADR-1: DynamoDB for field-state"), REPOS, ctx(AWS, cloud=None,
                                                                                     aws={"region": "x"})) == []
    [e] = resources.resolved(hld(body), AWS)
    assert (e["type"], e["named"]) == ("key-value-table", "dynamodb-table")


def test_a_design_on_google_cloud_gets_googles_services_and_prices():
    body = ("- field-state: key-value-table; partition_key=field_id\n  reads, writes: farm-api\n"
            "- readings: event-stream; retention_hours=168\n  writes: farm-api; reads: farm-worker")
    assert resources.check(hld(body), REPOS, ctx(GCP)) == []
    g4 = resources.summary(hld(body), GCP, ["dev", "staging", "production"], PROJECTS)
    assert g4["cloud_name"] == "Google Cloud" and g4["accounts"]["production"] == "farm-prod-1"
    assert {r["service"].split(":")[0] for r in g4["resources"]} == {
        "Firestore (native mode)", "Pub/Sub"}
    assert g4["monthly_usd"]["production"] == 50 + 60  # GCP's medium prices, not AWS's


@pytest.mark.parametrize(("body", "finding"), [
    ("- sessions: cache\n  reads: farm-api", "isn't offered on Google Cloud yet"),
    ("- readings: event-stream; retention_hours=800\n  writes: farm-api", "between 24 and 744"),
    ("- jobs: queue; max_receives=3\n  writes: farm-api", "between 5 and 100"),
])
def test_google_clouds_limits(body, finding):
    assert any(finding in f for f in resources.check(hld(body, "- ADR-1: records, readings, jobs"), REPOS, ctx(GCP)))


def test_new_resources_get_the_neutral_type_and_old_ones_keep_theirs():
    body = ("- field-state: key-value-table; partition_key=id\n  writes: farm-api\n"
            "- uploads: object-bucket\n  writes: farm-api")
    owner = lambda comp: ("farm", None)  # noqa: E731
    out = resources.plan(hld(body), GCP, owner, product_id="FARM", feature_id="FARM-FEAT-7", data_class="general",
                         existing={"field-state": "dynamodb-table"})
    by = {r["name"]: r for r in out}
    assert by["field-state"]["type"] == "dynamodb-table" and by["uploads"]["type"] == "object-bucket"
    assert by["uploads"]["parameters"]["tags"] == {"valxel_product": "farm", "valxel_feature": "farm-feat-7",
                                                    "valxel_resource": "uploads", "valxel_data-class": "general"}
    assert by["uploads"]["parameters"]["consumers"][0]["actions"] == ["roles/storage.objectCreator"]
    assert by["farm-api-identity"]["type"] == "workload-identity"
    # Re-declaring an old resource under its new name isn't a type change (no replacement finding).
    assert resources.check(hld(body, "- ADR-1: field-state, uploads"), REPOS,
                           ctx(GCP, aws_existing=[{"name": "field-state", "type": "dynamodb-table"}])) == []


@pytest.mark.parametrize(("catalog", "existing", "made_as"), [
    (AWS, {}, "postgres-crossplane"),                       # OpenChoreo's ClusterResourceType on AWS
    (AWS, {"records": "postgres-crossplane"}, "postgres-crossplane"),
    (GCP, {}, "postgres"),                                  # platform-resources' Cloud SQL on Google Cloud
])
def test_postgres_is_made_as_the_clouds_resource_type(catalog, existing, made_as):
    body = "- records: postgres; database=farm\n  reads, writes: farm-api"
    out = resources.plan(hld(body), catalog, lambda comp: ("farm", None), product_id="FARM", feature_id="FARM-FEAT-7",
                         data_class="general", existing=existing)
    [r] = [r for r in out if r["name"] == "records"]
    assert r["type"] == made_as and r["outputs"]["password"] == "DB_PASSWORD"


def test_a_products_projects_are_checked():
    assert resources.check_cloud(PROJECTS, "general", ["dev", "staging", "production"], GCP) == []
    bad = {**PROJECTS, "region": "europe-west1", "accounts": {"dev": "Farm_Dev", "staging": "x1", "production": "x1"}}
    problems = resources.check_cloud(bad, "phi", ["dev", "staging", "production"], GCP)
    assert any("not allowed for phi" in p for p in problems)
    assert any("accounts.dev must be the environment's Google Cloud project ID" in p for p in problems)
    assert any("its own Google Cloud project" in p for p in problems)
    assert PROJECT.match("farm-prod-1")


def test_the_architects_guide_names_the_cloud_and_what_isnt_offered():
    guide = resources.catalog_guide(GCP, "general")
    assert guide.startswith("Google Cloud services the platform offers")
    assert "Not offered on Google Cloud yet: cache" in guide and "Cloud SQL" in guide
    assert "Firestore" in guide and "DynamoDB" not in guide


def test_a_version_1_catalog_is_aws_only():
    v1 = "types:\n  x: {service: X, use_for: y, data_classes: [general], sizes: {small: {monthly_usd: 1}}}\n"
    assert "x" in resources.load_catalog(v1, "aws")["types"]
    with pytest.raises(resources.CatalogError, match="version 2 catalog"):
        resources.load_catalog(v1, "gcp")


def test_the_catalog_is_found_under_either_name():
    files = {"platform/aws-resources.yaml": "old"}
    assert resources.read_catalog(files.get, "platform/catalog.yaml") == "old"
    files = {"platform/catalog.yaml": "new"}
    assert resources.read_catalog(files.get, "platform/aws-resources.yaml") == "new"
    assert resources.read_catalog({}.get, "catalog.yaml") == ""

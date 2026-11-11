"""catalog-info.yaml: Git is the source for the Backstage catalog."""

import yaml

from vrixel_sdlc import catalog_info

ARGS = dict(repo="acme-farm/farm-field-api", role="provider", product_id="FARM", owner="group:product-owners",
            component="farm-field-api", project="farm", capability="CAP-FARM-CROP-LOG")


def test_a_new_repo_gets_a_component_entity():
    doc = yaml.safe_load(catalog_info.render(None, **ARGS))
    assert doc["kind"] == "Component" and doc["metadata"]["name"] == "farm-field-api"
    assert doc["metadata"]["annotations"] == {
        "github.com/project-slug": "acme-farm/farm-field-api", "vrixel.io/product": "FARM",
        "openchoreo.dev/project": "farm", "openchoreo.dev/component": "farm-field-api",
        "vrixel.io/capabilities": "CAP-FARM-CROP-LOG"}
    assert doc["spec"] == {"type": "service", "lifecycle": "production", "owner": "group:product-owners", "system": "farm"}


def test_what_people_wrote_stays_and_capabilities_accumulate():
    mine = """apiVersion: backstage.io/v1alpha1
kind: Component
metadata:
  name: field-api
  description: Field records
  annotations:
    github.com/project-slug: acme-farm/farm-field-api
    vrixel.io/capabilities: CAP-FARM-OLD
spec:
  type: grpc-service
  lifecycle: experimental
  owner: group:field-team
  dependsOn: [resource:field-db]
---
apiVersion: backstage.io/v1alpha1
kind: API
metadata: {name: field-api}
spec: {type: openapi, lifecycle: production, owner: group:field-team, definition: x}
"""
    comp, api = yaml.safe_load_all(catalog_info.render(mine, **ARGS))
    assert comp["metadata"]["name"] == "field-api" and comp["metadata"]["description"] == "Field records"
    assert comp["metadata"]["annotations"]["vrixel.io/capabilities"] == "CAP-FARM-OLD,CAP-FARM-CROP-LOG"
    assert comp["spec"]["type"] == "grpc-service" and comp["spec"]["owner"] == "group:field-team"
    assert comp["spec"]["lifecycle"] == "experimental" and comp["spec"]["dependsOn"] == ["resource:field-db"]
    assert api["kind"] == "API"
    again = catalog_info.render(catalog_info.render(mine, **ARGS), **ARGS)
    assert again is None  # nothing new: no commit


def test_the_build_commits_it_to_each_feature_branch(engine, services):
    fid = engine.start(product_id="FARM", title="Offline crop log", description="x")["feature_id"]
    engine.auto_drive(fid)
    repos = {t["repo"] for t in engine._state(fid)["tasks"]}
    assert repos
    for repo in repos:
        text = services.github.get_file(repo=repo, path="catalog-info.yaml", ref=f"feature/{fid}")
        doc = yaml.safe_load(text)
        assert doc["metadata"]["annotations"]["vrixel.io/capabilities"] == "CAP-FARM-OFFLINE_CROP_LOG"
        assert doc["spec"]["owner"] == "group:product-owners" and doc["spec"]["system"] == "farm"

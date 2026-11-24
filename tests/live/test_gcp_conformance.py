"""The Google Cloud adapters against a real project (ADR 0010, CP-113): the nightly
cloud-conformance workflow runs this with the sandbox project's settings. Skipped anywhere else.

VALXEL_GCP_LIVE=1, VALXEL_GCP_PROJECT, VALXEL_GCP_CONFORMANCE_BUCKET (per-object retention on,
with a lifecycle rule that deletes objects once their retention ends) and
VALXEL_GCP_CONFORMANCE_KEY (an RSA_SIGN_PKCS1_2048_SHA256 key version)."""

from __future__ import annotations

import os
import uuid

import pytest

from valxel_sdlc.cloud import conformance

pytestmark = pytest.mark.skipif(os.environ.get("VALXEL_GCP_LIVE") != "1", reason="the nightly job's own")


def test_secret_manager():
    from google.cloud import secretmanager

    from valxel_sdlc.cloud.gcp.secrets import GcpSecretStore, gcp_id
    from valxel_sdlc.secret_ring import secret_id

    project, client = os.environ["VALXEL_GCP_PROJECT"], secretmanager.SecretManagerServiceClient()
    prefix = f"conformance-{uuid.uuid4().hex[:8]}"
    store = GcpSecretStore(project=project, prefix=prefix, client=client, ttl=0)
    made: list[str] = []

    def rotate(name: str, value: str) -> None:
        sid = gcp_id(secret_id(name, prefix))
        parent = f"projects/{project}/secrets/{sid}"
        if parent not in made:
            client.create_secret(request={"parent": f"projects/{project}", "secret_id": sid,
                                          "secret": {"replication": {"automatic": {}},
                                                     "labels": {"valxel-kind": "conformance"}}})
            made.append(parent)
        client.add_secret_version(request={"parent": parent, "payload": {"data": value.encode()}})

    try:
        assert conformance.secret_store(store, set_value=rotate) == []
    finally:
        for parent in made:
            client.delete_secret(request={"name": parent})


def test_cloud_kms():
    from google.cloud import kms

    from valxel_sdlc.cloud.gcp.signing import GcpKmsSigner

    client, key = kms.KeyManagementServiceClient(), os.environ["VALXEL_GCP_CONFORMANCE_KEY"]
    pem = client.get_public_key(request={"name": key}).pem
    assert conformance.signer(GcpKmsSigner(key, client=client), public_pem=pem) == []


def test_cloud_storage_locked_objects():
    from valxel_sdlc.cloud.gcp.storage import GcsImmutable

    store = GcsImmutable(os.environ["VALXEL_GCP_CONFORMANCE_BUCKET"])
    assert conformance.immutable_store(store, prefix=f"conformance/{uuid.uuid4().hex[:8]}/") == []


@pytest.mark.skipif(not os.environ.get("VALXEL_GCP_BILLING_DATASET"), reason="no billing export in this sandbox yet")
def test_the_billing_export_answers():
    """CP-204 against the sandbox's export: the table is there, and a day's rows have the shape."""
    from datetime import UTC, datetime, timedelta

    from valxel_sdlc.cloud.gcp.costs import GcpBillingCosts

    src = GcpBillingCosts(os.environ["VALXEL_GCP_BILLING_DATASET"])
    end = datetime.now(UTC)
    rows = src.cloud_costs_daily(product_id="CONFORMANCE", account=os.environ["VALXEL_GCP_PROJECT"],
                                 start=end - timedelta(days=3), end=end)
    assert all({"day", "service", "feature_id", "usd"} <= set(r) for r in rows)


def test_cloud_asset_inventory_answers():
    """CP-205: the search runs in the sandbox project with the tool's own credentials."""
    from google.cloud import asset_v1

    client = asset_v1.AssetServiceClient()
    got = client.search_all_resources(request={"scope": f"projects/{os.environ['VALXEL_GCP_PROJECT']}",
                                               "query": "labels.valxel_product=conformance"})
    assert all(r.name for r in got)

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

"""Google Cloud adapters (ADR 0010, CP-103 to CP-105): fakes of the client libraries' calls, and the
same conformance checks every adapter passes. The nightly job runs them against a real project."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from valxel_sdlc.cloud import conformance, immutable_store
from valxel_sdlc.cloud.gcp.secrets import GcpSecretStore, GcpVault, gcp_id
from valxel_sdlc.cloud.gcp.signing import GcpKmsSigner
from valxel_sdlc.cloud.gcp.storage import GcsImmutable


class NotFound(Exception):
    code = 404


class PreconditionFailed(Exception):
    code = 412


class FakeSecretManager:
    """versions: secret name -> [(create time, data, enabled)]"""

    def __init__(self):
        self.secrets: dict[str, list] = {}
        self.labels: dict[str, dict] = {}
        self.t = 0

    def list_secret_versions(self, request):
        if request["parent"] not in self.secrets:
            raise NotFound(request["parent"])
        assert request["filter"] == "state:ENABLED"
        return [SimpleNamespace(name=f"{request['parent']}/versions/{i}", create_time=t)
                for i, (t, _, on) in enumerate(self.secrets[request["parent"]]) if on]

    def access_secret_version(self, request):
        parent, _, v = request["name"].rpartition("/versions/")
        if parent not in self.secrets:
            raise NotFound(parent)
        rows = self.secrets[parent]
        data = rows[-1][1] if v == "latest" else rows[int(v)][1]
        return SimpleNamespace(payload=SimpleNamespace(data=data))

    def add_secret_version(self, request):
        if request["parent"] not in self.secrets:
            raise NotFound(request["parent"])
        self.t += 1
        self.secrets[request["parent"]].append((self.t, request["payload"]["data"], True))

    def create_secret(self, request):
        name = f"{request['parent']}/secrets/{request['secret_id']}"
        self.secrets[name] = []
        self.labels[name] = request["secret"]["labels"]

    def delete_secret(self, request):
        if request["name"] not in self.secrets:
            raise NotFound(request["name"])
        del self.secrets[request["name"]]


def test_secret_manager_store_conforms_and_reads_current_then_previous():
    sm = FakeSecretManager()
    store = GcpSecretStore(project="p1", client=sm, ttl=0)

    def rotate(name, value):
        parent = f"projects/p1/secrets/{gcp_id('valxel/platform/' + name)}"
        sm.secrets.setdefault(parent, [])
        sm.add_secret_version({"parent": parent, "payload": {"data": value.encode()}})

    assert conformance.secret_store(store, set_value=rotate) == []
    rotate("VALXEL_CONFORMANCE_SECRET", "third")
    parent = f"projects/p1/secrets/{gcp_id('valxel/platform/VALXEL_CONFORMANCE_SECRET')}"
    t, data, _ = sm.secrets[parent][1]
    sm.secrets[parent][1] = (t, data, False)  # the rotation is finished: the old version is disabled
    assert store.versions("VALXEL_CONFORMANCE_SECRET") == ["third", "first"]


def test_gcp_secret_ids_are_valid_and_distinct():
    assert gcp_id("valxel/platform/VALXEL_SESSION_SECRET") == "valxel__platform__VALXEL_SESSION_SECRET"
    assert gcp_id("valxel/workspaces/acme/jira/a.b") != gcp_id("valxel/workspaces/acme/jira/a-b")
    assert all(c.isalnum() or c in "_-" for c in gcp_id("valxel/workspaces/acme/jira/ana@x.com"))


def test_the_vault_creates_labels_reads_and_deletes():
    sm = FakeSecretManager()
    vault = GcpVault(project="p1", client=sm)
    assert vault.get("valxel/workspaces/acme/jira/ana") is None
    vault.put("valxel/workspaces/acme/jira/ana", {"access_token": "a"})
    vault.put("valxel/workspaces/acme/jira/ana", {"access_token": "b"})
    assert vault.get("valxel/workspaces/acme/jira/ana") == {"access_token": "b"}
    assert list(sm.labels.values()) == [{"valxel-kind": "connection-token"}]
    vault.delete("valxel/workspaces/acme/jira/ana")
    vault.delete("valxel/workspaces/acme/jira/ana")  # already gone: fine
    assert vault.get("valxel/workspaces/acme/jira/ana") is None


def test_cloud_kms_signs_rs256_over_a_sha256_digest():
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding, rsa
    from cryptography.hazmat.primitives.asymmetric.utils import Prehashed

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = "projects/p1/locations/global/keyRings/valxel/cryptoKeys/github-app/cryptoKeyVersions/1"

    class Kms:
        def asymmetric_sign(self, request):
            assert request["name"] == name and len(request["digest"]["sha256"]) == 32
            sig = key.sign(request["digest"]["sha256"], padding.PKCS1v15(), Prehashed(hashes.SHA256()))
            return SimpleNamespace(name=name, signature=sig)

    pem = key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    assert conformance.signer(GcpKmsSigner(name, client=Kms()), public_pem=pem.decode()) == []
    with pytest.raises(ValueError, match="key version"):
        GcpKmsSigner("projects/p1/locations/global/keyRings/valxel/cryptoKeys/github-app", client=Kms())


class FakeGcs:
    def __init__(self):
        self.objects: dict[str, dict] = {}

    def bucket(self, name):
        gcs = self

        class Blob:
            def __init__(self, key):
                self.name, self.metadata, self.content_encoding = key, None, None
                self.retention = SimpleNamespace(mode=None, retain_until_time=None)

            def upload_from_string(self, data, content_type, if_generation_match):
                assert if_generation_match == 0
                if self.name in gcs.objects:
                    raise PreconditionFailed(self.name)
                gcs.objects[self.name] = {"data": data, "type": content_type, "meta": self.metadata,
                                          "encoding": self.content_encoding, "mode": self.retention.mode,
                                          "until": self.retention.retain_until_time, "bucket": name}

            def download_as_bytes(self):
                if self.name not in gcs.objects:
                    raise NotFound(self.name)
                return gcs.objects[self.name]["data"]

        return SimpleNamespace(blob=Blob)

    def list_blobs(self, bucket, prefix=""):
        return [SimpleNamespace(name=k) for k in self.objects if k.startswith(prefix)]


def test_cloud_storage_writes_once_and_locks_each_object():
    gcs = FakeGcs()
    store = GcsImmutable("valxel-anchors", client=gcs, clock=lambda: 1_800_000_000.0)
    assert conformance.immutable_store(store) == []
    store.put("checkpoints/acme/CLN/t/x.jsonl.gz", b"data", retain_days=2190, content_type="application/x-ndjson",
              encoding="gzip", meta={"sha256": "abc"}, scope={"workspace": "acme", "product": "CLN"})
    obj = gcs.objects["checkpoints/acme/CLN/t/x.jsonl.gz"]
    assert obj["mode"] == "Locked" and obj["encoding"] == "gzip" and obj["bucket"] == "valxel-anchors"
    assert (obj["until"] - datetime.fromtimestamp(1_800_000_000, UTC)).days == 2190
    assert obj["meta"] == {"sha256": "abc", "valxel-workspace": "acme", "valxel-product": "CLN"}


def test_the_installations_cloud_picks_the_store_and_the_anchors_work_on_it():
    from valxel_sdlc.audit_anchors import ObjectAnchors

    gcs = FakeGcs()
    store = immutable_store("b", env={"VALXEL_CLOUD": "gcp"}, client=gcs)
    assert isinstance(store, GcsImmutable) and store.where == "gs://b"
    anchors = ObjectAnchors(store, 30)
    anchors.put("changes", {"count": 1, "hash": hashlib.sha256(b"x").hexdigest()})
    assert anchors.list("changes")[0]["count"] == 1


def test_github_app_signing_uses_cloud_kms_on_gcp(monkeypatch):
    from valxel_sdlc import signing
    from valxel_sdlc.cloud.gcp import signing as gcp_signing

    monkeypatch.setattr(gcp_signing, "GcpKmsSigner", lambda key: ("gcp-kms", key))
    env = {"VALXEL_CLOUD": "gcp", "VALXEL_GITHUB_APP_ID": "1",
           "VALXEL_GITHUB_APP_KMS_KEY_ID": "projects/p/locations/l/keyRings/r/cryptoKeys/k/cryptoKeyVersions/1"}
    assert signing.github_app_signer(env)[0] == "gcp-kms"


def test_strict_mode_checks_the_store_and_key_against_the_cloud():
    from valxel_sdlc import strict

    gcp = {"VALXEL_CLOUD": "gcp", "VALXEL_DELIVERY": "live", "VALXEL_GITHUB_APP_KMS_KEY_ID": "alias/valxel-app"}
    assert "Cloud KMS key version" in strict.app_key_in_kms(gcp)[0]
    gcp["VALXEL_GITHUB_APP_KMS_KEY_ID"] = "projects/p/locations/l/keyRings/r/cryptoKeys/k/cryptoKeyVersions/1"
    assert strict.app_key_in_kms(gcp) == []
    assert "AWS KMS key id" in strict.app_key_in_kms({**gcp, "VALXEL_CLOUD": "aws"})[0]
    got = strict.secrets_in_environment({"VALXEL_CLOUD": "gcp", "VALXEL_SECRET_STORE": "aws"}, live=True)
    assert "runs on gcp" in got[0]

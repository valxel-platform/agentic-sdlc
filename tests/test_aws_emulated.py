"""The AWS adapters against moto, a local AWS emulator, through the same conformance checks the GCP
nightly job runs against a real project (valxel_sdlc.cloud.conformance; tracker CP-116).

Moto answers AWS's APIs but enforces no Object Lock, KMS key policy, IAM policy or role trust
condition, and can't import key material: those are checked against the AWS sandbox (CP-119). Each
test here says what moto can't show where that matters."""

from __future__ import annotations

import socket
import uuid
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

pytest.importorskip("moto.server")
boto3 = pytest.importorskip("boto3")

from valxel_sdlc.cloud import conformance  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def moto_url():
    from moto.server import ThreadedMotoServer

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = ThreadedMotoServer(ip_address="127.0.0.1", port=port)
    server.start()
    yield f"http://127.0.0.1:{port}"
    server.stop()


@pytest.fixture
def aws(moto_url, monkeypatch):
    """Point every boto3 client this process makes at moto, as AWS_ENDPOINT_URL does on the laptop."""
    for k, v in {"AWS_ENDPOINT_URL": moto_url, "AWS_ACCESS_KEY_ID": "local", "AWS_SECRET_ACCESS_KEY": "local",
                 "AWS_DEFAULT_REGION": "us-east-1", "AWS_REGION": "us-east-1"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    return lambda service: boto3.client(service, region_name="us-east-1")


def test_secrets_manager_rotation(aws):
    from valxel_sdlc.cloud.aws.secrets import AwsSecretsManagerStore
    from valxel_sdlc.secret_ring import secret_id

    sm, prefix, made = aws("secretsmanager"), f"conf{uuid.uuid4().hex[:6]}", set()
    store = AwsSecretsManagerStore(prefix=prefix, client=sm, ttl=0)

    def rotate(name: str, value: str) -> None:
        sid = secret_id(name, prefix)
        if sid in made:
            sm.put_secret_value(SecretId=sid, SecretString=value)
        else:
            sm.create_secret(Name=sid, SecretString=value)
            made.add(sid)

    assert conformance.secret_store(store, set_value=rotate) == []


def test_kms_signs_rs256(aws):
    """A key made in KMS. The GitHub Apps' keys are imported (BYOK), which moto can't do."""
    from cryptography.hazmat.primitives import serialization

    from valxel_sdlc.cloud.aws.signing import KmsSigner

    kms = aws("kms")
    key = kms.create_key(KeyUsage="SIGN_VERIFY", KeySpec="RSA_2048")["KeyMetadata"]["KeyId"]
    pem = serialization.load_der_public_key(kms.get_public_key(KeyId=key)["PublicKey"]).public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    assert conformance.signer(KmsSigner(key, client=kms), public_pem=pem) == []


def test_s3_archive_writes_once(aws):
    """Written once, read back, listed. Moto records COMPLIANCE locks but lets them be shortened, so
    that the lock holds is the AWS sandbox's to show."""
    from valxel_sdlc.cloud.aws.storage import S3Immutable

    s3, bucket = aws("s3"), f"conf-{uuid.uuid4().hex[:8]}"
    s3.create_bucket(Bucket=bucket, ObjectLockEnabledForBucket=True)
    assert conformance.immutable_store(S3Immutable(bucket, s3=s3)) == []
    key = s3.list_objects_v2(Bucket=bucket)["Contents"][0]["Key"]
    head = s3.head_object(Bucket=bucket, Key=key)
    assert head["ObjectLockMode"] == "COMPLIANCE" and head["ObjectLockRetainUntilDate"] > datetime.now(UTC)


def test_the_connect_stack_deploys_and_its_roles_assume(aws):
    """deploy/customer-stack.yaml as `valxel dev aws connect` deploys it, then the tool's check.
    Moto doesn't evaluate the roles' sts:ExternalId condition, so a wrong external ID isn't refused
    here (conformance.cloud_accounts checks that in the sandbox)."""
    from valxel_sdlc.cloud.aws.local import ACCOUNT, deploy_stack
    from valxel_sdlc.connections import AwsConnect

    ws = f"ws{uuid.uuid4().hex[:6]}"
    cf = aws("cloudformation")
    for mode in ("lite", "full"):  # the second is an update in place
        deploy_stack(cf, ws, external_id="ext-1", mode=mode, template=ROOT / "deploy" / "customer-stack.yaml")
    stack = cf.describe_stacks(StackName=f"valxel-{ws}")["Stacks"][0]
    assert stack["StackStatus"] in ("CREATE_COMPLETE", "UPDATE_COMPLETE")
    roles = {r["RoleName"] for r in aws("iam").list_roles(PathPrefix="/valxel/")["Roles"]}
    assert {f"valxel-{ws}-deploy", f"valxel-{ws}-agents"} <= roles
    got = AwsConnect(valxel_account=ACCOUNT, template_url="https://x").check(ws, account=ACCOUNT, external_id="ext-1")
    assert got["account_id"] == ACCOUNT and got["deploy_role"].endswith(f"valxel-{ws}-deploy")


def test_inventory_reads_the_product_tags(aws):
    from valxel_sdlc.cloud.aws.inventory import AwsTags

    s3, bucket = aws("s3"), f"inv-{uuid.uuid4().hex[:8]}"
    s3.create_bucket(Bucket=bucket)
    s3.put_bucket_tagging(Bucket=bucket, Tagging={"TagSet": [
        {"Key": "valxel:product", "Value": "INV1"}, {"Key": "valxel:resource", "Value": "files"},
        {"Key": "valxel:feature", "Value": "INV1-FEAT-1"}]})
    rows = AwsTags(client_for=lambda account, region: aws("resourcegroupstaggingapi")).tagged(
        product_id="INV1", account="123456789012", region="us-east-1")
    assert rows == [{"arn": f"arn:aws:s3:::{bucket}", "resource": "files", "feature": "INV1-FEAT-1"}]


def test_cost_explorer_reads_moto_rows(aws, moto_url):
    """Moto has no billing data: the rows are scripted (moto's own API), in the order the adapter asks."""
    from valxel_sdlc.cloud.aws.costs import CostExplorerSource

    day, end = "2026-11-20", "2026-11-21"

    def group(keys, usd):
        return {"Keys": keys, "Metrics": {"UnblendedCost": {"Amount": usd, "Unit": "USD"}}}

    def results(*groups):
        period = {"Start": day, "End": end}
        return {"ResultsByTime": [{"TimePeriod": period, "Estimated": False, "Groups": list(groups)}]}

    rows = {"results": [results(group(["Amazon DynamoDB"], "1.25"), group(["AWS Lambda"], "0.50")),
                        results(group(["valxel:feature$F1", "Amazon DynamoDB"], "1.25"),
                                group(["valxel:feature$F1", "AWS Lambda"], "0.50"))]}
    httpx.post(f"{moto_url}/moto-api/static/ce/cost-and-usage-results", json=rows).raise_for_status()
    assert conformance.cost_source(CostExplorerSource(client=aws("ce")), product_id="P1", feature_id="F1",
                                   account="123456789012", day=day,
                                   expect={"Amazon DynamoDB": 1.25, "AWS Lambda": 0.5}) == []


def test_laptop_seed_makes_the_archive_buckets(aws):
    from valxel_sdlc.cloud.aws import local

    assert set(local.seed()) <= {local.CHECKPOINTS, local.ANCHORS}
    assert local.seed() == []  # again: nothing new
    lock = aws("s3").get_object_lock_configuration(Bucket=local.ANCHORS)["ObjectLockConfiguration"]
    assert lock["ObjectLockEnabled"] == "Enabled"


def test_aws_local_refuses_without_an_emulator(monkeypatch):
    from valxel_sdlc.cli import main

    monkeypatch.delenv("AWS_ENDPOINT_URL", raising=False)
    with pytest.raises(SystemExit, match="moto"):
        main(["aws-local", "seed"])
    monkeypatch.setenv("AWS_ENDPOINT_URL", "https://s3.us-east-1.amazonaws.com")
    with pytest.raises(SystemExit, match="moto"):
        main(["aws-local", "seed"])

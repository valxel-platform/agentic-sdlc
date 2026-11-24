"""AWS on the laptop tier: what the tool's settings name, made in moto, a local AWS emulator
(valxel_sdlc.laptop.aws, docs/dev-aws.md; tracker CP-116). `valxel aws-local` runs these inside the
tool's api container, where AWS_ENDPOINT_URL points boto3 at moto.

Moto takes any credentials and enforces no Object Lock, KMS key policy, IAM policy or role trust
condition: these flows run here, but whether the access is right is the AWS sandbox's to show."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

ACCOUNT = "123456789012"  # moto's account
REGION = "us-east-1"
CHECKPOINTS, ANCHORS = "valxel-local-checkpoints", "valxel-local-anchors"
TEMPLATE = Path(os.environ.get("VALXEL_AWS_STACK_TEMPLATE_FILE", "/app/deploy/customer-stack.yaml"))


def client(service: str) -> Any:
    import boto3  # the 'aws' extra

    return boto3.client(service, region_name=REGION)


def seed(s3: Any = None) -> list[str]:
    """The archive buckets, with Object Lock (moto records locks but doesn't enforce them)."""
    s3, made = s3 or client("s3"), []
    have = {b["Name"] for b in s3.list_buckets().get("Buckets", [])}
    for bucket in (CHECKPOINTS, ANCHORS):
        if bucket not in have:
            s3.create_bucket(Bucket=bucket, ObjectLockEnabledForBucket=True)
            made.append(bucket)
    return made


def deploy_stack(cf: Any, workspace: str, *, external_id: str, mode: str, template: Path | None = None) -> str:
    """What a customer does in the AWS console: the connect stack (deploy/customer-stack.yaml) for
    the workspace. Moto can't update it in place (it makes the roles again), so a second connect
    replaces it."""
    params = {"ValxelAccountId": ACCOUNT, "ExternalId": external_id, "WorkspaceId": workspace, "Mode": mode}
    name = f"valxel-{workspace}"
    kw = {"StackName": name, "TemplateBody": (template or TEMPLATE).read_text(),
          "Capabilities": ["CAPABILITY_NAMED_IAM"],
          "Parameters": [{"ParameterKey": k, "ParameterValue": v} for k, v in params.items()]}
    live = {s["StackName"] for s in cf.list_stacks().get("StackSummaries", []) if s["StackStatus"] != "DELETE_COMPLETE"}
    if name in live:
        cf.delete_stack(StackName=name)
    cf.create_stack(**kw)
    return name

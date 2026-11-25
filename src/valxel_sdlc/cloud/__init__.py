"""The cloud an installation runs on, and the ports the core reaches it through (ADR 0010).

One installation runs on one cloud, and the products its customers build run on that same cloud:
no installation plans across clouds. `VALXEL_CLOUD` names it (`aws` by default). The core never
imports a cloud's SDK: it uses the ports in `ports.py`, and each cloud's adapters live in
`cloud/<provider>/`, installed with that cloud's extra (`pip install .[aws]`). config/seams.yaml's
`cloud_sdk` rule keeps the SDKs there.

`fakes.py` holds an in-memory stand-in for every port, and `conformance.py` the checks each
adapter must pass: the fakes pass them in every test run, and a real adapter passes the same
checks against its cloud.
"""

from __future__ import annotations

import os
import time
from collections.abc import Mapping
from typing import Any

CLOUDS = ("aws", "gcp", "azure")
# Clouds with a full set of adapters. Strict mode refuses any other: a cloud named here before its
# adapters land would quietly fall back to stand-ins.
SUPPORTED = ("aws", "gcp", "azure")
DEFAULT = "aws"


class UnknownCloud(ValueError):
    pass


def provider(env: Mapping[str, str] | None = None) -> str:
    """The installation's cloud: VALXEL_CLOUD, lower-cased, `aws` if unset."""
    env = env if env is not None else os.environ
    name = (env.get("VALXEL_CLOUD") or DEFAULT).strip().lower()
    if name not in CLOUDS:
        raise UnknownCloud(f"VALXEL_CLOUD is one of {', '.join(CLOUDS)}, not {name!r}")
    return name


def strict_problems(env: Mapping[str, str] | None = None) -> list[str]:
    """What strict mode says about the installation's cloud."""
    try:
        name = provider(env)
    except UnknownCloud as exc:
        return [str(exc)]
    if name not in SUPPORTED:
        return [f"VALXEL_CLOUD is {name}, which has no adapters yet (supported: {', '.join(SUPPORTED)}): "
                "its secrets, signing, archives and costs would run as stand-ins"]
    env = env if env is not None else os.environ
    # An endpoint override sends AWS calls to an emulator (moto on the laptop tier, docs/dev-aws.md),
    # which takes any credentials and enforces no lock, key or IAM policy.
    emulated = sorted(k for k, v in env.items() if k.startswith("AWS_ENDPOINT_URL") and v)
    if emulated:
        return [f"{', '.join(emulated)} is set: AWS calls go to {env[emulated[0]]}, not AWS (an emulator enforces "
                "no Object Lock, KMS or IAM policy)"]
    return []


def immutable_store(bucket: str, *, env: Mapping[str, str] | None = None, client: Any = None, clock=time.time):
    """The installation's ImmutableStore on `bucket`: S3 Object Lock, Cloud Storage with locked
    object retention, or a Blob Storage container with locked version-level immutability
    (`https://<account>.blob.core.windows.net/<container>`). `client` is the cloud's storage client
    (tests pass a fake)."""
    name = provider(env)
    if name == "aws":
        from .aws.storage import S3Immutable

        return S3Immutable(bucket, s3=client, clock=clock)
    if name == "gcp":
        from .gcp.storage import GcsImmutable

        return GcsImmutable(bucket, client=client, clock=clock)
    if name == "azure":
        from .azure.storage import AzureBlobImmutable

        account_url, _, container = bucket.rstrip("/").rpartition("/")
        if not account_url.startswith("https://") or not container:
            raise ValueError(f"On Azure a locked bucket is https://<account>.blob.core.windows.net/<container>, "
                             f"not {bucket!r}")
        return AzureBlobImmutable(container, account_url=account_url, client=client, clock=clock)
    raise UnknownCloud(f"no immutable store for {name} yet")


def database_copies(env: Mapping[str, str] | None = None):
    """The installation's DatabaseCopies (the release rehearsal): RDS point-in-time copies in
    AWS_REGION, Cloud SQL clones in VALXEL_GCP_PROJECT, or Flexible Server restores beside
    REHEARSAL_SOURCE (the source server's resource id)."""
    env = env if env is not None else os.environ
    name = provider(env)
    if name == "aws":
        from .aws.rehearsal import RdsCopies

        return RdsCopies(env.get("AWS_REGION") or env.get("AWS_DEFAULT_REGION") or "",
                         instance_class=env.get("REHEARSAL_INSTANCE_CLASS", ""))
    if name == "gcp":
        from . import gcp
        from .gcp.rehearsal import CloudSqlCopies

        return CloudSqlCopies(gcp.project(env))
    if name == "azure":
        from .azure.rehearsal import FlexibleServerCopies

        return FlexibleServerCopies(env.get("REHEARSAL_SOURCE", ""))
    raise UnknownCloud(f"no database copies for {name} yet")


def report_store(bucket: str, *, env: Mapping[str, str] | None = None, client: Any = None):
    """The installation's ReportStore on `bucket`: S3, Cloud Storage (gs://NAME or NAME), or a Blob
    Storage container (https://<account>.blob.core.windows.net/<container>)."""
    env = env if env is not None else os.environ
    name = provider(env)
    if name == "aws":
        from .aws.rehearsal import S3Reports

        return S3Reports(bucket, region=env.get("AWS_REGION", ""), client=client)
    if name == "gcp":
        from .gcp.rehearsal import GcsReports

        return GcsReports(bucket, client=client)
    if name == "azure":
        from .azure.rehearsal import BlobReports

        return BlobReports(bucket, client=client)
    raise UnknownCloud(f"no report store for {name} yet")


# Tags and labels (ADR 0010). AWS and Azure tags are free-form (`valxel:feature` = `FARM-FEAT-1`). Google Cloud
# labels allow only lowercase letters, digits, `_` and `-`, up to 63 characters: `valxel:feature`
# becomes `valxel_feature` and its value `farm-feat-1`. Valxel's ids are uppercase letters, digits
# and hyphens, so a label's value reads back as the id by upper-casing it.
def label_key(tag: str) -> str:
    return "".join(c if c.isalnum() or c in "_-" else "_" for c in tag.lower())[:63]


def label_value(value: str) -> str:
    return "".join(c if c.isalnum() or c in "_-" else "_" for c in value.lower())[:63]


def id_from_label(value: str) -> str:
    return value.upper()


def tags_for(cloud: str, tags: dict[str, str]) -> dict[str, str]:
    """A resource's Valxel tags as the cloud spells them."""
    if cloud == "gcp":
        return {label_key(k): label_value(v) for k, v in tags.items()}
    return dict(tags)

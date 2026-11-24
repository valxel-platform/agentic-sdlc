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
SUPPORTED = ("aws", "gcp")
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
    return []


def immutable_store(bucket: str, *, env: Mapping[str, str] | None = None, client: Any = None, clock=time.time):
    """The installation's ImmutableStore on `bucket`: S3 Object Lock, or Cloud Storage with locked
    object retention. `client` is the cloud's storage client (tests pass a fake)."""
    name = provider(env)
    if name == "aws":
        from .aws.storage import S3Immutable

        return S3Immutable(bucket, s3=client, clock=clock)
    if name == "gcp":
        from .gcp.storage import GcsImmutable

        return GcsImmutable(bucket, client=client, clock=clock)
    raise UnknownCloud(f"no immutable store for {name} yet")

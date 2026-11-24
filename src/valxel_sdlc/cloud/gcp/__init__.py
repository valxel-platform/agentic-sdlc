"""Google Cloud adapters for the ports in valxel_sdlc.cloud.ports (ADR 0010): the only modules that
import the Google Cloud client libraries. Installed with the `gcp` extra. Credentials are the
process's own (GKE Workload Identity): no key files.

The project is VALXEL_GCP_PROJECT, else GOOGLE_CLOUD_PROJECT."""

from __future__ import annotations

import os
from collections.abc import Mapping


def project(env: Mapping[str, str] | None = None) -> str:
    env = env if env is not None else os.environ
    got = env.get("VALXEL_GCP_PROJECT") or env.get("GOOGLE_CLOUD_PROJECT") or ""
    if not got:
        raise RuntimeError("VALXEL_GCP_PROJECT is not set: the Google Cloud project the tool runs in")
    return got


def not_found(exc: Exception) -> bool:
    """google.api_core's NotFound, without importing it."""
    return type(exc).__name__ == "NotFound" or getattr(exc, "code", None) == 404


def precondition_failed(exc: Exception) -> bool:
    return type(exc).__name__ == "PreconditionFailed" or getattr(exc, "code", None) == 412

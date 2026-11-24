"""Azure adapters for the ports in valxel_sdlc.cloud.ports (ADR 0010): the only modules that
import the Azure SDK. Installed with the `azure` extra. Credentials are the process's own
(DefaultAzureCredential: AKS Workload Identity in the cluster, no secrets or certificates).

The subscription the tool runs in is VALXEL_AZURE_SUBSCRIPTION, else AZURE_SUBSCRIPTION_ID; its
Key Vault is VALXEL_AZURE_KEY_VAULT (https://<name>.vault.azure.net)."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from typing import Any

SUBSCRIPTION = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def subscription(env: Mapping[str, str] | None = None) -> str:
    env = env if env is not None else os.environ
    got = (env.get("VALXEL_AZURE_SUBSCRIPTION") or env.get("AZURE_SUBSCRIPTION_ID") or "").lower()
    if not got:
        raise RuntimeError("VALXEL_AZURE_SUBSCRIPTION is not set: the Azure subscription the tool runs in")
    return got


def key_vault(env: Mapping[str, str] | None = None) -> str:
    env = env if env is not None else os.environ
    url = env.get("VALXEL_AZURE_KEY_VAULT", "")
    if not url.startswith("https://"):
        raise RuntimeError("VALXEL_AZURE_KEY_VAULT is not set: the tool's Key Vault, https://<name>.vault.azure.net")
    return url.rstrip("/")


def credential() -> Any:
    from azure.identity import DefaultAzureCredential  # the 'azure' extra

    return DefaultAzureCredential()


def not_found(exc: Exception) -> bool:
    """azure.core's ResourceNotFoundError, without importing it."""
    return type(exc).__name__ == "ResourceNotFoundError" or getattr(exc, "status_code", None) == 404


def exists(exc: Exception) -> bool:
    return type(exc).__name__ == "ResourceExistsError" or getattr(exc, "status_code", None) in (409, 412)

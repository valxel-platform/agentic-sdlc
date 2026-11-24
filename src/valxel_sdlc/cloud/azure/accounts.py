"""Reaching a customer's Azure subscription (CloudAccounts; tracker CP-404).

The connect template (deploy/azure-connect, Bicep at subscription scope) makes a resource group
`valxel` with two user-assigned managed identities, `valxel-deploy` and `valxel-agents`, each with
its roles on the subscription and a federated credential that trusts Valxel's own workload
identity (VALXEL_AZURE_FEDERATION_ISSUER and _SUBJECT: the cluster's OIDC issuer and the tool's
Kubernetes service account). No secret crosses: for each job Valxel trades its own projected
service-account token for a one-hour token of that identity, in the customer's tenant.

Azure has no external ID, so the template tags both identities with the workspace and a
fingerprint of its external ID (`connect_label`), and `check` reads them back through the identity
itself: a subscription set up for one workspace can't be claimed by another (the confused
deputy), because the other workspace's fingerprint doesn't match. It also checks that the client
IDs given are these identities', so a connection can't point at someone else's.

A connection's `reach` keeps the tenant and the two client IDs; `token` needs them. The services
point `set_reach` at the connections store, so cost and inventory reads find them by subscription.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from . import SUBSCRIPTION

JOBS = ("deploy", "agents")
ARM = "https://management.azure.com"
SCOPE = "https://management.azure.com/.default"
REPO = "https://github.com/valxel-platform/agentic-sdlc"
GUID = SUBSCRIPTION
IDENTITY_API = "2023-01-31"


_reach: Callable[[str], dict] = lambda subscription: {}  # noqa: E731


def set_reach(lookup: Callable[[str], dict]) -> None:
    """Where every AzureConnect finds a connected subscription's tenant and client IDs."""
    global _reach  # noqa: PLW0603 - one lookup for the process, set once when the services start
    _reach = lookup


def connect_label(external_id: str) -> str:
    """What the template writes for the workspace's external ID: a fingerprint, not the ID."""
    return hashlib.sha256(external_id.encode()).hexdigest()[:40]


def identity_path(subscription: str, job: str) -> str:
    return (f"/subscriptions/{subscription}/resourceGroups/valxel/providers/"
            f"Microsoft.ManagedIdentity/userAssignedIdentities/valxel-{job}")


def tenant_of(subscription: str) -> str:
    """The subscription's tenant, from ARM's sign-in challenge (no credentials needed)."""
    try:
        urlopen(Request(f"{ARM}/subscriptions/{subscription}?api-version=2022-12-01"), timeout=15)  # noqa: S310 - fixed host
    except HTTPError as exc:
        found = re.search(r"login\.microsoftonline\.com/([0-9a-f-]{36})", exc.headers.get("WWW-Authenticate", ""))
        if found:
            return found.group(1)
    return ""


def federated_token(tenant: str, client_id: str, token_file: str) -> str:
    """A token for `client_id` in `tenant`, for Azure Resource Manager, traded for the tool's own
    projected service-account token (the 'azure' extra)."""
    from azure.identity import ClientAssertionCredential

    cred = ClientAssertionCredential(tenant, client_id, lambda: Path(token_file).read_text().strip())
    return str(cred.get_token(SCOPE).token)


def arm_get(path: str, token: str, api_version: str = IDENTITY_API) -> dict:
    url = f"{ARM}{path}?api-version={api_version}"
    with urlopen(Request(url, headers={"Authorization": f"Bearer {token}"}), timeout=15) as resp:  # noqa: S310 - fixed host
        return json.loads(resp.read())


def azure_reason(exc: Exception, job: str, subscription: str) -> str:
    text = str(exc)
    if "AADSTS70021" in text or "AADSTS700213" in text or "No matching federated identity" in text:
        return (f"Valxel can't sign in as the {job} identity in {subscription}: its federated credential doesn't "
                "trust this Valxel. Run the connect template again from the connections page")
    if "AADSTS700016" in text or "AADSTS90002" in text:
        return (f"The {job} client ID isn't an identity in that subscription's tenant: copy it from the "
                "template's output")
    if "404" in text or "ResourceNotFound" in text or "ResourceGroupNotFound" in text:
        return f"There is no valxel-{job} identity in {subscription}: deploy the connect template there first"
    if "403" in text or "AuthorizationFailed" in text:
        return f"The {job} identity in {subscription} has lost its roles: deploy the connect template again"
    return f"Checking the {job} identity in {subscription} failed: {text[:200]}"


class AzureConnect:
    """Azure's side of a workspace connection, the counterpart of AwsConnect and GcpConnect."""

    def __init__(self, *, issuer: str, subject: str, token_file: str = "", repo: str = REPO,
                 exchange: Callable[[str, str], str] | None = None, read: Callable[[str, str], dict] | None = None,
                 tenant: Callable[[str], str] | None = None, reach_of: Callable[[str], dict] | None = None) -> None:
        self.issuer, self.subject, self.repo = issuer, subject, repo
        self.token_file = token_file or os.environ.get("AZURE_FEDERATED_TOKEN_FILE", "")
        self._exchange = exchange or (lambda tenant, client: federated_token(tenant, client, self.token_file))
        self._read = read or arm_get
        self._tenant = tenant or tenant_of
        # subscription -> a connection's reach (tenant, client IDs): set by the services from the
        # connections store, for the cost and inventory reads.
        # Late-bound: set_reach may run after this connect is made.
        self.reach_of = reach_of or (lambda subscription: _reach(subscription))  # noqa: PLW0108

    @classmethod
    def from_env(cls, env: Any = None) -> AzureConnect | None:
        env = env if env is not None else os.environ
        issuer, subject = env.get("VALXEL_AZURE_FEDERATION_ISSUER", ""), env.get("VALXEL_AZURE_FEDERATION_SUBJECT", "")
        if not (issuer and subject):
            return None
        return cls(issuer=issuer, subject=subject, token_file=env.get("AZURE_FEDERATED_TOKEN_FILE", ""),
                   repo=env.get("VALXEL_AZURE_CONNECT_REPO") or REPO)

    @staticmethod
    def roles(workspace: str, account: str) -> dict[str, str]:
        return {job: identity_path(account, job) for job in JOBS}

    def launch_url(self, workspace: str, *, external_id: str, mode: str, region: str) -> str:
        """Azure Cloud Shell; the command (below) deploys the template there."""
        return "https://shell.azure.com/bash"

    def command(self, workspace: str, *, external_id: str, mode: str, region: str) -> str:
        """What to run in Cloud Shell: the template, with this workspace's values."""
        params = {"workspace": workspace, "connectLabel": connect_label(external_id), "mode": mode,
                  "valxelIssuer": self.issuer, "valxelSubject": self.subject}
        return (f"git clone --depth 1 {self.repo} valxel-connect && cd valxel-connect/deploy/azure-connect && "
                f"az deployment sub create --name valxel-connect --location {region} --template-file main.bicep "
                + "--parameters " + " ".join(f"{k}={v}" for k, v in params.items())
                + " --query properties.outputs.connection.value -o json")

    def token(self, account: str, job: str, reach: dict | None = None) -> str:
        """A token for one job's identity in the subscription."""
        reach = reach or self.reach_of(account)
        if not reach.get("tenant") or not reach.get(f"{job}_client_id"):
            raise RuntimeError(f"No Azure connection for {account}: connect it from the workspace's page")
        return self._exchange(reach["tenant"], reach[f"{job}_client_id"])

    def check(self, workspace: str, *, account: str, external_id: str, deploy_client_id: str = "",
              agents_client_id: str = "", tenant: str = "") -> dict:
        from ...connections import ConnectionError_

        account = (account or "").strip().lower()
        if not SUBSCRIPTION.match(account):
            raise ConnectionError_("An Azure subscription ID is a GUID, like 1b2c3d4e-...")
        given = {"deploy": deploy_client_id.strip().lower(), "agents": agents_client_id.strip().lower()}
        if not all(GUID.match(c) for c in given.values()):
            given = {job: (self.reach_of(account).get(f"{job}_client_id") or "") for job in JOBS}
            if not all(given.values()):
                raise ConnectionError_("Paste the deploy and agents client IDs the connect template printed")
        tenant = (tenant or self.reach_of(account).get("tenant") or self._tenant(account)).lower()
        if not GUID.match(tenant):
            raise ConnectionError_(f"Couldn't find {account}'s tenant: check the subscription ID")
        want = {"valxel-workspace": workspace, "valxel-connect": connect_label(external_id)}
        for job in JOBS:
            try:
                got = self._read(identity_path(account, job), self._exchange(tenant, given[job]))
            except Exception as exc:
                raise ConnectionError_(azure_reason(exc, job, account)) from exc
            tags = got.get("tags") or {}
            if any(tags.get(k) != v for k, v in want.items()):
                raise ConnectionError_(f"The {job} identity in {account} was set up for another workspace, or with "
                                       "an old link: deploy the connect template again from this workspace's page")
            if str((got.get("properties") or {}).get("clientId") or "").lower() != given[job]:
                raise ConnectionError_(f"The {job} client ID isn't valxel-{job}'s in {account}: copy it from the "
                                       "template's output")
        roles = self.roles(workspace, account)
        return {"account_id": account, "tenant": tenant, "deploy_role": roles["deploy"], "agents_role": roles["agents"],
                "deploy_client_id": given["deploy"], "agents_client_id": given["agents"]}

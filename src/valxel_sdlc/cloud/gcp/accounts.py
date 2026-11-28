"""Reaching a customer's Google Cloud project (CloudAccounts; tracker CP-202).

The connect template (deploy/gcp-connect) makes two service accounts in the customer's project,
`valxel-deploy` and `valxel-agents`, and lets Valxel's own service account (VALXEL_GCP_SERVICE_ACCOUNT,
the identity the tool runs as through GKE Workload Identity, or Workload Identity Federation from
elsewhere) impersonate each, for 15 minutes at a time, for one job.

Google Cloud has no external ID, so the template writes the workspace and a fingerprint of its
external ID (`connect_label`) into each account's description, and `check` reads them back through
the account itself: a project set up for one workspace can't be claimed by another (the confused
deputy), because the other workspace's fingerprint doesn't match.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Callable
from typing import Any
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

JOBS = ("deploy", "agents")
SCOPES = ["https://www.googleapis.com/auth/cloud-platform"]
PROJECT = re.compile(r"^[a-z][a-z0-9-]{4,28}[a-z0-9]$")
REPO = "https://github.com/valxel-platform/agentic-sdlc"


def connect_label(external_id: str) -> str:
    """What the template writes for the workspace's external ID: a fingerprint, not the ID."""
    return hashlib.sha256(external_id.encode()).hexdigest()[:40]


def description(workspace: str, external_id: str) -> str:
    return f"valxel workspace={workspace} connect={connect_label(external_id)}"


def impersonated_token(source_account: str, target: str, lifetime: int = 900) -> str:
    """An access token for `target`, made by the tool's own credentials (the 'gcp' extra)."""
    import google.auth
    import google.auth.transport.requests
    from google.auth import impersonated_credentials

    source, _ = google.auth.default(scopes=SCOPES)
    creds = impersonated_credentials.Credentials(source_credentials=source, target_principal=target,
                                                 target_scopes=SCOPES, lifetime=lifetime)
    creds.refresh(google.auth.transport.requests.Request())  # type: ignore[no-untyped-call]
    return str(creds.token)


def read_account(email: str, token: str) -> dict:
    """The service account's own record, read with its own token."""
    url = f"https://iam.googleapis.com/v1/projects/-/serviceAccounts/{quote(email)}"
    with urlopen(Request(url, headers={"Authorization": f"Bearer {token}"}), timeout=15) as resp:
        return json.loads(resp.read())


def gcp_reason(exc: Exception, job: str, project: str) -> str:
    text = str(exc)
    if "403" in text or "PERMISSION_DENIED" in text or "iam.serviceAccounts.getAccessToken" in text:
        return (f"Valxel can't act as the {job} account in {project}: the connect template hasn't been applied "
                "there (or was removed), or it named another Valxel. Run it again from the connections page")
    if "404" in text or "NOT_FOUND" in text:
        return f"There is no valxel-{job} service account in {project}: apply the connect template there first"
    return f"Checking the {job} account in {project} failed: {text[:200]}"


class GcpConnect:
    """Google Cloud's side of a workspace connection, the counterpart of AwsConnect."""

    def __init__(self, *, valxel_service_account: str, repo: str = REPO,
                 impersonate: Callable[[str, str], str] | None = None,
                 read: Callable[[str, str], dict] | None = None) -> None:
        self.valxel_service_account, self.repo = valxel_service_account, repo
        self._impersonate = impersonate or impersonated_token
        self._read = read or read_account

    @classmethod
    def from_env(cls, env: Any = None) -> GcpConnect | None:
        env = env if env is not None else os.environ
        sa = env.get("VALXEL_GCP_SERVICE_ACCOUNT", "")
        return cls(valxel_service_account=sa, repo=env.get("VALXEL_GCP_CONNECT_REPO") or REPO) if sa else None

    @staticmethod
    def roles(workspace: str, account: str) -> dict[str, str]:
        return {job: f"valxel-{job}@{account}.iam.gserviceaccount.com" for job in JOBS}

    def launch_url(self, workspace: str, *, external_id: str, mode: str, region: str) -> str:
        """Cloud Shell, with the connect template open and its walkthrough beside it."""
        q = urlencode({"cloudshell_git_repo": self.repo, "cloudshell_workspace": "deploy/gcp-connect",
                       "cloudshell_tutorial": "TUTORIAL.md", "show": "ide,terminal"})
        return f"https://shell.cloud.google.com/cloudshell/editor?{q}"

    def command(self, workspace: str, *, external_id: str, mode: str, region: str,
                project: str = "") -> str:
        """What to run in that shell: the template, with this workspace's values (and the project, when
        Valxel knows which one: a new product's, from inception)."""
        args = {"project": project or "YOUR_PROJECT_ID", "workspace": workspace,
                "connect_label": connect_label(external_id),
                "mode": mode, "region": region, "valxel_service_account": self.valxel_service_account}
        return "terraform init && terraform apply " + " ".join(f"-var {k}={v}" for k, v in args.items())

    def token(self, account: str, job: str) -> str:
        """A 15-minute token for one job in the project (the deploy or the agents account)."""
        return self._impersonate(self.valxel_service_account, self.roles("", account)[job])

    def check(self, workspace: str, *, account: str, external_id: str) -> dict:
        from ...connections import ConnectionError_

        if not PROJECT.match(account or ""):
            raise ConnectionError_("A Google Cloud project ID is 6 to 30 lowercase letters, digits and hyphens")
        want = description(workspace, external_id)
        for job, email in self.roles(workspace, account).items():
            try:
                got = self._read(email, self._impersonate(self.valxel_service_account, email))
            except Exception as exc:
                raise ConnectionError_(gcp_reason(exc, job, account)) from exc
            if str(got.get("description") or "").strip() != want:
                raise ConnectionError_(f"The {job} account in {account} was set up for another workspace, or with "
                                       "an old link: apply the connect template again from this workspace's page")
        roles = self.roles(workspace, account)
        return {"account_id": account, "deploy_role": roles["deploy"], "agents_role": roles["agents"]}

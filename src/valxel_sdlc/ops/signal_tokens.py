"""Who may send which signals: tokens scoped to the sender, not one for the whole platform.

Every token is derived from VALXEL_SIGNALS_TOKEN (and its previous value while it rotates), so the
tool stores one secret and each sender holds only its own:

  product:<PRODUCT>   a product's own alert sources: Alertmanager, OpenChoreo notification
                      channels, a support tool's tickets. Signals for that product only.
  account:<ACCOUNT>   a data plane account's EventBridge senders: CloudWatch alarms (AlarmRouting)
                      and Inspector findings. Events from that AWS account only.
  project:<PROJECT>   a Google Cloud project's senders: Cloud Monitoring's webhook channel and
                      Security Command Center's Pub/Sub push. Events from that project only.

`valxel signals-token product CLN` or `valxel signals-token account 123456789012` prints one. A
leaked token can then raise or resolve signals for its own product or account and nothing else;
rotating VALXEL_SIGNALS_TOKEN replaces them all.

The platform-wide token itself is still accepted on these endpoints, for every product, unless
VALXEL_SIGNALS_TOKENS=scoped (strict mode asks for it). AWS Budgets keep it: their SNS topic is the
platform's own, in the tools account.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
from dataclasses import dataclass

NAME = "VALXEL_SIGNALS_TOKEN"
KINDS = ("product", "account", "project")
PROJECT_ID = re.compile(r"[a-z][a-z0-9-]{4,28}[a-z0-9]")


@dataclass(frozen=True)
class Scope:
    kind: str  # platform | product | account
    name: str = ""

    def allows(self, signal: dict) -> bool:
        """Whether a signal this sender raised is one it may raise."""
        if self.kind == "platform":
            return True
        if self.kind == "product":
            return str(signal.get("product_id") or "") == self.name
        return str((signal.get("labels") or {}).get(self.kind) or "") == self.name


def derive(secret: str, kind: str, name: str) -> str:
    if kind not in KINDS:
        raise ValueError(f"a signals token is for a product, an account or a project, not {kind!r}")
    if kind == "project" and not PROJECT_ID.fullmatch(name):
        raise ValueError("a Google Cloud project ID is 6 to 30 lowercase letters, digits and hyphens")
    if kind == "account" and not re.fullmatch(r"\d{12}", name):
        raise ValueError("an AWS account is 12 digits")
    if kind == "product" and not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", name):
        raise ValueError(f"not a product id: {name!r}")
    return "vxs_" + hmac.new(secret.encode(), f"signals:{kind}:{name}".encode(), hashlib.sha256).hexdigest()[:40]


def token_for(kind: str, name: str, secret: str | None = None) -> str:
    if secret is None:
        from ..secret_ring import current

        secret = current(NAME)
    return derive(secret, kind, name) if secret else ""


def scoped_only(env=None) -> bool:
    return str((env if env is not None else os.environ).get("VALXEL_SIGNALS_TOKENS", "")).lower() == "scoped"


def scope_of(header: str, kinds: tuple[str, ...], products: list[str], env=None) -> Scope | None:
    """The sender an Authorization header names, or None. `kinds`: which scoped tokens this endpoint
    takes; an account token names its account by the token's own shape (vxs_...), so it is checked
    against the account the signals claim (Scope.allows)."""
    from ..secret_ring import values

    got = header[7:].strip() if header.lower().startswith("bearer ") else ""
    if not got:
        return None
    secrets = values(NAME, env)
    if not scoped_only(env) and any(hmac.compare_digest(got, s) for s in secrets):
        return Scope("platform")
    if not got.startswith("vxs_"):
        return None
    for secret in secrets:
        if "product" in kinds:
            for pid in products:
                if hmac.compare_digest(got, derive(secret, "product", pid)):
                    return Scope("product", pid)
    if "account" in kinds:
        return Scope("account?", got)
    return Scope("project?", got) if "project" in kinds else None


def account_scope(candidate: Scope, account: str, env=None) -> Scope | None:
    """An account token checked against the account the event comes from."""
    from ..secret_ring import values

    if candidate.kind != "account?":
        return candidate
    if not re.fullmatch(r"\d{12}", account or ""):
        return None
    if any(hmac.compare_digest(candidate.name, derive(s, "account", account)) for s in values(NAME, env)):
        return Scope("account", account)
    return None


def project_scope(candidate: Scope, project: str, env=None) -> Scope | None:
    """A project token checked against the Google Cloud project the event comes from."""
    from ..secret_ring import values

    if candidate.kind != "project?":
        return candidate
    if not PROJECT_ID.fullmatch(project or ""):
        return None
    if any(hmac.compare_digest(candidate.name, derive(s, "project", project)) for s in values(NAME, env)):
        return Scope("project", project)
    return None

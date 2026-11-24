"""The ports: what the core needs from a cloud, and nothing about which one (ADR 0010).

Each is a small Protocol. A cloud implements it in `cloud/<provider>/`; `fakes.py` has an
in-memory one for each, and `conformance.py` the behavior every implementation must show.

    SecretStore        the tool's secrets, current and previous version (secret_ring)
    Signer             RS256 signing with a key the tool never holds (signing: the GitHub Apps)
    AnchorStore        hash-chain heads, kept where the tool can't rewrite them (audit_anchors)
    ImmutableStore     objects that no one can change or delete until their retention ends
    IdentityProvider   who signed in: an OIDC issuer's verified claims, mapped to a user
    CloudAccounts      reaching a customer's account or project, one short-lived session per job
    CostSource         what a product's features and components cost (ops.cost, finops)
    ResourceInventory  which cloud resources a product owns, by its labels or tags (ops.inventory)
    SignalSource       a cloud's alarms, budget alerts and findings, parsed into signals (ops.signals)

The first four and CostSource were already interfaces; they live here now, and the modules that
used them re-export them under the same names. CostSource's cloud methods are cloud_costs and
cloud_costs_daily (aws_costs before the GCP adapter).
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable


@runtime_checkable
class SecretStore(Protocol):
    def versions(self, name: str) -> list[str]: ...  # [current] or [current, previous]; [] if unset


@runtime_checkable
class Signer(Protocol):
    def sign(self, message: bytes) -> bytes: ...  # RSASSA-PKCS1-v1_5 with SHA-256


@runtime_checkable
class AnchorStore(Protocol):
    def put(self, chain: str, h: dict) -> None: ...
    def list(self, chain: str, limit: int = 0) -> list[dict]: ...  # newest last; limit 0 = all


@runtime_checkable
class ImmutableStore(Protocol):
    """Write once: `put` of a key that exists raises FileExistsError, and no one (the tool
    included) can change or delete an object until `retain_days` have passed: S3 Object Lock in
    compliance mode, or Cloud Storage's locked object retention.

    `meta` is the object's own metadata. `scope` names whose object it is (workspace, product):
    where the cloud can, reading it is limited to that scope (on AWS: tags the bucket policy
    matches, and the KMS encryption context); everywhere, the key's path names it too."""

    def put(self, key: str, data: bytes, *, retain_days: int, content_type: str = "application/octet-stream",
            encoding: str = "", meta: dict[str, str] | None = None, scope: dict[str, str] | None = None) -> None: ...
    def get(self, key: str) -> bytes: ...  # KeyError if absent
    def keys(self, prefix: str = "") -> list[str]: ...  # sorted


@runtime_checkable
class IdentityProvider(Protocol):
    """Sign-in through an OIDC issuer: Cognito, Identity Platform or Entra ID are each a profile."""

    issuer: str

    def claims(self, id_token: str) -> dict: ...  # verified claims; ValueError if invalid
    def user(self, claims: dict) -> dict: ...  # {"login", "email", "name", "groups": [...]}


@runtime_checkable
class CloudAccounts(Protocol):
    """A customer's account (AWS) or project (GCP) or subscription (Azure), reached per job."""

    def roles(self, workspace: str, account: str) -> dict[str, str]: ...  # job -> role or service account
    def check(self, workspace: str, *, account: str, external_id: str) -> dict: ...  # ConnectionError_ if not


@runtime_checkable
class CostSource(Protocol):
    def component_costs(self, *, namespace: str, project: str, env: str, components: list[str],
                        start: datetime, end: datetime) -> dict[str, float]: ...  # component -> USD
    def cloud_costs(self, *, product_id: str, feature_id: str, account: str,
                  start: datetime, end: datetime) -> dict[str, float]: ...  # service -> USD
    # For the cost ledger (valxel_sdlc.finops): day by day, everything in a project or account.
    def component_costs_daily(self, *, namespace: str, project: str, env: str, start: datetime,
                              end: datetime) -> list[dict]: ...  # [{day, component, usd}]
    def cloud_costs_daily(self, *, product_id: str, account: str, start: datetime,
                        end: datetime) -> list[dict]: ...  # [{day, service, feature_id ('' = untagged), usd}]


@runtime_checkable
class ResourceInventory(Protocol):
    def resources(self, *, account: str, product_id: str) -> list[dict]: ...  # [{id, type, feature_id, labels}]


@runtime_checkable
class SignalSource(Protocol):
    """One kind of cloud event (an alarm, a budget alert, a security finding) as signals."""

    kind: str

    def parse(self, payload: dict) -> list[dict]: ...  # ops.signals' shape; [] if not this kind


PORTS = (SecretStore, Signer, AnchorStore, ImmutableStore, IdentityProvider, CloudAccounts, CostSource,
         ResourceInventory, SignalSource)

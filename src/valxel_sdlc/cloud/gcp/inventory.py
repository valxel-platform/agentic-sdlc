"""What carries a product's labels in a Google Cloud project: Cloud Asset Inventory
(ResourceInventory; tracker CP-205). Run as the project's valxel-agents account (roles
cloudasset.viewer), impersonated for the job through GcpConnect."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .. import id_from_label, label_key, label_value

PRODUCT, FEATURE, RESOURCE = label_key("valxel:product"), label_key("valxel:feature"), label_key("valxel:resource")


def search_assets(connect: Any) -> Callable[[str, str], list[dict]]:
    def search(project: str, query: str) -> list[dict]:
        from google.cloud import asset_v1  # the 'gcp' extra
        from google.oauth2.credentials import Credentials

        creds = Credentials(token=connect.token(project, "agents"))
        client = asset_v1.AssetServiceClient(credentials=creds)
        pages = client.search_all_resources(request={"scope": f"projects/{project}", "query": query})
        return [{"name": r.name, "asset_type": r.asset_type, "labels": dict(r.labels)} for r in pages]

    return search


class GcpAssets:
    def __init__(self, search: Callable[[str, str], list[dict]]) -> None:
        self.search = search

    def _found(self, product_id: str, account: str) -> list[dict]:
        return self.search(account, f"labels.{PRODUCT}={label_value(product_id)}")

    def resources(self, *, account: str, product_id: str) -> list[dict]:
        return [{"id": r["name"], "type": r["asset_type"], "feature_id": id_from_label(r["labels"].get(FEATURE, "")),
                 "labels": dict(r["labels"])} for r in self._found(product_id, account)]

    def tagged(self, *, product_id: str, account: str, region: str = "") -> list[dict]:
        """The shape ops.inventory reads from AWS's tagging API: arn (here the asset's full name),
        resource and feature."""
        return [{"arn": r["name"], "resource": r["labels"].get(RESOURCE, ""),
                 "feature": id_from_label(r["labels"].get(FEATURE, ""))} for r in self._found(product_id, account)]


def from_env(env: Any = None) -> GcpAssets | None:
    from .accounts import GcpConnect

    connect = GcpConnect.from_env(env)
    return GcpAssets(search_assets(connect)) if connect else None

"""What carries a product's tags in an Azure subscription: Azure Resource Graph
(ResourceInventory; tracker CP-405). Run as the subscription's valxel-agents identity (Reader),
signed in for the job through AzureConnect."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import Any
from urllib.request import Request, urlopen

from .accounts import ARM

PRODUCT, FEATURE, RESOURCE = "valxel:product", "valxel:feature", "valxel:resource"
ID = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
API = "2022-10-01"


def resource_graph(connect: Any) -> Callable[[str, str], list[dict]]:
    def query(subscription: str, kql: str) -> list[dict]:
        token, out, skip = connect.token(subscription, "agents"), [], ""
        while True:
            body = {"subscriptions": [subscription], "query": kql,
                    "options": {"resultFormat": "objectArray", **({"$skipToken": skip} if skip else {})}}
            req = Request(f"{ARM}/providers/Microsoft.ResourceGraph/resources?api-version={API}",  # noqa: S310
                          data=json.dumps(body).encode(), method="POST",
                          headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
            with urlopen(req, timeout=30) as resp:  # noqa: S310 - fixed host
                got = json.loads(resp.read())
            out += list(got.get("data") or [])
            skip = got.get("$skipToken") or ""
            if not skip:
                return out

    return query


class AzureResources:
    def __init__(self, query: Callable[[str, str], list[dict]]) -> None:
        self.query = query

    def _found(self, product_id: str, account: str) -> list[dict]:
        if not ID.match(product_id or ""):
            raise ValueError(f"not a product id: {product_id!r}")
        # The id is checked above: letters, digits, `_` and `-` only, so it can't end the string.
        kql = (f"resources | where tags['{PRODUCT}'] =~ '{product_id}' "
               "| project id, type, tags")
        return [{"id": r.get("id", ""), "type": r.get("type", ""), "tags": dict(r.get("tags") or {})}
                for r in self.query(account, kql)]

    def resources(self, *, account: str, product_id: str) -> list[dict]:
        return [{"id": r["id"], "type": r["type"], "feature_id": r["tags"].get(FEATURE, ""), "labels": r["tags"]}
                for r in self._found(product_id, account)]

    def tagged(self, *, product_id: str, account: str, region: str = "") -> list[dict]:
        """The shape ops.inventory reads from AWS's tagging API: arn (here the resource ID), resource
        and feature."""
        return [{"arn": r["id"], "resource": r["tags"].get(RESOURCE, ""), "feature": r["tags"].get(FEATURE, "")}
                for r in self._found(product_id, account)]


def from_env(env: Any = None, connect: Any = None) -> AzureResources | None:
    from .accounts import AzureConnect

    connect = connect or AzureConnect.from_env(env)
    return AzureResources(resource_graph(connect)) if connect else None

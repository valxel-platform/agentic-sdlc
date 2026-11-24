"""What a product's features cost on Azure, from Cost Management (CostSource; tracker CP-405).

Read as the subscription's valxel-agents identity (Cost Management Reader), signed in for the job
through AzureConnect. Resources carry the tags valxel:product and valxel:feature, as on AWS; the
query groups the subscription's actual cost by service and feature tag, filtered to the product.
Costs are actual cost in the billing currency, which a USD billing account reports in dollars.
Cost Management settles a day's figures within about 72 hours.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime
from typing import Any
from urllib.request import Request, urlopen

from .accounts import ARM

PRODUCT, FEATURE = "valxel:product", "valxel:feature"
API = "2023-11-01"


def cost_query(connect: Any) -> Callable[[str, dict], list[dict]]:
    def run(subscription: str, body: dict) -> list[dict]:
        token = connect.token(subscription, "agents")
        url, rows = f"{ARM}/subscriptions/{subscription}/providers/Microsoft.CostManagement/query?api-version={API}", []
        while url:
            req = Request(url, data=json.dumps(body).encode(), method="POST",  # noqa: S310 - ARM's https host
                          headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
            with urlopen(req, timeout=60) as resp:  # noqa: S310 - fixed host
                got = (json.loads(resp.read()) or {}).get("properties") or {}
            names = [c["name"] for c in got.get("columns") or []]
            rows += [dict(zip(names, r, strict=False)) for r in got.get("rows") or []]
            url = got.get("nextLink") or ""
        return rows

    return run


def _body(start: datetime, end: datetime, *, product_id: str, daily: bool, feature_id: str = "") -> dict:
    tags = [{"tags": {"name": PRODUCT, "operator": "In", "values": [product_id]}}]
    if feature_id:
        tags.append({"tags": {"name": FEATURE, "operator": "In", "values": [feature_id]}})
    return {"type": "ActualCost", "timeframe": "Custom",
            "timePeriod": {"from": start.isoformat(), "to": end.isoformat()},
            "dataset": {**({"granularity": "Daily"} if daily else {}),
                        "aggregation": {"totalCost": {"name": "Cost", "function": "Sum"}},
                        "grouping": [{"type": "Dimension", "name": "ServiceName"},
                                     {"type": "TagKey", "name": FEATURE}],
                        "filter": tags[0] if len(tags) == 1 else {"and": tags}}}


def _day(v: Any) -> str:
    s = str(int(v)) if isinstance(v, (int, float)) else str(v)
    return f"{s[:4]}-{s[4:6]}-{s[6:8]}" if len(s) == 8 and s.isdigit() else s[:10]


def _usd(r: dict) -> float:
    return float(r.get("Cost", r.get("PreTaxCost", r.get("totalCost", 0))) or 0)


class AzureCosts:
    def __init__(self, run: Callable[[str, dict], list[dict]]) -> None:
        self.run = run

    def component_costs(self, **_: Any) -> dict[str, float]:
        return {}

    def component_costs_daily(self, **_: Any) -> list[dict]:
        return []

    def cloud_costs(self, *, product_id, feature_id, account, start, end) -> dict[str, float]:
        out: dict[str, float] = {}
        for r in self.run(account, _body(start, end, product_id=product_id, daily=False, feature_id=feature_id)):
            out[str(r.get("ServiceName") or "")] = round(out.get(str(r.get("ServiceName") or ""), 0.0) + _usd(r), 4)
        return out

    def cloud_costs_daily(self, *, product_id, account, start, end) -> list[dict]:
        rows = self.run(account, _body(start, end, product_id=product_id, daily=True))
        return [{"day": _day(r.get("UsageDate")), "service": str(r.get("ServiceName") or ""),
                 "feature_id": str(r.get("TagValue") or "") if str(r.get("TagKey") or "") == FEATURE else "",
                 "usd": _usd(r)} for r in rows]


def from_env(env: Any = None, connect: Any = None) -> AzureCosts | None:
    from .accounts import AzureConnect

    connect = connect or AzureConnect.from_env(env)
    return AzureCosts(cost_query(connect)) if connect else None

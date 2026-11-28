"""Who may open a product's dashboards and agent traces, and where (the Support page).

Support reaches a product's Grafana (metrics and HTTP requests) and MLflow (its agents'
traces) only through the tool: the product's Support page links to them, and the tool is their only
sign-in (web/oidc_provider.py), so it decides, at each sign-in:

- Grafana: the product's own members (anyone who may see the product) and the workspace's Support
  group may open it, as Viewer; the workspace's admins and owners as Editor. It shows only that
  product's metrics, even on a plane products share (platform-foundation, docs/grafana.md).
- MLflow: only the groups a workspace admin has granted it, per product (workspaces.support): agent
  traces can hold prompts and tool data, so nobody has it until then.

Each sign-in is for one product: Grafana puts a person in that product's organization only.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

PLANES = ("prod", "nonprod")
EDITOR_ROLES = ("admin", "owner")


@dataclass(frozen=True)
class Access:
    grafana: str = ""     # "Viewer", "Editor" or "" (none)
    mlflow: bool = False

    def allows(self, to: str) -> bool:
        return bool(self.grafana) if to == "grafana" else self.mlflow if to == "mlflow" else False


def access(user, product_id: str, workspace: str, visible: list[str], mlflow_groups: list[str]) -> Access:
    """What `user` may open for the product. `visible` is the products they may see (lanes_for),
    `mlflow_groups` the product's granted groups (slugs in its workspace)."""
    groups = {g.lower() for g in getattr(user, "groups", []) or []}
    in_support = bool(workspace) and f"{workspace}/support".lower() in groups
    member = product_id in visible
    if not (member or in_support):
        return Access()
    role = (getattr(user, "roles", {}) or {}).get(workspace, "")
    grafana = "Editor" if role in EDITOR_ROLES else "Viewer"
    mlflow = bool(workspace) and any(f"{workspace}/{g}".lower() in groups for g in mlflow_groups)
    return Access(grafana=grafana, mlflow=mlflow)


def grafana_url() -> str:
    return os.environ.get("VALXEL_GRAFANA_URL", "").rstrip("/")


def mlflow_url(product_id: str, plane: str) -> str:
    """The product's MLflow on a plane, from VALXEL_MLFLOW_URL
    ("https://mlflow-{product}.{plane}.example.com"; {product} is the id in lower case)."""
    template = os.environ.get("VALXEL_MLFLOW_URL", "")
    return template.format(product=product_id.lower(), plane=plane).rstrip("/") if template else ""


def links(product_id: str, granted: Access) -> list[dict]:
    """The Support page's links for the product, only those the person may open. Each goes through
    the tool (/api/support/open), which signs them in to that product alone."""
    out = []
    base = f"/api/support/open?product={product_id}"
    if granted.grafana and grafana_url():
        out.append({"to": "grafana", "label": "Dashboards", "role": granted.grafana,
                    "detail": "Grafana: requests, errors and latency per route",
                    "href": f"{base}&to=grafana"})
    if granted.mlflow:
        for plane in PLANES:
            if mlflow_url(product_id, plane):
                out.append({"to": "mlflow", "plane": plane, "label": f"Agent traces ({plane})",
                            "detail": "MLflow: what the product's agents did, step by step",
                            "href": f"{base}&to=mlflow&plane={plane}"})
    return out

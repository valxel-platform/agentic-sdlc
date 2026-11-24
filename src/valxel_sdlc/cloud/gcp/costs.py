"""What a product's features cost on Google Cloud, from the Cloud Billing export (CostSource;
tracker CP-204).

The export writes the billing account's usage, with each resource's labels, into a BigQuery
dataset (VALXEL_GCP_BILLING_DATASET, `project.dataset`; the connect template makes it and gives the
tool read access). A billing administrator turns the export on in the console: Google has no API
for it. Until the first rows land the source says so, and reports nothing.

A feature's resources carry the label valxel_feature (cloud.label_key), products valxel_product.
Costs are net of credits (sustained use, committed use, promotions), like Cost Explorer's
unblended cost less credits.

The queries name tables and label keys from settings and INFORMATION_SCHEMA, never from a request;
every value (project, dates, ids) is a bound query parameter.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any

from .. import id_from_label, label_key, label_value

PRODUCT, FEATURE = label_key("valxel:product"), label_key("valxel:feature")
NET = "SUM(cost) + SUM(IFNULL((SELECT SUM(c.amount) FROM UNNEST(credits) c), 0))"


class BillingExportMissing(RuntimeError):
    pass


def run_query(dataset_project: str) -> Callable[[str, dict], list[dict]]:
    def run(sql: str, params: dict) -> list[dict]:
        from google.cloud import bigquery  # the 'gcp' extra

        client = bigquery.Client(project=dataset_project)
        cfg = bigquery.QueryJobConfig(query_parameters=[
            bigquery.ScalarQueryParameter(k, "TIMESTAMP" if isinstance(v, datetime) else "STRING", v)
            for k, v in params.items()])
        return [dict(r.items()) for r in client.query(sql, job_config=cfg).result()]

    return run


class GcpBillingCosts:
    def __init__(self, dataset: str, *, run: Callable[[str, dict], list[dict]] | None = None) -> None:
        if dataset.count(".") != 1:
            raise ValueError("VALXEL_GCP_BILLING_DATASET is project.dataset")
        self.dataset = dataset
        self.run = run or run_query(dataset.split(".", 1)[0])
        self._table = ""

    def table(self) -> str:
        """The standard usage cost table, gcp_billing_export_v1_<billing account>."""
        if not self._table:
            rows = self.run(f"SELECT table_name FROM `{self.dataset}.INFORMATION_SCHEMA.TABLES` "  # noqa: S608 - see below
                            "WHERE table_name LIKE 'gcp_billing_export_v1_%' ORDER BY table_name", {})
            if not rows:
                raise BillingExportMissing(
                    f"The billing export isn't writing to {self.dataset} yet: a billing account administrator turns "
                    "it on in the console (Billing, Billing export, BigQuery export, Standard usage cost, this "
                    "dataset). The first rows take up to a day")
            self._table = f"{self.dataset}.{rows[0]['table_name']}"
        return self._table

    @staticmethod
    def _label(key: str) -> str:
        return f"(SELECT l.value FROM UNNEST(labels) l WHERE l.key = '{key}')"  # noqa: S608 - see below

    def component_costs(self, **_: Any) -> dict[str, float]:
        return {}

    def component_costs_daily(self, **_: Any) -> list[dict]:
        return []

    def cloud_costs(self, *, product_id, feature_id, account, start, end) -> dict[str, float]:
        sql = (f"SELECT service.description AS service, {NET} AS usd FROM `{self.table()}` "  # noqa: S608 - see below
               f"WHERE project.id = @project AND usage_start_time >= @start AND usage_start_time < @end "
               f"AND {self._label(FEATURE)} = @feature GROUP BY service")
        rows = self.run(sql, {"project": account, "start": start, "end": end, "feature": label_value(feature_id)})
        return {r["service"]: round(float(r["usd"]), 4) for r in rows}

    def cloud_costs_daily(self, *, product_id, account, start, end) -> list[dict]:
        sql = (f"SELECT FORMAT_DATE('%Y-%m-%d', DATE(usage_start_time)) AS day, service.description AS service, "  # noqa: S608 - see below
               f"IFNULL({self._label(FEATURE)}, '') AS feature, {NET} AS usd FROM `{self.table()}` "
               f"WHERE project.id = @project AND usage_start_time >= @start AND usage_start_time < @end "
               f"AND {self._label(PRODUCT)} = @product GROUP BY day, service, feature ORDER BY day")
        rows = self.run(sql, {"project": account, "start": start, "end": end, "product": label_value(product_id)})
        return [{"day": r["day"], "service": r["service"], "feature_id": id_from_label(r["feature"]),
                 "usd": float(r["usd"])} for r in rows]

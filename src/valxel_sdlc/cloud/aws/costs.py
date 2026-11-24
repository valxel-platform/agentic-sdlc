"""AWS Cost Explorer: what a product's features cost, by the valxel:product and valxel:feature tags."""

from __future__ import annotations


class CostExplorerSource:
    """AWS Cost Explorer: the feature's resources (tag valxel:feature, activated as a cost
    allocation tag in the payer account), in one environment's account, by service. Runs with the
    tool's own AWS credentials, which need ce:GetCostAndUsage and nothing else."""

    def __init__(self, client=None) -> None:
        if client is None:
            import boto3  # the 'aws' extra

            client = boto3.client("ce", region_name="us-east-1")
        self.ce = client

    def component_costs(self, **_) -> dict[str, float]:
        return {}

    def component_costs_daily(self, **_) -> list[dict]:
        return []

    def cloud_costs_daily(self, *, product_id, account, start, end) -> list[dict]:
        """Everything tagged valxel:product in the account, by day, feature tag and service."""
        flt = {"And": [{"Tags": {"Key": "valxel:product", "Values": [product_id], "MatchOptions": ["EQUALS"]}},
                       {"Dimensions": {"Key": "LINKED_ACCOUNT", "Values": [account]}}]}
        out, token = [], None
        while True:
            kw = {"TimePeriod": {"Start": start.date().isoformat(), "End": end.date().isoformat()},
                  "Granularity": "DAILY", "Metrics": ["UnblendedCost"], "Filter": flt,
                  "GroupBy": [{"Type": "TAG", "Key": "valxel:feature"}, {"Type": "DIMENSION", "Key": "SERVICE"}]}
            if token:
                kw["NextPageToken"] = token
            res = self.ce.get_cost_and_usage(**kw)
            for day in res.get("ResultsByTime") or []:
                for g in day.get("Groups") or []:
                    tag, service = g["Keys"]
                    out.append({"day": day["TimePeriod"]["Start"], "service": service,
                                "feature_id": tag.split("$", 1)[1] if "$" in tag else "",
                                "usd": float(g["Metrics"]["UnblendedCost"]["Amount"])})
            token = res.get("NextPageToken")
            if not token:
                return out

    def cloud_costs(self, *, product_id, feature_id, account, start, end) -> dict[str, float]:
        flt = {"And": [{"Tags": {"Key": "valxel:feature", "Values": [feature_id], "MatchOptions": ["EQUALS"]}},
                       {"Dimensions": {"Key": "LINKED_ACCOUNT", "Values": [account]}}]}
        out: dict[str, float] = {}
        token = None
        while True:
            kw = {"TimePeriod": {"Start": start.date().isoformat(), "End": end.date().isoformat()},
                  "Granularity": "DAILY", "Metrics": ["UnblendedCost"], "Filter": flt,
                  "GroupBy": [{"Type": "DIMENSION", "Key": "SERVICE"}]}
            if token:
                kw["NextPageToken"] = token
            res = self.ce.get_cost_and_usage(**kw)
            for day in res.get("ResultsByTime") or []:
                for g in day.get("Groups") or []:
                    out[g["Keys"][0]] = out.get(g["Keys"][0], 0.0) + float(g["Metrics"]["UnblendedCost"]["Amount"])
            token = res.get("NextPageToken")
            if not token:
                return {k: round(v, 4) for k, v in out.items()}

"""The AWS tagging API: what is tagged valxel:product in a product's account."""

from __future__ import annotations

import os


class AwsTags:
    """What is tagged valxel:product in an account: [{"arn", "resource" (valxel:resource tag), "feature"}].
    `client_for(account, region)` returns a resourcegroupstaggingapi client; by default the tool
    assumes VALXEL_AWS_INVENTORY_ROLE in the account (tag:GetResources only)."""

    def __init__(self, client_for=None, role: str | None = None) -> None:
        self.role = role if role is not None else os.environ.get("VALXEL_AWS_INVENTORY_ROLE", "")
        self.client_for = client_for or self._assume

    def _assume(self, account: str, region: str):
        import boto3  # the 'aws' extra

        creds = boto3.client("sts").assume_role(RoleArn=f"arn:aws:iam::{account}:role/{self.role}",
                                                RoleSessionName="valxel-inventory")["Credentials"]
        return boto3.client("resourcegroupstaggingapi", region_name=region,
                            aws_access_key_id=creds["AccessKeyId"], aws_secret_access_key=creds["SecretAccessKey"],
                            aws_session_token=creds["SessionToken"])

    def tagged(self, *, product_id: str, account: str, region: str) -> list[dict]:
        client = self.client_for(account, region)
        out, token = [], ""
        while True:
            kw = {"TagFilters": [{"Key": "valxel:product", "Values": [product_id]}], "ResourcesPerPage": 100}
            if token:
                kw["PaginationToken"] = token
            res = client.get_resources(**kw)
            for m in res.get("ResourceTagMappingList") or []:
                tags = {t["Key"]: t["Value"] for t in m.get("Tags") or []}
                out.append({"arn": m["ResourceARN"], "resource": tags.get("valxel:resource", ""),
                            "feature": tags.get("valxel:feature", "")})
            token = res.get("PaginationToken") or ""
            if not token:
                return out

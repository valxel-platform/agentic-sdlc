"""The resource inventory: what a product runs in each environment, read live from where it runs.

Nothing here is kept by hand. Per product and environment:

    components   from OpenChoreo's release bindings: the release running and its state
    resources    from OpenChoreo's resource release bindings: AWS services (and other Resources,
                 such as a self-hosted GlitchTip's database) with their size and non-secret outputs
                 (names, ARNs, endpoints); a secret output shows only which Secret holds it
    store apps   the version the tool last released to the stores

each with the identifiers to find it directly: the OpenChoreo binding, a kubectl label selector for
the data plane (OpenChoreo labels every workload with openchoreo.dev/project, /component and
/environment), and for AWS the account, region and ARN. The feature that created it, its last 30
days of cost (components) or planned monthly cost (AWS, from the platform catalog), and production
against the lower environments.

With VALXEL_AWS_INVENTORY_ROLE set, the tool also lists what is tagged valxel:product=<product> in
each environment's AWS account (the Resource Groups Tagging API, through that read-only role):
that fills in ARNs, and anything tagged for the product that OpenChoreo doesn't know is reported
as unmanaged, for a person to adopt or remove.
"""

from __future__ import annotations

import os
from collections import Counter
from datetime import datetime, timedelta, timezone


def now() -> datetime:
    return datetime.now(timezone.utc)


def kubectl_for(project: str, component: str, env: str, namespace: str | None) -> str:
    sel = [f"openchoreo.dev/project={project}", f"openchoreo.dev/component={component}",
           f"openchoreo.dev/environment={env}"]
    if namespace:
        sel.insert(0, f"openchoreo.dev/namespace={namespace}")
    return f"kubectl get deploy,svc,pods -A -l {','.join(sel)}"


def resource_kubectl(name: str, namespace: str | None) -> str:
    return f"kubectl get resourcereleasebindings -n {namespace or 'default'} -o wide | grep -w {name}"


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


def aws_tags_from_env():
    return AwsTags() if os.environ.get("VALXEL_AWS_INVENTORY_ROLE") else None


def collect(oc, snap, known: dict, *, costs=None, aws=None, at: datetime | None = None) -> dict:
    """The product's inventory. `known`: what the tool's features deployed and created,
    {"components": {repo: {component, project, namespace, feature_id, kind}},
     "resources": {name: {type, feature_id, sizes}}, "store_apps": {repo: {env, version, feature_id}}}."""
    at = at or now()
    rt = snap.product.runtime or {}
    pipeline = list(rt.get("pipeline") or ["dev", "staging", "production"])
    project, ns = rt.get("openchoreo_project") or "", rt.get("namespace")
    aws_cfg = rt.get("aws") or {}
    accounts, region = dict(aws_cfg.get("accounts") or {}), aws_cfg.get("region") or ""
    catalog = snap.aws_catalog or {}
    rows, problems = [], []

    month_ago = (at - timedelta(days=30)).isoformat()
    spend: Counter = Counter()
    for r in costs or []:
        if r.period_start >= month_ago and r.category in ("compute", "environment") and r.resource:
            spend[(r.resource.rsplit("/", 1)[-1], r.env)] += r.usd

    for repo, c in sorted((known.get("components") or {}).items()):
        name = c.get("component") or repo
        short = name.rsplit("/", 1)[-1]
        try:
            bindings = oc.component_bindings(component=name, project=c.get("project") or project,
                                             namespace=c.get("namespace") or ns)
        except Exception as exc:
            problems.append(f"{short}: {type(exc).__name__}: {exc}")
            continue
        for b in bindings:
            rows.append({"kind": "component", "name": short, "repo": repo, "env": b["env"], "state": b["state"],
                         "release": b["release"], "feature_id": c.get("feature_id", ""),
                         "cost_30d_usd": round(spend.get((short, b["env"]), 0.0), 2),
                         "ids": {"openchoreo": f"{c.get('namespace') or ns or 'default'}/{c.get('project') or project}/{short}",
                                 "binding": b.get("binding", ""),
                                 "kubectl": kubectl_for(c.get("project") or project, short, b["env"], c.get("namespace") or ns)}})

    try:
        resources = oc.list_resources(project=project, namespace=ns) if project else []
    except Exception as exc:
        problems.append(f"resources: {type(exc).__name__}: {exc}")
        resources = []
    meta = known.get("resources") or {}
    for res in sorted(resources, key=lambda r: r["name"]):
        name, rtype = res["name"], res.get("type", "")
        try:
            bindings = oc.resource_bindings_of(name=name, project=project, namespace=ns)
        except Exception as exc:
            problems.append(f"{name}: {type(exc).__name__}: {exc}")
            continue
        info = meta.get(name) or {}
        sizes = ((catalog.get("types") or {}).get(rtype) or {}).get("sizes") or {}
        for b in bindings:
            size = b.get("size") or (info.get("sizes") or {}).get(b["env"], "")
            planned = (sizes.get(size) or {}).get("monthly_usd")
            outputs = b.get("outputs") or []
            arn = next((o["value"] for o in outputs if o.get("value", "").startswith("arn:")), "")
            rows.append({"kind": "resource", "name": name, "type": rtype, "env": b["env"], "state": b["state"],
                         "release": b.get("release", ""), "size": size, "feature_id": info.get("feature_id", ""),
                         "planned_monthly_usd": float(planned) if planned is not None else None,
                         "outputs": [o for o in outputs if "value" in o],
                         "secrets": [{"output": o["name"], **o["secret"]} for o in outputs if "secret" in o],
                         "ids": {"openchoreo": f"{ns or 'default'}/{project}/{name}", "binding": b.get("binding", ""),
                                 "kubectl": resource_kubectl(name, ns), "aws_account": accounts.get(b["env"], ""),
                                 "aws_region": region if accounts.get(b["env"]) else "", "arn": arn}})

    for repo, a in sorted((known.get("store_apps") or {}).items()):
        rows.append({"kind": "store_app", "name": repo.rsplit("/", 1)[-1], "repo": repo, "env": a.get("env", ""),
                     "state": "released", "release": a.get("version", ""), "feature_id": a.get("feature_id", ""),
                     "ids": {"stores": a.get("stores", "")}})

    unmanaged, aws_note = [], "not checked: set VALXEL_AWS_INVENTORY_ROLE to list tagged AWS resources"
    if aws is not None and accounts:
        aws_note = "checked"
        for env, account in sorted(accounts.items()):
            try:
                found = aws.tagged(product_id=snap.product.product_id, account=account, region=region)
            except Exception as exc:
                problems.append(f"AWS {env} ({account}): {type(exc).__name__}: {exc}")
                aws_note = "partly checked"
                continue
            mine = {r["name"]: r for r in rows if r["kind"] == "resource" and r["env"] == env}
            for f in found:
                row = mine.get(f["resource"]) if f["resource"] else None
                if row is not None:
                    row["ids"]["arn"] = row["ids"]["arn"] or f["arn"]
                elif not f["resource"] and ":iam::" in f["arn"]:
                    continue  # a component's workload identity: tagged with the product only
                else:
                    unmanaged.append({"env": env, "account": account, "arn": f["arn"], "resource_tag": f["resource"],
                                      "feature": f["feature"]})
    return {"product_id": snap.product.product_id, "at": at.isoformat(), "project": project, "namespace": ns or "default",
            "pipeline": pipeline, "items": rows, "summary": summarize(rows, pipeline), "unmanaged": unmanaged,
            "aws": aws_note, "problems": problems}


def summarize(rows: list[dict], pipeline: list[str]) -> dict:
    """Counts and money per environment, and production against everything before it."""
    prod = pipeline[-1] if pipeline else "production"
    per_env = {env: {"components": 0, "resources": 0, "store_apps": 0, "not_ready": 0, "cost_30d_usd": 0.0,
                     "planned_monthly_usd": 0.0} for env in pipeline}
    for r in rows:
        e = per_env.setdefault(r["env"], {"components": 0, "resources": 0, "store_apps": 0, "not_ready": 0,
                                          "cost_30d_usd": 0.0, "planned_monthly_usd": 0.0})
        e[{"component": "components", "resource": "resources", "store_app": "store_apps"}[r["kind"]]] += 1
        if r["state"] not in ("ready", "released"):
            e["not_ready"] += 1
        e["cost_30d_usd"] = round(e["cost_30d_usd"] + (r.get("cost_30d_usd") or 0.0), 2)
        e["planned_monthly_usd"] = round(e["planned_monthly_usd"] + (r.get("planned_monthly_usd") or 0.0), 2)
    lower = [e for env, e in per_env.items() if env != prod]
    sum_keys = ("components", "resources", "store_apps", "cost_30d_usd", "planned_monthly_usd")
    return {"per_env": per_env, "production": per_env.get(prod, {}),
            "lower": {k: round(sum(e[k] for e in lower), 2) for k in sum_keys}}


def snapshot_row(inv: dict) -> dict:
    """What a daily snapshot keeps: counts per environment, for the trend."""
    return {"product_id": inv["product_id"], "day": inv["at"][:10],
            "per_env": {env: {k: e[k] for k in ("components", "resources", "store_apps", "not_ready")}
                        for env, e in inv["summary"]["per_env"].items()},
            "unmanaged": len(inv["unmanaged"])}


class InMemorySnapshots:
    def __init__(self) -> None:
        self.rows: dict[tuple[str, str], dict] = {}

    def put(self, row: dict) -> None:
        self.rows[(row["product_id"], row["day"])] = row

    def history(self, product_id: str, days: int = 90) -> list[dict]:
        return [r for (p, _), r in sorted(self.rows.items()) if p == product_id][-days:]


class PostgresSnapshots:
    DDL = ("""CREATE TABLE IF NOT EXISTS valxel_inventory_snapshots (
                product_id TEXT NOT NULL, day DATE NOT NULL, data JSONB NOT NULL, PRIMARY KEY (product_id, day))""",)

    def __init__(self, pool) -> None:
        self.pool = pool
        with self.pool.connection() as conn:
            for s in self.DDL:
                conn.execute(s)

    def put(self, row: dict) -> None:
        import json

        with self.pool.connection() as conn:
            conn.execute("INSERT INTO valxel_inventory_snapshots (product_id, day, data) VALUES (%s, %s, %s::jsonb) "
                         "ON CONFLICT (product_id, day) DO UPDATE SET data = EXCLUDED.data",
                         (row["product_id"], row["day"], json.dumps(row)))

    def history(self, product_id: str, days: int = 90) -> list[dict]:
        with self.pool.connection() as conn:
            rows = conn.execute("SELECT data FROM valxel_inventory_snapshots WHERE product_id = %s "
                                "ORDER BY day DESC LIMIT %s", (product_id, days)).fetchall()
        return [dict(r["data"]) for r in reversed(rows)]

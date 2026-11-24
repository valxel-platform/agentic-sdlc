"""Cost after release: what a released feature really costs, against what G4 approved.

G4 approves a feature's AWS services with planning costs per environment (VX-19). When G4
approves, the feature records that as its cost plan. After release the tool reviews the feature
on a schedule (by default 7 and 30 days after release):

    AWS          what the feature's own resources cost (Cost Explorer, tag valxel:feature, in each
                 environment's account), projected to a month, against the approved monthly figure
    compute      what the feature's components cost in production (OpenChoreo FinOps, cpu + memory),
                 the week after release against the week before it

Spend above the plan, beyond a tolerance, becomes a cost signal: the incident flow triages it and
on-call decides what to do (right-size, change the intent, turn the flag off, or accept it).
Every review is kept, so the product's cost view can show each feature's run-rate.

Cost signals don't resolve on their own: each is a finding about a period, not an alert that
clears. The next review raises it again if the spend is still above the plan.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta, timezone

from ..cloud.ports import CostSource  # noqa: F401 - the port (ADR 0010), re-exported
from .cost_plan import plan_from_g4  # noqa: F401  (the core's; re-exported)

CATEGORY = "cost"
DAYS_PER_MONTH = 30.0


def now() -> datetime:
    return datetime.now(timezone.utc)


def released_at(state: dict) -> datetime | None:
    """When production went live: the feature's finish, else its G6 approval."""
    for e in reversed(state.get("events") or []):
        if e.get("node") == "finalize" and e.get("kind") == "done":
            return datetime.fromisoformat(e["at"])
    at = next((g["at"] for g in reversed(state.get("gate_decisions") or []) if g.get("gate") == "g6"), "")
    return datetime.fromisoformat(at) if at else None


# --------------------------------------------------------------------------- where costs come from
def _days(start: datetime, end: datetime):
    d = start.date()
    while datetime(d.year, d.month, d.day, tzinfo=timezone.utc) < end:
        day0 = datetime(d.year, d.month, d.day, tzinfo=timezone.utc)
        yield d, max(day0, start), min(day0 + timedelta(days=1), end)
        d += timedelta(days=1)


class StubCostSource:
    """Daily rates set by tests or a demo: (env, component) -> USD/day, and (feature, account) ->
    USD/day. A component's rate can change at a moment, to model a release that made it dearer."""

    def __init__(self) -> None:
        self.component_daily: dict[tuple[str, str], list[tuple[datetime | None, float]]] = {}
        self.aws_daily: dict[tuple[str, str], float] = {}
        self.component_project: dict[str, str] = {}  # component -> its OpenChoreo project, when set
        self.aws_product_daily: dict[tuple[str, str], float] = {}  # (product, account): untagged spend

    def set_component(self, env: str, component: str, usd_per_day: float, since: datetime | None = None,
                      project: str = "") -> None:
        self.component_daily.setdefault((env, component), []).append((since, usd_per_day))
        if project:
            self.component_project[component] = project

    def set_aws(self, feature_id: str, account: str, usd_per_day: float) -> None:
        self.aws_daily[(feature_id, account)] = usd_per_day

    def component_costs(self, *, namespace, project, env, components, start, end) -> dict[str, float]:
        out = {}
        for c in components:
            rates = sorted(self.component_daily.get((env, c)) or [], key=lambda r: r[0] or datetime.min.replace(tzinfo=timezone.utc))
            total, t = 0.0, start
            while t < end:
                step = min(end, t + timedelta(hours=1))
                rate = next((r for since, r in reversed(rates) if since is None or since <= t), 0.0)
                total += rate * (step - t).total_seconds() / 86400
                t = step
            out[c] = round(total, 4)
        return out

    def cloud_costs(self, *, product_id, feature_id, account, start, end) -> dict[str, float]:
        rate = self.aws_daily.get((feature_id, account), 0.0)
        return {"Amazon DynamoDB": round(rate * (end - start).total_seconds() / 86400, 4)} if rate else {}

    def component_costs_daily(self, *, namespace, project, env, start, end) -> list[dict]:
        comps = sorted({c for e, c in self.component_daily
                        if e == env and self.component_project.get(c, project) == project})
        out = []
        for d, a, b in _days(start, end):
            for c, usd in self.component_costs(namespace=namespace, project=project, env=env, components=comps,
                                               start=a, end=b).items():
                if usd:
                    out.append({"day": d.isoformat(), "component": c, "usd": usd})
        return out

    def cloud_costs_daily(self, *, product_id, account, start, end) -> list[dict]:
        out = []
        for d, a, b in _days(start, end):
            frac = (b - a).total_seconds() / 86400
            for (fid, acct), rate in sorted(self.aws_daily.items()):
                if acct == account and fid.startswith(product_id + "-"):
                    out.append({"day": d.isoformat(), "service": "Amazon DynamoDB", "feature_id": fid,
                                "usd": round(rate * frac, 4)})
            rate = self.aws_product_daily.get((product_id, account), 0.0)
            if rate:
                out.append({"day": d.isoformat(), "service": "AWS Key Management Service", "feature_id": "",
                            "usd": round(rate * frac, 4)})
        return out


class ObserverCostSource:
    """OpenChoreo's FinOps module, through the Observer's cost API (needs the finops-opencost
    module): per-component cost records, cpu + memory, for a namespace and environment."""

    def __init__(self, url: str, auth=None, opener=None) -> None:
        from urllib.request import build_opener

        self.url, self.auth, self.opener = url.rstrip("/"), auth, opener or build_opener()

    def component_costs(self, *, namespace, project, env, components, start, end) -> dict[str, float]:
        from urllib.parse import quote, urlencode
        from urllib.request import Request

        q = urlencode({"project": project, "startTime": _iso(start), "endTime": _iso(end)})
        req = Request(f"{self.url}/api/v1alpha1/costs/namespaces/{quote(namespace)}/environments/{quote(env)}?{q}",
                      headers={"Accept": "application/json", **(self.auth() if self.auth else {})})
        with self.opener.open(req, timeout=30) as resp:
            items = json.loads(resp.read().decode("utf-8")).get("items") or []
        out = {c: 0.0 for c in components}
        for it in items:
            if it.get("component") in out:
                out[it["component"]] += float(it.get("cpuCost") or 0) + float(it.get("memoryCost") or 0)
        return {k: round(v, 4) for k, v in out.items()}

    def cloud_costs(self, **_) -> dict[str, float]:
        return {}

    def component_costs_daily(self, *, namespace, project, env, start, end) -> list[dict]:
        from urllib.parse import quote, urlencode
        from urllib.request import Request

        q = urlencode({"project": project, "startTime": _iso(start), "endTime": _iso(end), "granularity": "1d"})
        req = Request(f"{self.url}/api/v1alpha1/costs/namespaces/{quote(namespace)}/environments/{quote(env)}?{q}",
                      headers={"Accept": "application/json", **(self.auth() if self.auth else {})})
        with self.opener.open(req, timeout=60) as resp:
            items = json.loads(resp.read().decode("utf-8")).get("items") or []
        return [{"day": str(it.get("startTime", ""))[:10], "component": it.get("component", ""),
                 "usd": round(float(it.get("cpuCost") or 0) + float(it.get("memoryCost") or 0), 6)}
                for it in items if it.get("component")]

    def cloud_costs_daily(self, **_) -> list[dict]:
        return []


class CostSources:
    """Compute from one source, the cloud's own resources from another."""

    def __init__(self, compute, cloud) -> None:
        self.compute, self.cloud = compute, cloud

    def component_costs(self, **kw) -> dict[str, float]:
        return self.compute.component_costs(**kw)

    def cloud_costs(self, **kw) -> dict[str, float]:
        return self.cloud.cloud_costs(**kw)

    def component_costs_daily(self, **kw) -> list[dict]:
        return self.compute.component_costs_daily(**kw)

    def cloud_costs_daily(self, **kw) -> list[dict]:
        return self.cloud.cloud_costs_daily(**kw)


def _iso(t: datetime) -> str:
    return t.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def default_sources(settings=None):
    """Live sources when configured (VALXEL_OPENCHOREO_OBSERVER_URL; VALXEL_AWS_COST_EXPLORER=1, or
    on Google Cloud VALXEL_GCP_BILLING_DATASET); otherwise the stub, which reports no spend."""
    import os

    compute = aws = None
    url = os.environ.get("VALXEL_OPENCHOREO_OBSERVER_URL", "")
    if url:
        auth = None
        oc = settings.tools.servers.get("openchoreo") if settings is not None else None
        if oc and oc.setting("token_url") and oc.setting("client_secret"):
            from ..integrations.mcp_client import ClientCredentials

            auth = ClientCredentials(oc.setting("token_url"), oc.setting("client_id") or "service_mcp_client",
                                     oc.setting("client_secret"), scope=oc.setting("scope"))
        compute = ObserverCostSource(url, auth)
    from ..cloud import provider

    if provider() == "gcp" and os.environ.get("VALXEL_GCP_BILLING_DATASET"):
        from ..cloud.gcp.costs import GcpBillingCosts

        aws = GcpBillingCosts(os.environ["VALXEL_GCP_BILLING_DATASET"])
    elif os.environ.get("VALXEL_AWS_COST_EXPLORER", "").lower() in ("1", "true", "yes"):
        from ..cloud.aws.costs import CostExplorerSource

        aws = CostExplorerSource()
    if compute is None and aws is None:
        return StubCostSource()
    stub = StubCostSource()
    return CostSources(compute or stub, aws or stub)


# --------------------------------------------------------------------------- the review
def review(plan: dict, *, feature_id: str, product_id: str, released: datetime, at: datetime,
           aws_actual: dict[str, dict[str, float]], window_days: float,
           compute_before: dict[str, float], compute_after: dict[str, float],
           tolerance: float = 0.2, floor_usd: float = 5.0) -> dict:
    """Compare actuals with the plan. aws_actual: env -> service -> USD over the window;
    compute_*: production component -> USD over the window before and after release."""
    findings = []
    aws = {}
    for env, approved in sorted((plan.get("aws_monthly_usd") or {}).items()):
        spent = round(sum((aws_actual.get(env) or {}).values()), 4)
        projected = round(spent / window_days * DAYS_PER_MONTH, 2) if window_days else 0.0
        aws[env] = {"approved_monthly_usd": approved, "spent_usd": spent, "projected_monthly_usd": projected,
                    "by_service": dict(aws_actual.get(env) or {})}
        over = projected - approved
        if over > floor_usd and projected > approved * (1 + tolerance):
            ratio = projected / approved if approved else None
            findings.append({
                "kind": "aws", "env": env, "approved_monthly_usd": approved, "projected_monthly_usd": projected,
                "severity": "sev2" if env == "production" and (ratio is None or ratio >= 2) else "sev3",
                "text": (f"AWS services in {env} run at ${projected:,.2f}/month against ${approved:,.2f} approved at G4"
                         + (f" ({ratio:.1f}x)" if ratio else " (none approved)"))})
    compute = {}
    for comp in sorted(set(compute_before) | set(compute_after)):
        before, after = compute_before.get(comp, 0.0), compute_after.get(comp, 0.0)
        compute[comp] = {"before_usd": before, "after_usd": after}
        monthly_delta = (after - before) / window_days * DAYS_PER_MONTH if window_days else 0.0
        if before > 0 and after > before * (1 + tolerance) and monthly_delta > floor_usd:
            findings.append({
                "kind": "compute", "env": "production", "component": comp, "before_usd": before, "after_usd": after,
                "severity": "sev3",
                "text": (f"{comp} costs {after / before:.1f}x what it did the week before release "
                         f"(+${monthly_delta:,.2f}/month)")})
    return {"feature_id": feature_id, "product_id": product_id, "at": at.isoformat(),
            "released_at": released.isoformat(), "day": round((at - released).total_seconds() / 86400, 1),
            "window_days": window_days, "tolerance": tolerance, "aws": aws, "compute": compute,
            "findings": findings}


def signals(rv: dict, *, title: str = "", repo_of=lambda c: c) -> list[dict]:
    """One cost signal per finding, in the shared signal shape."""
    out = []
    for f in rv["findings"]:
        scope = f.get("component") or f"aws-{f['env']}"
        out.append({
            "source": "cost-review", "category": CATEGORY, "resolves": False, "status": "firing",
            "product_id": rv["product_id"], "component": repo_of(f["component"]) if f.get("component") else "",
            "env": f["env"], "severity": f["severity"],
            "title": f"{rv['feature_id']} costs more than approved" + (f": {title}" if title else ""),
            "detail": f["text"], "fingerprint": f"cost:{rv['feature_id']}:{scope}", "url": "", "at": rv["at"],
            "labels": {"feature_id": rv["feature_id"], "kind": f["kind"], "review_day": str(rv["day"])}})
    return out


# --------------------------------------------------------------------------- the reviews kept
class InMemoryCostReviews:
    def __init__(self) -> None:
        self.rows: list[dict] = []
        self._lock = threading.Lock()

    def add(self, rv: dict) -> None:
        with self._lock:
            self.rows.append(rv)

    def for_feature(self, feature_id: str) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self.rows if r["feature_id"] == feature_id]

    def for_product(self, product_id: str) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self.rows if r["product_id"] == product_id]


class PostgresCostReviews:
    DDL = ("""CREATE TABLE IF NOT EXISTS valxel_cost_reviews (
                id BIGSERIAL PRIMARY KEY, feature_id TEXT NOT NULL, product_id TEXT NOT NULL,
                at TIMESTAMPTZ NOT NULL, data JSONB NOT NULL)""",
           "CREATE INDEX IF NOT EXISTS valxel_cost_reviews_feature ON valxel_cost_reviews (feature_id)",
           "CREATE INDEX IF NOT EXISTS valxel_cost_reviews_product ON valxel_cost_reviews (product_id, at)")

    def __init__(self, pool) -> None:
        self.pool = pool
        with self.pool.connection() as conn:
            for statement in self.DDL:
                conn.execute(statement)

    def add(self, rv: dict) -> None:
        with self.pool.connection() as conn:
            conn.execute("INSERT INTO valxel_cost_reviews (feature_id, product_id, at, data) VALUES (%s, %s, %s, %s::jsonb)",
                         (rv["feature_id"], rv["product_id"], rv["at"], json.dumps(rv, default=str)))

    def _query(self, column: str, value: str) -> list[dict]:
        with self.pool.connection() as conn:
            rows = conn.execute(f"SELECT data FROM valxel_cost_reviews WHERE {column} = %s ORDER BY at, id",
                                (value,)).fetchall()
        return [dict(r["data"]) for r in rows]

    def for_feature(self, feature_id: str) -> list[dict]:
        return self._query("feature_id", feature_id)

    def for_product(self, product_id: str) -> list[dict]:
        return self._query("product_id", product_id)


def __getattr__(name: str):
    """Names that moved to valxel_sdlc.cloud.aws.costs (ADR 0010), still importable from here."""
    if name in ("CostExplorerSource",):
        from ..cloud.aws import costs

        return getattr(costs, name)
    raise AttributeError(name)

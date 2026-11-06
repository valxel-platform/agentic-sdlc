"""What people see: a feature's cost card, a product's monthly rollup, and the FOCUS export."""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

from .ledger import CATEGORIES, CostRecord

# The order categories are shown in, and what each is called.
LABELS = {"llm": "Agents (model calls)", "ci": "CI", "test": "Acceptance tests", "build": "Builds",
          "environment": "Dev and staging environments", "aws": "AWS services", "compute": "Production compute",
          "incident": "Incident triage", "people": "People at gates", "platform": "Shared platform"}


def _by_category(records: list[CostRecord]) -> list[dict]:
    acc: dict[str, dict] = {}
    for r in records:
        c = acc.setdefault(r.category, {"category": r.category, "label": LABELS[r.category], "usd": 0.0,
                                        "basis": set(), "provisional": False})
        c["usd"] += r.usd
        c["basis"].add(r.basis)
        c["provisional"] |= r.status == "provisional"
    return [{**c, "usd": round(c["usd"], 4), "basis": sorted(c["basis"])}
            for c in sorted(acc.values(), key=lambda c: CATEGORIES.index(c["category"]))]


def _total(rows: list[dict]) -> float:
    return round(sum(r["usd"] for r in rows), 4)


def build_so_far(ledger, state: dict) -> dict:
    """For gate packets: what the feature has cost to build, by category. Agent spend comes
    straight from the feature (the ledger catches up hourly)."""
    recs = [r for r in ledger.query(feature_id=state["feature_id"], stage="build") if r.category != "llm"] if ledger else []
    rows = _by_category(recs)
    llm = round(float(state.get("cost_usd", 0.0)), 4)
    rows.insert(0, {"category": "llm", "label": LABELS["llm"], "usd": llm, "basis": ["metered"], "provisional": False})
    return {"total_usd": _total(rows), "by_category": rows}


def feature_card(ledger, state: dict, *, at: datetime | None = None, reviews: list[dict] | None = None) -> dict:
    """Build: everything spent until production, by category. Run: what the feature's own
    resources cost over the last 30 days. Plan: what G4 approved. Reviews: post-release checks."""
    at = at or datetime.now(timezone.utc)
    fid = state["feature_id"]
    recs = ledger.query(feature_id=fid)
    build = _by_category([r for r in recs if r.stage == "build"])
    since = (at - timedelta(days=30)).isoformat()
    run = _by_category([r for r in recs if r.stage == "run" and r.period_end > since])
    reviews = [r for r in (reviews or []) if not r.get("skipped")]
    return {"feature_id": fid, "product_id": state.get("product_id"), "title": state.get("title", ""),
            "status": state.get("status", ""),
            "build": {"total_usd": _total(build), "by_category": build,
                      "provisional": any(c["provisional"] for c in build),
                      "llm_budget_usd": state.get("token_budget_usd")},
            "run_30d": {"total_usd": _total(run), "by_category": run},
            "plan": state.get("cost_plan") or None,
            "reviews": reviews,
            "latest_review": reviews[-1] if reviews else None}


def month_bounds(month: str | None, at: datetime) -> tuple[date, str, str]:
    first = date.fromisoformat(f"{month}-01") if month else at.date().replace(day=1)
    nxt = (first + timedelta(days=32)).replace(day=1)
    return first, datetime.combine(first, datetime.min.time(), timezone.utc).isoformat(), \
        datetime.combine(nxt, datetime.min.time(), timezone.utc).isoformat()


def product_rollup(ledger, product_id: str, features: list[dict], *, month: str | None = None,
                   at: datetime | None = None) -> dict:
    """A product's month: what it cost to run (by category and environment), what its features
    cost to build that month, and each feature's build cost to date and run cost this month."""
    at = at or datetime.now(timezone.utc)
    first, start, end = month_bounds(month, at)
    recs = ledger.query(product_id=product_id, start=start, end=end)
    run = [r for r in recs if r.stage == "run"]
    build = [r for r in recs if r.stage == "build"]
    by_env: dict[str, float] = defaultdict(float)
    for r in run:
        by_env[r.env or "shared"] += r.usd
    every = ledger.query(product_id=product_id)
    per_feature: dict[str, dict] = defaultdict(lambda: {"build_to_date_usd": 0.0, "build_month_usd": 0.0,
                                                        "run_month_usd": 0.0})
    for r in every:
        if r.feature_id and r.stage == "build":
            per_feature[r.feature_id]["build_to_date_usd"] += r.usd
    for r in recs:
        if r.feature_id:
            per_feature[r.feature_id]["build_month_usd" if r.stage == "build" else "run_month_usd"] += r.usd
    rows = []
    for f in features:
        p = per_feature.get(f["feature_id"], {"build_to_date_usd": 0.0, "build_month_usd": 0.0, "run_month_usd": 0.0})
        rows.append({"feature_id": f["feature_id"], "title": f["title"], "status": f["status"],
                     **{k: round(v, 4) for k, v in p.items()},
                     "approved_monthly_usd": (f.get("approved_monthly_usd") or {}).get("production")})
    run_cats, build_cats = _by_category(run), _by_category(build)
    return {"product_id": product_id, "month": f"{first:%Y-%m}",
            "totals": {"run_usd": _total(run_cats), "build_usd": _total(build_cats),
                       "total_usd": round(_total(run_cats) + _total(build_cats), 4),
                       "build_to_date_usd": round(sum(r.usd for r in every if r.stage == "build"), 4)},
            "run": {"by_category": run_cats,
                    "by_env": {k: round(v, 4) for k, v in sorted(by_env.items(), key=lambda kv: (kv[0] == "shared", kv[0]))}},
            "build": {"by_category": build_cats},
            "features": sorted(rows, key=lambda r: -(r["build_to_date_usd"] + r["run_month_usd"])),
            "provisional": any(r.status == "provisional" for r in recs)}

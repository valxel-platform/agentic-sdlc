"""Collectors: one per place money is spent. Each reads the system that knows the number and
upserts records into the cost ledger; running one again replaces its figures.

    llm          the usage ledger: every agent call, priced per model (metered)
    ci, test     GitHub Actions: each job of the feature's PR runs, its merge, and the acceptance
                 runs dispatched for it, by runner OS (metered, minutes rounded up per job)
    build        OpenChoreo build runs (metered at the build plane's minute rate); store app
                 builds (EAS, per build) when a rate is set
    environment  dev and staging: each component's daily cost (OpenChoreo FinOps), shared among the
                 features deployed there that day; what no feature used is the product's (allocated)
    compute      production: each component's daily cost, the product's (allocated)
    aws          Cost Explorer for the product's accounts, by feature tag: a feature's resources
                 are its own (build until release, run after); the rest is the product's (billed)
    people       minutes at gates times an hourly rate, when one is set
    platform     the shared platform's monthly cost, split by products' production compute, when set

Rates live in VRIXEL_COST_RATES (JSON) over the defaults below. GitHub's per-minute prices change:
set them from your plan.
"""

from __future__ import annotations

import json
import math
import os
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

from ..gateway.ledger import UsageRecord
from ..ops.cost import released_at
from .ledger import CostRecord

DEFAULT_RATES = {
    "github_minute_usd": {"linux": 0.008, "windows": 0.016, "macos": 0.08},
    "build_minute_usd": 0.005,  # OpenChoreo build plane: node price per vCPU-minute times a build's size
    "eas_build_usd": 0.0,  # Expo EAS per store build; 0 leaves store builds out
    "people_hour_usd": 0.0,  # loaded cost of an hour at a gate; 0 leaves people out
    "platform_monthly_usd": 0.0,  # OpenChoreo, observability and this tool; 0 leaves it out
    "settle_days": {"aws": 2, "compute": 1},  # a figure is provisional until it is this old
}
LOOKBACK_DAYS = 35


def rates() -> dict:
    r = json.loads(json.dumps(DEFAULT_RATES))
    try:
        r.update(json.loads(os.environ.get("VRIXEL_COST_RATES") or "{}"))
    except ValueError:
        pass
    return r


def utc(s: str | datetime) -> datetime:
    t = s if isinstance(s, datetime) else datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def day_bounds(d: date) -> tuple[str, str]:
    start = datetime(d.year, d.month, d.day, tzinfo=timezone.utc)
    return start.isoformat(), (start + timedelta(days=1)).isoformat()


def settled(d: date, kind: str, at: datetime, r: dict) -> str:
    return "final" if (at.date() - d).days >= int(r["settle_days"].get(kind, 1)) else "provisional"


# --------------------------------------------------------------------------- feature windows
def started_at(state: dict) -> datetime | None:
    ev = state.get("events") or []
    return utc(ev[0]["at"]) if ev else None


def ended_at(state: dict, at: datetime) -> datetime:
    """When the feature stopped building: released, aborted, or still going (now)."""
    if state.get("status") == "done":
        return released_at(state) or at
    if state.get("status") == "aborted":
        return utc((state.get("events") or [{"at": at.isoformat()}])[-1]["at"])
    return at


def env_windows(state: dict, at: datetime) -> dict[str, tuple[datetime, datetime]]:
    """Non-production environment -> (first deployed there, stopped building)."""
    end = ended_at(state, at)
    out = {}
    for e in state.get("events") or []:
        node = e.get("node", "")
        if node.startswith("deploy_") and e.get("kind") == "deployed":
            env = node[len("deploy_"):]
            if env != "production" and env not in out:
                out[env] = (utc(e["at"]), end)
    return out


# --------------------------------------------------------------------------- llm
def collect_llm(records: list[UsageRecord], kind_of, release_of) -> list[CostRecord]:
    """Agent calls by thread, day, phase and model. Feature threads are build cost; incidents are
    the product's run cost; initiative and release planning is build cost no one feature owns."""
    groups: dict[tuple, list[UsageRecord]] = defaultdict(list)
    for r in records:
        groups[(r.product_id, r.feature_id, utc(r.ts).date(), r.phase, r.model)].append(r)
    out = []
    for (pid, thread, d, phase, model), rows in groups.items():
        kind = kind_of(thread) if thread else "product"
        feature = thread if kind == "feature" else ""
        category, stage = ("incident", "run") if kind == "incident" else ("llm", "build")
        start, end = day_bounds(d)
        tokens = sum(r.total_input + r.output_tokens for r in rows)
        out.append(CostRecord(
            key=f"llm:{thread or pid}:{d}:{phase}:{model}", product_id=pid, feature_id=feature, stage=stage,
            category=category, usd=sum(r.est_cost_usd for r in rows), period_start=start, period_end=end,
            source="usage-ledger", service=model, resource=thread or pid, quantity=tokens, unit="tokens",
            basis="metered", detail={"phase": phase, "calls": len(rows), "description": f"{phase} agents"}))
    return out


# --------------------------------------------------------------------------- GitHub Actions
def _os(labels: list[str]) -> str:
    text = " ".join(labels).lower()
    return "macos" if "macos" in text else "windows" if "windows" in text else "linux"


def _run_records(state: dict, repo: str, runs: list[dict], category: str, r: dict) -> list[CostRecord]:
    out = []
    for run in runs:
        minutes: dict[str, float] = defaultdict(float)
        for job in run.get("jobs") or []:
            if job.get("started_at") and job.get("completed_at"):
                secs = (utc(job["completed_at"]) - utc(job["started_at"])).total_seconds()
                minutes[_os(job.get("labels") or [])] += math.ceil(max(secs, 1) / 60)  # billed per job, rounded up
        if not minutes:
            continue
        usd = sum(m * float(r["github_minute_usd"].get(os_, 0.0)) for os_, m in minutes.items())
        at = run.get("created_at") or datetime.now(timezone.utc).isoformat()
        out.append(CostRecord(
            key=f"{category}:{repo}:{run['id']}", product_id=state["product_id"], feature_id=state["feature_id"],
            stage="build", category=category, usd=usd, period_start=at, period_end=run.get("updated_at") or at,
            source="github-actions", service="GitHub Actions", resource=f"{repo}#{run['id']}",
            quantity=sum(minutes.values()), unit="minutes", basis="metered",
            status="final" if run.get("status") == "completed" else "provisional",
            detail={"workflow": run.get("name", ""), "minutes": dict(minutes),
                    "description": f"{run.get('name', 'workflow')} on {repo}"}))
    return out


def collect_ci(github, state: dict, r: dict) -> list[CostRecord]:
    """The feature's PR runs (by branch), its merges (by merge commit) and the acceptance runs
    dispatched for it (their run name carries the feature id)."""
    lister = getattr(github, "workflow_runs", None)
    if lister is None:
        return []
    out, seen = [], set()
    for pr in (state.get("pull_requests") or {}).values():
        runs = []
        if pr.get("branch"):
            runs += lister(repo=pr["repo"], branch=pr["branch"])
        if pr.get("merge_sha"):
            runs += lister(repo=pr["repo"], head_sha=pr["merge_sha"])
        runs = [x for x in runs if (pr["repo"], x["id"]) not in seen and not seen.add((pr["repo"], x["id"]))]
        out += _run_records(state, pr["repo"], runs, "ci", r)
    acc = (state.get("change_set") or {}).get("acceptance") or {}
    if acc.get("repo"):
        runs = [x for x in lister(repo=acc["repo"], event="repository_dispatch")
                if state["feature_id"] in (x.get("display_title") or x.get("name") or "")]
        out += _run_records(state, acc["repo"], runs, "test", r)
    return out


# --------------------------------------------------------------------------- builds
def collect_builds(openchoreo, state: dict, r: dict) -> list[CostRecord]:
    out = []
    build = (state.get("delivery") or {}).get("build") or {}
    for res in build.get("results") or []:
        if not res.get("run_id"):
            continue
        try:
            st = openchoreo.run_status(run_id=res["run_id"], component=res["component"], project=res.get("project"),
                                       namespace=res.get("namespace"))
        except Exception:
            continue
        if not st.get("started_at") or not st.get("finished_at"):
            continue
        minutes = math.ceil((utc(st["finished_at"]) - utc(st["started_at"])).total_seconds() / 60)
        out.append(CostRecord(
            key=f"build:{res['component']}:{res['run_id']}", product_id=state["product_id"],
            feature_id=state["feature_id"], stage="build", category="build",
            usd=minutes * float(r["build_minute_usd"]), period_start=st["started_at"], period_end=st["finished_at"],
            source="openchoreo", service="OpenChoreo build", resource=res["run_id"], quantity=minutes,
            unit="minutes", basis="metered", detail={"component": res["component"],
                                                     "description": f"image build for {res['component']}"}))
    if float(r["eas_build_usd"]):
        for repo, dep in (state.get("deployments") or {}).items():
            if dep.get("kind") != "store":
                continue
            stages = [s for s in ("dev", "staging", "production")
                      if any(e.get("node") == f"deploy_{s}" for e in state.get("events") or [])] or ["dev"]
            for s in stages:
                at = next((e["at"] for e in state.get("events") or [] if e.get("node") == f"deploy_{s}"),
                          datetime.now(timezone.utc).isoformat())
                out.append(CostRecord(
                    key=f"eas:{repo}:{state['feature_id']}:{s}", product_id=state["product_id"],
                    feature_id=state["feature_id"], stage="build", category="build",
                    usd=2 * float(r["eas_build_usd"]), period_start=at, period_end=at, source="rates",
                    service="Expo EAS Build", resource=repo, quantity=2, unit="builds", basis="metered",
                    detail={"description": f"Android and iOS builds of {repo} for {s}"}))
    return out


# --------------------------------------------------------------------------- people
def collect_people(state: dict, r: dict) -> list[CostRecord]:
    rate = float(r["people_hour_usd"])
    if not rate:
        return []
    out = []
    for i, g in enumerate(state.get("gate_decisions") or []):
        minutes = float(g.get("active_minutes") or 0.0)
        if minutes and g.get("at"):
            out.append(CostRecord(
                key=f"people:{state['feature_id']}:{i}", product_id=state["product_id"], feature_id=state["feature_id"],
                stage="build", category="people", usd=minutes / 60 * rate, period_start=g["at"], period_end=g["at"],
                source="rates", service="People", resource=f"{g.get('gate', '')} {g.get('approver', '')}".strip(),
                quantity=minutes, unit="minutes", basis="estimate",
                detail={"description": f"{g.get('gate', '').upper()} decision"}))
    return out


# --------------------------------------------------------------------------- environments and compute
def _share(windows: list[tuple[str, datetime, datetime]], d: date) -> dict[str, float]:
    """Each feature's share of a day's cost: the part of the day it was deployed there, scaled
    down when features overlap so the shares never exceed the whole day."""
    day0 = datetime(d.year, d.month, d.day, tzinfo=timezone.utc)
    day1 = day0 + timedelta(days=1)
    f = {}
    for fid, a, b in windows:
        overlap = (min(b, day1) - max(a, day0)).total_seconds()
        if overlap > 0:
            f[fid] = f.get(fid, 0.0) + overlap / 86400
    total = sum(f.values())
    return {k: v / total for k, v in f.items()} if total > 1 else f


def collect_runtime(costs, product_id: str, targets: list[tuple[str, str, str, bool]],
                    windows: dict[tuple[str, str, str], list[tuple[str, datetime, datetime]]],
                    at: datetime, r: dict) -> list[CostRecord]:
    """targets: (namespace, project, env, production?). windows: (project, env, component) ->
    the features deployed there and when. Each day's component cost is split among them; the
    rest, and production, is the product's own."""
    daily = getattr(costs, "component_costs_daily", None)
    if daily is None:
        return []
    start = datetime.combine(at.date() - timedelta(days=LOOKBACK_DAYS), datetime.min.time(), timezone.utc)
    out = []
    for ns, project, env, production in targets:
        for row in daily(namespace=ns, project=project, env=env, start=start, end=at):
            d, comp, usd = date.fromisoformat(row["day"]), row["component"], float(row["usd"])
            ds, de = day_bounds(d)
            status = settled(d, "compute", at, r)
            base = dict(product_id=product_id, env=env, period_start=ds, period_end=de, source="openchoreo-finops",
                        service="Kubernetes compute", resource=f"{project}/{comp}", basis="allocated", status=status)
            if production:
                out.append(CostRecord(key=f"compute:{product_id}:{project}:{env}:{comp}:{d}", stage="run",
                                      category="compute", usd=usd, **base,
                                      detail={"description": f"{comp} in {env}"}))
                continue
            shares = _share(windows.get((project, env, comp), []), d)
            for fid, s in shares.items():
                out.append(CostRecord(key=f"env:{fid}:{project}:{env}:{comp}:{d}", stage="build", category="environment",
                                      usd=usd * s, feature_id=fid, **base,
                                      detail={"share": round(s, 4), "description": f"{comp} in {env} while in flight"}))
            idle = 1.0 - sum(shares.values())
            if idle > 1e-9:
                out.append(CostRecord(key=f"env:{product_id}:{project}:{env}:{comp}:{d}", stage="run",
                                      category="environment", usd=usd * idle, **base,
                                      detail={"share": round(idle, 4), "description": f"{comp} in {env}, no feature in flight"}))
    return out


# --------------------------------------------------------------------------- AWS
def collect_aws(costs, product_id: str, accounts: dict[str, str], releases: dict[str, datetime | None],
                at: datetime, r: dict) -> list[CostRecord]:
    """The product's AWS spend per account by day, service and feature tag. A feature's spend is
    build cost until it reached production, run cost after; untagged spend is the product's."""
    daily = getattr(costs, "aws_costs_daily", None)
    if daily is None:
        return []
    start = datetime.combine(at.date() - timedelta(days=LOOKBACK_DAYS), datetime.min.time(), timezone.utc)
    out = []
    for env, account in sorted(accounts.items()):
        for row in daily(product_id=product_id, account=account, start=start, end=at):
            d = date.fromisoformat(row["day"])
            fid = row.get("feature_id") or ""
            ds, de = day_bounds(d)
            rel = releases.get(fid)
            stage = "build" if fid and (rel is None or utc(de) <= rel) else "run"
            out.append(CostRecord(
                key=f"aws:{product_id}:{account}:{fid or '-'}:{row['service']}:{d}", product_id=product_id,
                feature_id=fid, stage=stage, category="aws", env=env, usd=float(row["usd"]), period_start=ds,
                period_end=de, source="cost-explorer", service=row["service"], resource=account, basis="billed",
                status=settled(d, "aws", at, r), detail={"description": f"{row['service']} in {env}"}))
    return out


def collect_platform(product_compute: dict[str, float], month: date, at: datetime, r: dict) -> list[CostRecord]:
    """The shared platform's cost for a month (to date), split by products' production compute."""
    monthly = float(r["platform_monthly_usd"])
    if not monthly or not product_compute:
        return []
    first = month.replace(day=1)
    nxt = (first + timedelta(days=32)).replace(day=1)
    days = (nxt - first).days
    elapsed = max(0.0, min(days, (at - datetime.combine(first, datetime.min.time(), timezone.utc)).total_seconds() / 86400))
    total = sum(product_compute.values())
    sharing = {p: c for p, c in product_compute.items() if c > 0} if total else dict(product_compute)
    out = []
    for pid, c in sorted(sharing.items()):
        share = c / total if total else 1 / len(sharing)
        out.append(CostRecord(
            key=f"platform:{pid}:{first:%Y-%m}", product_id=pid, stage="run", category="platform",
            usd=monthly * elapsed / days * share, period_start=day_bounds(first)[0], period_end=day_bounds(nxt)[0],
            source="allocation", service="Vrixel platform", resource=f"{first:%Y-%m}", basis="allocated",
            status="final" if at.date() >= nxt else "provisional",
            detail={"share": round(share, 4), "description": "OpenChoreo, observability and the SDLC tool"}))
    return out

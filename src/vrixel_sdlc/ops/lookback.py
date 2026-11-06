"""Looking back: how many incidents a product had, how they ended, and which changes caused them.

Per calendar month (UTC), from the incidents' own records and the change log:

    incidents          opened, by severity; the components they started in
    fixed by the tool  mitigated under the remediation policy and recovered without a person
    escalated          on-call was needed (paged, or waiting for them)
    time to recover    median and worst minutes from the first signal to recovery (or on-call
                       resolving it)
    changes            production changes by type (standard, emergency, automatic), and failed ones
    change failure     releases that caused an incident (an incident's flag off or rollback undid
                       them), as a share of releases
    causes             the releases behind the most incidents

Merged incidents count once, as the incident they joined.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timezone
from statistics import median


def _at(value) -> datetime | None:
    try:
        t = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def months_back(n: int, now: datetime | None = None) -> list[str]:
    now = now or datetime.now(timezone.utc)
    y, m, out = now.year, now.month, []
    for _ in range(max(1, n)):
        out.append(f"{y:04d}-{m:02d}")
        y, m = (y - 1, 12) if m == 1 else (y, m - 1)
    return list(reversed(out))


def recovered_at(incident: dict) -> datetime | None:
    for e in incident.get("events") or []:
        if e.get("kind") in ("recovered", "resolved"):
            return _at(e.get("at"))
    return None


def outcome(incident: dict) -> str:
    """fixed_by_tool | escalated | open | other."""
    taken = incident.get("taken") or []
    if incident.get("escalated"):
        return "escalated"
    if recovered_at(incident) and any(t.get("by") == "agent:remediate" and t.get("ok") for t in taken):
        return "fixed_by_tool"
    if incident.get("status") not in ("postmortem", "closed"):
        return "open"
    return "other"


def report(incidents: list[dict], changes: list[dict], links: list[dict], months: list[str]) -> dict:
    rows = {m: {"month": m, "incidents": 0, "by_severity": Counter(), "components": Counter(),
                "fixed_by_tool": 0, "escalated": 0, "open": 0, "recover_minutes": [],
                "changes": Counter(), "failed_changes": 0, "releases": 0, "releases_causing_incidents": 0}
            for m in months}
    caused = defaultdict(set)  # change id -> incidents it caused
    for x in links:
        caused[x["change_id"]].add(x["incident_id"])
    for inc in incidents:
        if inc.get("status") == "merged":
            continue
        opened = _at(inc.get("opened_at"))
        row = rows.get(opened.strftime("%Y-%m")) if opened else None
        if row is None:
            continue
        row["incidents"] += 1
        row["by_severity"][inc.get("severity") or "sev3"] += 1
        if inc.get("component"):
            row["components"][inc["component"].rsplit("/", 1)[-1]] += 1
        o = outcome(inc)
        if o in ("fixed_by_tool", "escalated", "open"):
            row[o] += 1
        done = recovered_at(inc)
        if done and opened and done >= opened:
            row["recover_minutes"].append(round((done - opened).total_seconds() / 60, 1))
    by_cause = Counter()
    for c in changes:
        at = _at(c.get("at"))
        row = rows.get(at.strftime("%Y-%m")) if at else None
        if c["id"] in caused:
            by_cause[c["id"]] += len(caused[c["id"]])
        if row is None:
            continue
        row["changes"][c["type"]] += 1
        if c.get("outcome") == "failed":
            row["failed_changes"] += 1
        if c["kind"] == "release" and c.get("action") == "deploy":
            row["releases"] += 1
            if c["id"] in caused:
                row["releases_causing_incidents"] += 1
    out = []
    for m in months:
        r = rows[m]
        mins = r.pop("recover_minutes")
        out.append({**r, "by_severity": dict(sorted(r["by_severity"].items())),
                    "components": dict(r["components"].most_common(5)), "changes": dict(r["changes"]),
                    "change_failure_rate": round(r["releases_causing_incidents"] / r["releases"], 3) if r["releases"] else None,
                    "recover_minutes_median": median(mins) if mins else None,
                    "recover_minutes_max": max(mins) if mins else None})
    named = {c["id"]: c for c in changes}
    causes = [{"change_id": cid, "summary": named[cid]["summary"], "at": named[cid]["at"], "incidents": sorted(caused[cid])}
              for cid, _ in by_cause.most_common(10) if cid in named]
    total = {k: sum(r[k] for r in out) for k in ("incidents", "fixed_by_tool", "escalated", "releases",
                                                 "releases_causing_incidents", "failed_changes")}
    total["change_failure_rate"] = (round(total["releases_causing_incidents"] / total["releases"], 3)
                                    if total["releases"] else None)
    return {"months": out, "total": total, "causes": causes}

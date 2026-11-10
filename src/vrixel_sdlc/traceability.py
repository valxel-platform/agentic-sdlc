"""Traceability: from the reason for a feature to what runs in production, and back.

Each link is already written down somewhere in the pipeline; this reads them into one view:

    metric (brief) <- goal (spec) <- acceptance criterion (spec)
        -> task (plan, with its test) -> pull request (build) -> repo
        -> acceptance result (the cross-repo run) -> deployment (environment, release)
        -> production incidents on those components
    decisions (the HLD's ADRs) and the repos they change sit beside them

and lists the gaps: a criterion no task covers, a task without a pull request, a criterion whose
acceptance test failed or never ran, a goal without a metric, an affected repo nothing changes.

Nothing is stored twice: the documents, the feature's state and the incidents are the record, so
the view is always what the pipeline has now. `search` answers the other direction across a
product: which features, criteria and pull requests touched a repo, or a pull request.
"""

from __future__ import annotations

import re

from .agents.parse import bullets, parse_acs, parse_affected_repos, parse_tasks, parse_trace, section

_ID_LINE = re.compile(r"^((?:M|G)\d+)\s*:\s*(.+?)\s*$")
_ADR = re.compile(r"^(ADR-[\w.-]+)\s*:\s*(.+?)\s*$")


def _ids(text: str, heading: str) -> list[tuple[str, str]]:
    return [m.groups() for b in bullets(section(text, heading)) if (m := _ID_LINE.match(b))]


def _pr_ref(key: str, pr: dict) -> dict:
    return {"key": key, "repo": pr.get("repo", ""), "number": pr.get("number"), "url": pr.get("url", ""),
            "task": pr.get("task_id", ""), "merged": bool(pr.get("merged"))}


def acceptance_results(state: dict) -> dict[str, dict[str, str]]:
    """Each criterion's latest acceptance result per stage (dev, staging, ...)."""
    out: dict[str, dict[str, str]] = {}
    for stage, q in (state.get("quality") or {}).items():
        for r in q.get("suites") or []:
            crit = (r.get("details") or {}).get("criteria") or {}
            for ac, st in crit.items():
                out.setdefault(ac, {})[stage] = st
    return out


def build(state: dict, load, incidents: list[dict] | None = None) -> dict:
    """The feature's trace. `load(kind)` returns a document's text ('' when it doesn't exist)."""
    opp, prd, hld, plan = (load(k) for k in ("OPP", "PRD", "HLD", "PLAN"))
    metrics = [{"id": i, "text": t, "goals": []} for i, t in _ids(opp, "Success metrics")]
    goals = []
    for gid, text in _ids(prd, "Goals"):
        measured = re.findall(r"\bM\d+\b", text)
        goals.append({"id": gid, "text": text, "metrics": measured})
        for m in metrics:
            if m["id"] in measured:
                m["goals"].append(gid)
    tasks = list(state.get("tasks") or parse_tasks(plan))
    tests = {ac: (task, test) for ac, task, test in parse_trace(plan)}
    prs = {k: _pr_ref(k, pr) for k, pr in (state.get("pull_requests") or {}).items()}
    by_task: dict[str, list[dict]] = {}
    for p in prs.values():
        by_task.setdefault(p["task"], []).append(p)
    deployments = {r: d for r, d in (state.get("deployments") or {}).items() if d.get("kind") != "resource"}
    results = acceptance_results(state)
    merged = bool((state.get("change_set") or {}).get("merged"))
    built = bool(prs) or (state.get("phase_status") or {}).get("build") in ("passed", "resolved")

    criteria = []
    for ac, text in parse_acs(prd).items():
        mine = [t for t in tasks if ac in (t.get("acs") or [])]
        repos = sorted({t["repo"] for t in mine})
        criteria.append({
            "id": ac, "text": text,
            "tasks": [t["id"] for t in mine],
            "test": (tests.get(ac) or ("", ""))[1],
            "prs": [p for t in mine for p in by_task.get(t["id"], [])],
            "repos": repos,
            "acceptance": results.get(ac, {}),
            "deployed": {r: deployments[r].get("env", "") for r in repos if r in deployments},
        })

    affected = parse_affected_repos(hld)
    repos = []
    for r in affected or [{"name": n, "role": ""} for n in sorted({t["repo"] for t in tasks})]:
        d = deployments.get(r["name"]) or {}
        repos.append({"repo": r["name"], "role": r["role"],
                      "tasks": [t["id"] for t in tasks if t["repo"] == r["name"]],
                      "prs": [p for p in prs.values() if p["repo"] == r["name"]],
                      "deployed": {"env": d.get("env", ""), "release": d.get("release", "")} if d else None})
    decisions = [{"id": m.group(1), "text": m.group(2)} for b in bullets(section(hld, "ADRs")) if (m := _ADR.match(b))]

    gaps = []
    gaps += [f"{g['id']} has no metric that measures it" for g in goals if not g["metrics"]]
    gaps += [f"{m['id']} measures no goal" for m in metrics if not m["goals"] and goals]
    if tasks:
        gaps += [f"{c['id']} is covered by no task" for c in criteria if not c["tasks"]]
        gaps += [f"{r['repo']} is affected but no task changes it" for r in repos
                 if not r["tasks"] and r["role"] not in ("", "library")]
    if built:
        gaps += [f"{t['id']} ({t['repo']}) has no pull request" for t in tasks if not by_task.get(t["id"])]
    for c in criteria:
        bad = [st for st, s in c["acceptance"].items() if s == "failed"]
        if bad:
            gaps.append(f"{c['id']}'s acceptance test failed in {', '.join(sorted(bad))}")
        elif results and not c["acceptance"]:
            gaps.append(f"{c['id']} has no acceptance result")
    release = state.get("release") or {}
    return {
        "feature_id": state.get("feature_id", ""),
        "title": state.get("title", ""),
        "profile": state.get("profile", "feature"),
        "metrics": metrics, "goals": goals, "criteria": criteria, "decisions": decisions, "repos": repos,
        "release": {"flag": state.get("flag", ""), "percent": release.get("flag_percent"),
                    "production": bool(release.get("production")), "merged": merged},
        "incidents": list(incidents or []),
        "gaps": gaps,
    }


def search(rows: list[dict], *, repo: str = "", pr: str = "") -> list[dict]:
    """Across a product's features (`rows` of {"feature_id", "title", "state"}): which touched a
    repo or a pull request ('org/repo#12'), with the criteria and tasks involved."""
    out = []
    for row in rows:
        s = row["state"]
        prs = s.get("pull_requests") or {}
        tasks = list(s.get("tasks") or [])
        if pr:
            hits = [p for k, p in prs.items() if k == pr]
            mine = [t for t in tasks if any(h.get("task_id") == t["id"] for h in hits)]
        else:
            mine = [t for t in tasks if t.get("repo") == repo]
            hits = [p for p in prs.values() if p.get("repo") == repo]
        if not mine and not hits:
            continue
        out.append({"feature_id": row["feature_id"], "title": row["title"], "status": s.get("status", ""),
                    "criteria": sorted({ac for t in mine for ac in t.get("acs") or []}),
                    "tasks": [t["id"] for t in mine],
                    "prs": [_pr_ref(k, p) for k, p in prs.items() if p in hits]})
    return out

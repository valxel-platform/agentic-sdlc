"""Traceability: from the reason for a feature to what runs in production, and back.

Each link is already written down somewhere in the pipeline; this reads them into one view:

    metric (brief) <- goal (spec) <- requirement (spec: acceptance criteria and NFRs)
        -> where the design handles it (UX spec, HLD Coverage)
        -> task (plan) -> test cases (plan) -> the tests on the branch (build)
        -> pull request -> repo -> results (CI, acceptance runs, quality suites)
        -> deployment (environment, release) -> production incidents on those components
    decisions (the HLD's ADRs) and the repos they change sit beside them

and lists the gaps: a requirement with no goal, task or test case, a test case whose test isn't on
its branch, hasn't passed or is manual, a task without a pull request, a goal without a metric or
requirement, an affected repo nothing changes. A requirement the plan defers (with a reason) is
listed apart, not as a gap. With no gaps the matrix is complete, which G6 requires before
production (graph/pipeline.py); `rows` and `csv` give the matrix as a table to export.

Nothing is stored twice: the documents, the feature's state and the incidents are the record, so
the view is always what the pipeline has now. `search` answers the other direction across a
product: which features, criteria and pull requests touched a repo, or a pull request.
"""

from __future__ import annotations

import re

import csv
import io

from .agents.parse import (
    bullets,
    parse_ac_goals,
    parse_acs,
    parse_affected_repos,
    parse_cases,
    parse_deferred,
    parse_nfrs,
    parse_tasks,
    parse_trace,
    section,
)

_COVER = re.compile(r"^-\s*((?:AC|NFR)-\d+)\s*:\s*(.+?)\s*$", re.M)

_ID_LINE = re.compile(r"^((?:M|G)\d+)\s*:\s*(.+?)\s*$")
_ADR = re.compile(r"^(ADR-[\w.-]+)\s*:\s*(.+?)\s*$")


def _ids(text: str, heading: str) -> list[tuple[str, str]]:
    return [m.groups() for b in bullets(section(text, heading)) if (m := _ID_LINE.match(b))]


def _pr_ref(key: str, pr: dict, change_set_merged: bool = False) -> dict:
    return {"key": key, "repo": pr.get("repo", ""), "number": pr.get("number"), "url": pr.get("url", ""),
            "task": pr.get("task_id", ""),
            "merged": bool(pr.get("merged") or pr.get("state") == "merged" or change_set_merged)}


def acceptance_results(state: dict) -> dict[str, dict[str, str]]:
    """Each criterion's latest acceptance result per stage (dev, staging, ...)."""
    out: dict[str, dict[str, str]] = {}
    for stage, q in (state.get("quality") or {}).items():
        for r in q.get("suites") or []:
            crit = (r.get("details") or {}).get("criteria") or {}
            for ac, st in crit.items():
                out.setdefault(ac, {})[stage] = st
    return out


def _suite_status(state: dict, name: str) -> str:
    rows = [r for q in (state.get("quality") or {}).values() for r in q.get("suites") or [] if r.get("suite") == name]
    if any(r.get("state") == "failed" for r in rows):
        return "failed"
    return "passed" if any(r.get("state") == "passed" for r in rows) else "pending"


def case_status(case: dict, state: dict, evidence: dict, results: dict, merged_tasks: set[str]) -> str:
    """passed | failed | written (on the branch, not merged yet) | missing | pending (not run yet) | manual."""
    via = case.get("via")
    if via == "test":
        ev = evidence.get(case["id"])
        if not ev:
            return "pending"
        if not ev.get("found"):
            return "missing"
        return "passed" if case.get("task") in merged_tasks else "written"
    if via == "acceptance":
        got = results.get(case["req"]) or {}
        if any(v == "failed" for v in got.values()):
            return "failed"
        return "passed" if any(v == "passed" for v in got.values()) else "pending"
    if via == "suite":
        return _suite_status(state, case.get("suite", ""))
    return "manual" if via == "manual" else "unknown"


def build(state: dict, load, incidents: list[dict] | None = None) -> dict:
    """The feature's trace. `load(kind)` returns a document's text ('' when it doesn't exist)."""
    opp, prd, hld, plan, ux = (load(k) for k in ("OPP", "PRD", "HLD", "PLAN", "UX"))
    metrics = [{"id": i, "text": t, "goals": []} for i, t in _ids(opp, "Success metrics")]
    goals = []
    for gid, text in _ids(prd, "Goals"):
        measured = re.findall(r"\bM\d+\b", text)
        goals.append({"id": gid, "text": text, "metrics": measured, "requirements": []})
        for m in metrics:
            if m["id"] in measured:
                m["goals"].append(gid)
    tasks = list(state.get("tasks") or parse_tasks(plan))
    cases = list(state.get("cases") or parse_cases(plan))
    deferred = parse_deferred(plan)
    traced: dict[str, list[str]] = {}
    labels: dict[str, str] = {}  # older plans: 'AC-1 -> TASK-1 / TEST-1'
    for req, task, label in parse_trace(plan):
        traced.setdefault(req, []).append(task)
        if label:
            labels.setdefault(req, label)
    cs_merged = bool((state.get("change_set") or {}).get("merged"))
    prs = {k: _pr_ref(k, pr, cs_merged) for k, pr in (state.get("pull_requests") or {}).items()}
    by_task: dict[str, list[dict]] = {}
    for p in prs.values():
        by_task.setdefault(p["task"], []).append(p)
    merged_tasks = {t for t, ps in by_task.items() if ps and all(p["merged"] for p in ps)}
    deployments = {r: d for r, d in (state.get("deployments") or {}).items() if d.get("kind") != "resource"}
    results = acceptance_results(state)
    evidence = state.get("case_evidence") or {}
    merged = bool((state.get("change_set") or {}).get("merged"))
    built = bool(prs) or (state.get("phase_status") or {}).get("build") in ("passed", "resolved")
    ux_cover = dict(_COVER.findall(section(ux, "Coverage")))
    hld_cover = dict(_COVER.findall(section(hld, "Coverage")))
    ac_goals = parse_ac_goals(prd)
    task_ids = {t["id"]: t for t in tasks}

    def requirement(rid: str, kind: str, text: str, served: list[str]) -> dict:
        mine = [t for t in tasks if rid in (t.get("acs") or [])]
        mine += [task_ids[i] for i in traced.get(rid, []) if i in task_ids and task_ids[i] not in mine]
        repos = sorted({t["repo"] for t in mine})
        my_cases = [{**c, "status": case_status(c, state, evidence, results, merged_tasks)}
                    for c in cases if c["req"] == rid]
        for g in goals:
            if g["id"] in served:
                g["requirements"].append(rid)
        return {
            "id": rid, "type": kind, "text": text, "goals": served,
            "design": {k: v for k, v in (("ux", ux_cover.get(rid, "")), ("hld", hld_cover.get(rid, ""))) if v},
            "deferred": deferred.get(rid) if rid in deferred else None,
            "tasks": [t["id"] for t in mine],
            "cases": my_cases,
            "test": next((c.get("ref", "") for c in my_cases), labels.get(rid, "")),
            "prs": [p for t in mine for p in by_task.get(t["id"], [])],
            "repos": repos,
            "acceptance": results.get(rid, {}),
            "deployed": {r: deployments[r].get("env", "") for r in repos if r in deployments},
        }

    criteria = [requirement(ac, "functional", text, ac_goals.get(ac) or []) for ac, text in parse_acs(prd).items()]
    nfrs = [requirement(n, spec["kind"], spec["text"], [spec["goal"]] if spec["goal"] else [])
            for n, spec in parse_nfrs(prd).items()]

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
    if goals and any(c["goals"] for c in criteria):  # documents that link criteria to goals
        gaps += [f"{c['id']} serves no goal" for c in criteria if not c["goals"]]
        gaps += [f"{g['id']} has no requirement that delivers it" for g in goals if not g["requirements"]]
    planned = bool(tasks)
    if planned:
        for c in criteria + nfrs:
            if c["deferred"] is not None:
                continue
            if not c["tasks"]:
                gaps.append(f"{c['id']} is covered by no task")
            if cases and not c["cases"]:
                gaps.append(f"{c['id']} has no test case")
        if not cases and plan:
            gaps.append("The plan lists no test cases")
        gaps += [f"{r['repo']} is affected but no task changes it" for r in repos
                 if not r["tasks"] and r["role"] not in ("", "library")]
        gaps += [f"{t['id']} implements no requirement and doesn't say why" for t in tasks
                 if not t.get("acs") and not t.get("why") and not any(t["id"] in v for v in traced.values())]
    if built:
        gaps += [f"{t['id']} ({t['repo']}) has no pull request" for t in tasks if not by_task.get(t["id"])]
        for c in criteria + nfrs:
            if c["deferred"] is not None:
                continue
            for case in c["cases"]:
                if case["status"] != "passed":
                    gaps.append(f"{case['id']} ({c['id']}, {case['kind']}): {CASE_GAP.get(case['status'], case['status'])}")
    for c in criteria:
        bad = [st for st, s in c["acceptance"].items() if s == "failed"]
        if bad:
            gaps.append(f"{c['id']}'s acceptance test failed in {', '.join(sorted(bad))}")
        elif results and not c["acceptance"] and c["deferred"] is None:
            gaps.append(f"{c['id']} has no acceptance result")
    release = state.get("release") or {}
    return {
        "feature_id": state.get("feature_id", ""),
        "title": state.get("title", ""),
        "profile": state.get("profile", "feature"),
        "metrics": metrics, "goals": goals, "criteria": criteria, "nfrs": nfrs, "decisions": decisions, "repos": repos,
        "deferred": [{"id": c["id"], "reason": c["deferred"]} for c in criteria + nfrs if c["deferred"] is not None],
        "release": {"flag": state.get("flag", ""), "percent": release.get("flag_percent"),
                    "production": bool(release.get("production")), "merged": merged},
        "incidents": list(incidents or []),
        "gaps": gaps,
        "complete": not gaps,
    }


CASE_GAP = {"missing": "its test isn't on the branch under the planned name", "written": "its pull request isn't merged",
            "failed": "failed", "pending": "hasn't run yet", "manual": "is a manual check (needs an override)",
            "unknown": "has no test the pipeline can check"}

COLUMNS = ("feature", "requirement", "type", "goals", "requirement_text", "design", "deferred", "tasks",
           "pull_requests", "case", "case_kind", "case_checks", "test", "status", "acceptance", "deployed")


def rows(trace: dict) -> list[dict]:
    """The matrix as a table: one row per test case (a requirement without one gets a row too)."""
    out = []
    for r in trace.get("criteria", []) + trace.get("nfrs", []):
        base = {"feature": trace.get("feature_id", ""), "requirement": r["id"], "type": r["type"],
                "goals": " ".join(r["goals"]), "requirement_text": r["text"],
                "design": "; ".join(f"{k}: {v}" for k, v in r["design"].items()),
                "deferred": r["deferred"] or "", "tasks": " ".join(r["tasks"]),
                "pull_requests": " ".join(p["url"] or p["key"] for p in r["prs"]),
                "acceptance": " ".join(f"{k}={v}" for k, v in sorted(r["acceptance"].items())),
                "deployed": " ".join(f"{k}={v}" for k, v in sorted(r["deployed"].items()))}
        for c in r["cases"] or [None]:
            out.append({**base, "case": c["id"] if c else "", "case_kind": c["kind"] if c else "",
                        "case_checks": c["what"] if c else "", "test": c["ref"] if c else "",
                        "status": c["status"] if c else ("deferred" if r["deferred"] is not None else "no test case")})
    return out


def to_csv(trace: dict) -> str:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=COLUMNS)
    w.writeheader()
    w.writerows(rows(trace))
    return buf.getvalue()


def snapshot(trace: dict) -> dict:
    """What a release record keeps of the matrix: compact, and enough for an auditor."""
    return {"complete": trace["complete"], "gaps": list(trace["gaps"]), "deferred": list(trace["deferred"]),
            "requirements": [{"id": r["id"], "type": r["type"], "goals": r["goals"], "tasks": r["tasks"],
                              "cases": [{"id": c["id"], "kind": c["kind"], "test": c["ref"], "status": c["status"]}
                                        for c in r["cases"]],
                              "deferred": r["deferred"]} for r in trace.get("criteria", []) + trace.get("nfrs", [])]}


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

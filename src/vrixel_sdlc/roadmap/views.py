"""What the web app and API show: one initiative or release, and a product's roadmap (the Git
record merged with the threads the engine is running, with progress computed from features)."""

from __future__ import annotations

from . import read_roadmap


def _live(f: dict) -> bool:
    """Released and visible: in production with its flag on (or released before flags)."""
    return f["status"] == "released" and f.get("flag_on") is not False


def _common(engine, thread_id: str, s: dict, kind: str) -> dict:
    snap = engine._g(thread_id).get_state(engine._config(thread_id))
    return {
        "feature_id": thread_id, "kind": kind, "product_id": s.get("product_id"), "title": s.get("title"),
        "created_by": s.get("created_by", ""), "status": s.get("status"), "running": thread_id in engine.running,
        "error": engine.errors.get(thread_id), "pending": [{"interrupt_id": i.id, **i.value} for i in snap.interrupts],
        "gate_decisions": s.get("gate_decisions", []), "events": s.get("events", []),
        "cost_usd": round(float(s.get("cost_usd", 0.0)), 4), "record_pr": s.get("record_pr") or {},
    }


def thread_view(engine, thread_id: str) -> dict:
    from ..engine import kind_of

    kind = kind_of(thread_id)
    s = engine._state(thread_id)
    if not s:
        raise KeyError(f"Unknown {kind} {thread_id}")
    out = _common(engine, thread_id, s, kind)
    if kind == "incident":
        from ..ops.signals import still_firing

        from ..ops.correlate import root

        signals = engine.services.signals.for_incident(thread_id)
        try:
            root_component, root_why = root(engine.services.snapshot(s["product_id"]), signals, s.get("component", ""))
        except Exception:
            root_component, root_why = s.get("component", ""), ""
        return out | {"root": {"component": root_component, "why": root_why},"severity": s.get("severity"), "component": s.get("component", ""),
                      "opened_at": s.get("opened_at", ""), "triage": s.get("triage", ""),
                      "actions": s.get("actions", []), "taken": s.get("taken", []), "links": s.get("links", {}),
                      "postmortem": s.get("postmortem", ""), "signals": signals, "firing": len(still_firing(signals)),
                      "suspects": engine.suspects(s["product_id"], s.get("component", ""))[:5],
                      "analysis": s.get("analysis") or {}, "escalated": bool(s.get("escalated"))}
    if kind == "inception":
        return out | {"vision": s.get("vision") or {}, "brief": s.get("brief", ""), "architecture": s.get("architecture", ""),
                      "roadmap": s.get("roadmap", ""), "reports": s.get("reports") or {}, "setup": s.get("setup") or {},
                      "council": list((engine.services.settings.inception or {}).get("council") or [])}
    feats = engine.feature_summaries(s["product_id"])
    if kind == "initiative":
        d = s.get("draft") or {}
        linked = [f for f in feats if f.get("initiative_id") == thread_id]
        spent = sum(f["cost_usd"] for f in linked)
        started = {f["title"].lower() for f in linked}
        out |= {"request": s.get("request", ""), "content": s.get("content", ""), "draft": d,
                "findings": s.get("findings", []), "features": linked,
                "candidates": [{**c, "started": c["title"].lower() in started} for c in d.get("candidates", [])],
                "budget_usd": d.get("budget_usd", 0.0), "spent_usd": round(spent, 2),
                "progress": {"released": sum(_live(f) for f in linked), "total": len(linked)}}
    else:
        by_id = {f["feature_id"]: f for f in feats}
        out |= {"model": s.get("model"), "target_date": s.get("target_date", ""), "initiative": s.get("initiative", ""),
                "notes": s.get("notes", ""), "evidence": s.get("evidence", ""), "approved_by": s.get("approved_by", ""),
                "dropped": s.get("dropped", []), "flags": s.get("flags", {}),
                "features": [by_id.get(fid, {"feature_id": fid, "title": fid, "status": "unknown"})
                             for fid in s.get("features", [])]}
    return out


def product_roadmap(engine, product_id: str) -> dict:
    try:
        record = read_roadmap(engine.services, product_id)
        error = ""
    except Exception as exc:
        record, error = {"repo": "", "sha": "", "goals": [], "initiatives": [], "releases": []}, f"{type(exc).__name__}: {exc}"
    feats = engine.feature_summaries(product_id)
    running = {i["id"]: i for i in engine.initiative_summaries(product_id)}
    initiatives = []
    for i in record["initiatives"]:
        initiatives.append({**i, "record": "merged", **({"status": running[i["id"]]["status"]} if i["id"] in running
                                                         and running[i["id"]]["status"] == "dropped" else {})})
    for iid, i in running.items():
        if iid not in {x["id"] for x in initiatives}:
            initiatives.append({**i, "record": "pull request" if i["status"] == "approved" else "",
                                "measure": "", "scope": "product"})
    for i in initiatives:
        linked = [f for f in feats if f.get("initiative_id") == i["id"]]
        i["features"] = linked
        i["spent_usd"] = round(sum(f["cost_usd"] for f in linked), 2)
        i["progress"] = {"released": sum(_live(f) for f in linked), "total": len(linked),
                         "planned": len(i.get("candidates") or [])}
    releases = {r["id"]: {**r, "record": "merged"} for r in record["releases"]}
    for row in engine._rows(product_id, "release"):
        v = thread_view(engine, row["feature_id"])
        releases[row["feature_id"]] = {**releases.get(row["feature_id"], {}), "id": row["feature_id"],
                                       "title": v["title"], "model": v["model"], "status": v["status"],
                                       "target_date": v["target_date"], "features": [f["feature_id"] for f in v["features"]],
                                       "initiative": v["initiative"],
                                       "record": releases.get(row["feature_id"], {}).get("record", "pull request")}
    from ..outcomes.readouts import product_outcomes

    outcomes = product_outcomes(engine, product_id, {"initiatives": initiatives})
    for i in initiatives:
        i["outcome"] = outcomes["initiatives"].get(i["id"])
    return {"product_id": product_id, "record": {"repo": record["repo"], "sha": record["sha"], "error": error},
            "goals": [{**g, "outcome": outcomes["goals"].get(g["id"])} for g in record["goals"]],
            "outcomes": {"wanted": outcomes["wanted"], "windows": outcomes["windows"],
                         "readouts": outcomes["features"], "min_cell": outcomes.get("min_cell")},
            "initiatives": initiatives, "releases": list(releases.values()),
            "unplanned": [f for f in feats if not f.get("initiative_id") and f["status"] == "in_progress"]}

"""Weekly drift evals (T-77): the live config replayed on real cases with no config change behind
it, to catch what changes underneath the tool: a provider updating a model behind the same name,
or prompts crowding as products' knowledge grows.

The change gate (evals.ConfigGate) compares two configs on the same cases. Here the config stays
the same and time passes, so two sets are replayed, each answering one question:

  anchor  The same pinned cases every week (`cases_per_phase` per phase, labelled, of the data
          classes evals may send out), first run when the config last changed: that run is the
          baseline. Same inputs and same config, so a drop is the model or the provider moving.
          A new config pins a new anchor and baseline: the change gate already judged it.
  recent  The last week's labelled cases, compared with the anchor's baseline. Different cases,
          so it's a looser signal: a drop here and not on the anchor points at the work (bigger
          contexts, new kinds of product), not the model.

Both use the change gate's scoring and thresholds (evals.compare: pass rate, must-have rate,
critic agreement, wrong actions, injections followed). Cases are replayed with stand-ins for
everything with side effects, within `VALXEL_DRIFT_BUDGET_USD` (default 10) per run; coder and
reviewer cases run only in the eval runner, as for the change gate.

The watcher's `drift_evals` job runs it daily but replays only once `every_days` (7) have passed.
The latest result and the last 12 are in the cluster's notes (`drift:latest`, `drift:runs`), shown
at /api/evals/drift and in the tool's health; a drop goes to VALXEL_NOTIFY_WEBHOOK, and so does
its recovery.
"""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime, timedelta
from typing import Any

log = logging.getLogger(__name__)
ANCHOR = "drift:anchor"
LATEST = "drift:latest"
RUNS = "drift:runs"
KEEP_RUNS = 12


def _env(env=None) -> dict:
    env = env if env is not None else os.environ
    return {"every_days": int(env.get("VALXEL_DRIFT_EVERY_DAYS", "7")),
            "cases_per_phase": int(env.get("VALXEL_DRIFT_CASES_PER_PHASE", env.get("VALXEL_EVAL_CASES_PER_PHASE", "5"))),
            "budget_usd": float(env.get("VALXEL_DRIFT_BUDGET_USD", "10")),
            "max_drop": float(env.get("VALXEL_EVAL_MAX_DROP", "0.05")),
            "data_classes": tuple(c for c in env.get("VALXEL_EVAL_DATA_CLASSES", "general").split(",") if c)}


def due(cluster, *, now: datetime, every_days: int) -> bool:
    last = (cluster.get(LATEST) or {}).get("at")
    if not last:
        return True
    return now - datetime.fromisoformat(last) >= timedelta(days=every_days) - timedelta(hours=1)


def recent_cases(store, *, now: datetime, days: int, data_classes, per_phase: int) -> list:
    since = (now - timedelta(days=days)).isoformat()
    picked, seen = [], {}
    for c in store.cases(data_classes=data_classes):  # labelled, newest first
        if c.at < since:
            continue
        if seen.get(c.phase, 0) >= per_phase:
            continue
        seen[c.phase] = seen.get(c.phase, 0) + 1
        picked.append(c)
    return picked


def run(services: Any, cluster: Any, *, now: datetime | None = None, force: bool = False, alert=None,
        replay=None, env=None) -> dict | None:
    """One drift run, if it's due (or forced). Returns the result, or None when it isn't due."""
    from . import evals
    from .pilot import config_version

    now = now or datetime.now(UTC)
    cfg = _env(env)
    if not force and not due(cluster, now=now, every_days=cfg["every_days"]):
        return None
    replay = replay or (lambda cases: evals.run_cases(evals.services_for(services.settings), cases,
                                                      budget_usd=cfg["budget_usd"]))
    config = config_version(services.settings)
    anchor = cluster.get(ANCHOR) or {}
    store = services.evals
    if anchor.get("config") != config:  # a new config: pin a new anchor; its first run is the baseline
        cases = store.cases(data_classes=cfg["data_classes"], limit_per_phase=cfg["cases_per_phase"])
        anchor = {"config": config, "at": now.isoformat(), "case_ids": [c.id for c in cases], "baseline": None}
    by_id = {c.id: c for c in store.cases(data_classes=cfg["data_classes"])}
    anchored = [by_id[i] for i in anchor["case_ids"] if i in by_id]
    result: dict = {"at": now.isoformat(), "config": config, "anchor_cases": len(anchored), "regressions": [],
                    "anchor": None, "recent": None}
    if anchored:
        a = replay(anchored)
        result["anchor"] = a["summary"]
        if anchor["baseline"] is None:
            anchor["baseline"] = a["summary"]
            result["first_run_for_config"] = True
        else:
            cmp = evals.compare({"summary": anchor["baseline"]}, a, max_drop=cfg["max_drop"])
            result["anchor_comparison"] = cmp
            result["regressions"] += [f"Same cases, same config: {r}" for r in cmp["regressions"]]
    recent = recent_cases(store, now=now, days=cfg["every_days"], data_classes=cfg["data_classes"],
                          per_phase=cfg["cases_per_phase"])
    if recent and anchor.get("baseline"):
        r = replay(recent)
        result["recent"] = r["summary"]
        cmp = evals.compare({"summary": anchor["baseline"]}, r, max_drop=cfg["max_drop"] * 2)  # looser: other cases
        result["recent_comparison"] = cmp
        result["regressions"] += [f"This week's work: {x}" for x in cmp["regressions"] if "cases didn't run" not in x]
    result["recent_cases"] = len(recent)
    before = cluster.get(LATEST) or {}
    ledger = getattr(services, "ledger", None)
    if ledger is not None:  # what moved since the last run: setup, models, products (valxel_sdlc.what_changed)
        from . import what_changed as changes

        since = before.get("at") or (now - timedelta(days=cfg["every_days"])).isoformat()
        records = ledger.records(since=(datetime.fromisoformat(since) - timedelta(days=28)).isoformat())
        result["what_changed"] = changes.what_changed(services, cluster, records, since=since, until=now.isoformat())
        result["what_changed_summary"] = changes.summary(result["what_changed"])
    cluster.put(ANCHOR, anchor)
    cluster.put(LATEST, result)
    cluster.put(RUNS, ([{k: result[k] for k in ("at", "config", "regressions")}] + list(cluster.get(RUNS) or []))[:KEEP_RUNS])
    _say(result, before, alert)
    return result


def _say(result: dict, before: dict, alert) -> None:
    from .watcher_jobs import _webhook_alert

    say = alert or _webhook_alert
    try:
        if result["regressions"]:
            log.warning("drift evals: %s", "; ".join(result["regressions"]))
            say("Valxel drift evals: the agents did worse this week. " + "; ".join(result["regressions"][:5])
                + (f" {result['what_changed_summary']}" if result.get("what_changed_summary") else ""))
        elif before.get("regressions"):
            say("Valxel drift evals: back to the baseline.")
    except Exception:
        log.exception("drift alert not delivered")

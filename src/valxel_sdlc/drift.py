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
    _safe_mode(services, cluster, result, since=before.get("at") or now.isoformat(), at=now.isoformat(), alert=alert)
    return result


def phases_in(regressions: list[str]) -> list[str]:
    """The phases named in regression lines ("Same cases, same config: spec: pass rate fell ...")."""
    from .safe_mode import PHASES

    out = []
    for r in regressions:
        for part in r.split(": "):
            if part in PHASES and part not in out:
                out.append(part)
    return out


def _safe_mode(services, cluster, result: dict, *, since: str, at: str, alert=None) -> None:
    """Same-config drift moves a role every product uses: they all go to safe mode. A clean run is a
    normal check for the products already in it."""
    from . import safe_mode
    from .watcher_jobs import _webhook_alert

    products = list(getattr(services.settings, "registry", None) or [])
    if result["regressions"]:
        named = phases_in(result["regressions"])
        causes = ([{"source": "drift", "rule": "drift", "scope": p, "says": f"Drift evals: {p} did worse"} for p in named]
                  or [{"source": "drift", "rule": "drift", "scope": "", "says": "Drift evals: the agents did worse"}])
        for pid in products:
            safe_mode.record_check(cluster, pid, source="drift", normal=False, at=at, says="; ".join(result["regressions"][:3]))
        entered = safe_mode.enter(cluster, products, causes, at=at, bad_since=since)
        if entered:
            try:
                (alert or _webhook_alert)(f"Valxel safe mode on for {', '.join(entered)}: the drift evals did worse with "
                                          "no config change, so every gate goes to a person, read in full.")
            except Exception:
                log.exception("safe mode alert not delivered")
    elif not result.get("first_run_for_config") and result.get("anchor"):
        for pid in products:
            safe_mode.record_check(cluster, pid, source="drift", normal=True, at=at,
                                   says=f"Weekly drift run back at its baseline ({result['anchor_cases']} pinned cases)")


REPLAYS = "drift:replays"


def replay_scoped(services: Any, cluster: Any, product_id: str, phases: list[str] | None = None, *,
                  now: datetime | None = None, replay=None, env=None) -> dict:
    """A replay on demand for one product (T-87): the pinned cases of these phases, and the product's
    own labelled cases from the last `every_days`, each phase against the anchor's baseline for it.
    Doesn't move the anchor or the weekly schedule. For a product in safe mode, it's a check."""
    from . import evals, safe_mode

    now = now or datetime.now(UTC)
    cfg = _env(env)
    replay = replay or (lambda cases: evals.run_cases(evals.services_for(services.settings), cases,
                                                      budget_usd=cfg["budget_usd"]))
    sm = safe_mode.get(cluster, product_id)
    phases = list(phases or (sm or {}).get("phases") or [])
    anchor = cluster.get(ANCHOR) or {}
    result: dict = {"at": now.isoformat(), "product_id": product_id, "phases": phases or "all", "regressions": [],
                    "cases": 0}
    if not anchor.get("baseline"):
        result["says"] = "No drift baseline yet: run the drift evals once first"
        normal = None
    else:
        def wanted(c) -> bool:
            return not phases or c.phase in phases

        by_id = {c.id: c for c in services.evals.cases(data_classes=cfg["data_classes"])}
        pinned = [by_id[i] for i in anchor["case_ids"] if i in by_id and wanted(by_id[i])]
        own = [c for c in recent_cases(services.evals, now=now, days=cfg["every_days"], data_classes=cfg["data_classes"],
                                       per_phase=cfg["cases_per_phase"]) if c.product_id == product_id and wanted(c)
               and c.id not in set(anchor["case_ids"])]
        # Each phase against its own baseline: the baseline's all-phase totals don't compare with a subset.
        base = {"summary": {"all": None, "phases": anchor["baseline"].get("phases", {})}}
        for name, cases, drop in (("Pinned cases", pinned, cfg["max_drop"]), (f"{product_id}'s recent work", own,
                                                                              cfg["max_drop"] * 2)):
            if not cases:
                continue
            got = replay(cases)
            result["cases"] += len(cases)
            cmp = evals.compare(base, got, max_drop=drop)
            result[name] = got["summary"].get("phases")
            result["regressions"] += [f"{name}: {r}" for r in cmp["regressions"]]
        normal = None if not result["cases"] else not result["regressions"]
        result["says"] = ("No cases for these phases yet" if normal is None else
                          "; ".join(result["regressions"][:3]) if result["regressions"] else
                          f"Back at the baseline on {result['cases']} cases")
    result["normal"] = normal
    cluster.put(REPLAYS, ([result] + list(cluster.get(REPLAYS) or []))[:KEEP_RUNS])
    safe_mode.record_check(cluster, product_id, source="replay", normal=normal, at=now.isoformat(), says=result["says"])
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

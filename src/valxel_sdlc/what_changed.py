"""What changed around a health or drift alert (T-85): the setup, the models, or a product.

Drift has three usual causes: the tool's own setup changed, a model changed underneath it, or a
product changed what the agents read (its governance: constitution, context and approved lessons;
products can't edit the prompts, but lessons go into them as instructions). This says which of
them moved in an alert's window, from what's already recorded:

  setup     config versions (pilot.config_version) that first appeared in the ledger in the window
  models    per role, the model the provider says answered (ledger `served_model`) in the window
            that never answered that role before it: a new snapshot behind the same name, or a
            fallback taking over
  products  per product, governance commits first seen in the window, lessons added or removed,
            and prompt sections that grew (ledger `layer_tokens`, the window's average against the
            time before it: L3 constitution, L4 product context, L5 feature context, L6 the task,
            which carries the lessons)

Governance is observed by the hourly health job (`observe`): each product's governance commit and
its lessons, kept in the cluster's notes (`observed:governance:<product>`, the last 50 changes),
so a change is dated even when no feature has run since.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from typing import Any

log = logging.getLogger(__name__)

KEEP = 50
GROWTH = 0.25  # a prompt section 25% bigger than before...
GROWTH_MIN_TOKENS = 500  # ...and by at least this much, counts as grown
SECTIONS = {"L3": "constitution", "L4": "product context", "L5": "feature context", "L6": "task and lessons"}


def note_key(product_id: str) -> str:
    return f"observed:governance:{product_id}"


def observe(services: Any, cluster: Any, *, at: str) -> None:
    """Record each product's governance commit and lessons when they change."""
    for pid in sorted(services.settings.registry):
        try:
            snap = services.snapshot(pid)
        except Exception as exc:  # noqa: BLE001 - a product whose governance doesn't load is reported elsewhere
            log.info("what changed: governance for %s didn't load: %s", pid, exc)
            continue
        sha = ((snap.pins or {}).get("product") or {}).get("sha", "")
        lessons = sorted(lesson.id for lesson in snap.lessons or ())
        seen = list(cluster.get(note_key(pid)) or [])
        if seen and seen[-1]["sha"] == sha and seen[-1]["lessons"] == lessons:
            continue
        seen.append({"sha": sha, "lessons": lessons, "at": at})
        cluster.put(note_key(pid), seen[-KEEP:])


def _governance(cluster: Any, pid: str, since: str, until: str) -> dict:
    seen = list(cluster.get(note_key(pid)) or [])
    before = [s for s in seen if s["at"] < since]
    during = [s for s in seen if since <= s["at"] < until]
    if not during or not before:  # nothing new, or no earlier state to compare with
        return {}
    old, new = set(before[-1]["lessons"]), set(during[-1]["lessons"])
    return {"commits": [{"sha": s["sha"][:12], "at": s["at"]} for s in during],
            "lessons_added": sorted(new - old), "lessons_removed": sorted(old - new)}


def what_changed(services: Any, cluster: Any, records: list, *, since: str, until: str,
                 products: list[str] | None = None) -> dict:
    """What moved in [since, until), against the records before `since`."""
    before = [r for r in records if r.ts < since]
    during = [r for r in records if since <= r.ts < until]

    first_seen: dict[str, str] = {}
    for r in sorted(records, key=lambda r: r.ts):
        if r.config_version and r.config_version not in first_seen:
            first_seen[r.config_version] = r.ts
    setup = [{"config": c, "first_seen": t} for c, t in sorted(first_seen.items(), key=lambda x: x[1])
             if since <= t < until and any(r.ts < since for r in records)]

    answered_before: dict[str, set] = defaultdict(set)
    answered_now: dict[str, dict] = defaultdict(dict)
    for r in before:
        if r.served_model:
            answered_before[r.role].add(r.served_model)
    for r in during:
        if r.served_model:
            answered_now[r.role].setdefault(r.served_model, r.ts)
    models = []
    for role, now in sorted(answered_now.items()):
        old = answered_before.get(role)
        if not old:
            continue  # nothing to compare with
        for m, t in sorted(now.items(), key=lambda x: x[1]):
            if m not in old:
                models.append({"role": role, "answered": m, "before": sorted(old), "first_seen": t})

    pids = products if products is not None else sorted({r.product_id for r in during if r.product_id})
    prods: dict[str, dict] = {}
    for pid in pids:
        entry: dict = {}
        gov = _governance(cluster, pid, since, until)
        if gov:
            entry["governance"] = gov
        grown = _growth([r for r in before if r.product_id == pid], [r for r in during if r.product_id == pid])
        if grown:
            entry["prompt_growth"] = grown
        if entry:
            prods[pid] = entry
    return {"since": since, "until": until, "setup": setup, "models": models, "products": prods}


def _growth(before: list, during: list) -> dict:
    out = {}
    for layer, name in SECTIONS.items():
        b = [int((r.layer_tokens or {}).get(layer) or 0) for r in before if (r.layer_tokens or {}).get(layer)]
        d = [int((r.layer_tokens or {}).get(layer) or 0) for r in during if (r.layer_tokens or {}).get(layer)]
        if not b or not d:
            continue
        old, new = sum(b) / len(b), sum(d) / len(d)
        if new - old >= GROWTH_MIN_TOKENS and new >= old * (1 + GROWTH):
            out[name] = {"before": round(old), "now": round(new), "change": round(new / old - 1, 2)}
    return out


def summary(changed: dict, limit: int = 4) -> str:
    """One line for an alert: the likely cause first, or that none of the three moved."""
    parts = []
    for s in changed.get("setup") or []:
        parts.append(f"setup {s['config'][:12]} went live {s['first_seen'][:10]}")
    for m in changed.get("models") or []:
        parts.append(f"{m['role']} answered by {m['answered']} (before: {', '.join(m['before'])})")
    for pid, p in (changed.get("products") or {}).items():
        g = p.get("governance") or {}
        if g.get("lessons_added"):
            parts.append(f"{pid} added lessons {', '.join(g['lessons_added'])}")
        elif g.get("commits"):
            parts.append(f"{pid} governance changed ({g['commits'][-1]['sha']})")
        for name, x in (p.get("prompt_growth") or {}).items():
            parts.append(f"{pid} {name} grew {x['change']:.0%} ({x['before']} to {x['now']} tokens)")
    if not parts:
        return ("What changed: no setup, model or product change in the window: suspect the provider's serving "
                "(replay the pinned cases; use the fallback model if it holds).")
    more = f"; and {len(parts) - limit} more" if len(parts) > limit else ""
    return "What changed: " + "; ".join(parts[:limit]) + more + "."

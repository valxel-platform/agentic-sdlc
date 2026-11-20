"""Safe mode (T-86): when the tool's health or its drift evals say the agents are doing worse, a
product's gates all go to people, read in full, until it's shown to be back to normal.

While a product is in safe mode:

  - every gate goes to a person: new work can't use the express profile (it runs as a feature),
    and every change goes through G5, whatever its risk tier
  - no quick reads: the gate's confidence from history is shown but doesn't say "quick read"
  - no batch approval, and a single approval needs the decision open at least MIN_READ_S
  - the tripped phase gets a second critic (the shadow critic for documents, the shadow reviewer
    for build), and its verdict is shown at the gate beside the critic's
  - every gate page says why safe mode is on, and the tool's health shows how long it has been on

Entering is automatic. The hourly health job measures each product on its own: a quality rule that
trips for a product puts that product in safe mode. A drift regression (the same cases with the
same config doing worse) puts every product in it, since they all use the roles that moved.

Leaving takes two normal checks in a row and then an owner's confirmation (VALXEL_EVAL_APPROVERS).
A check is one of:

  replay  a replay of the tripped phases (valxel_sdlc.drift.replay_scoped): the pinned cases, and
          the product's own recent ones. Checks every cause.
  health  the hourly health job on the product's work since the fix (`mark_fixed`), or since safe
          mode began when no fix is marked, and only once enough new work has been decided to
          measure it (each tripped rule's `min_samples`). Checks the health causes, not drift.
  drift   the weekly drift run: back to its baseline. Checks every cause.

A check that comes back not normal starts the count again. Leaving lists the approvals made while
things were going wrong (from when the bad window began to now), for people to look at again.

State is in the cluster's notes: `safe_mode:<product>` while it's on, and the last episodes in
`safe_mode:history:<product>`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

NEEDED = 2  # normal checks in a row before an owner may end it
PHASES = ("discovery", "spec", "design", "architecture", "plan", "build", "ci")
GATE_PHASE = {"g1": "discovery", "g2": "spec", "g3": "design", "g4": "architecture", "g5": "build", "g6": "release"}
KEEP_CHECKS = 20
KEEP_EPISODES = 20


def key(product_id: str) -> str:
    return f"safe_mode:{product_id}"


def history_key(product_id: str) -> str:
    return f"safe_mode:history:{product_id}"


def _now() -> str:
    return datetime.now(UTC).isoformat()


def get(cluster: Any, product_id: str) -> dict | None:
    """The product's safe mode, or None when it's off."""
    if cluster is None:
        return None
    s = cluster.get(key(product_id))
    return s if s and s.get("on") else None


def active(services: Any, product_id: str) -> dict | None:
    """Safe mode as the pipeline sees it (services.cluster is the engine's notes)."""
    return get(getattr(services, "cluster", None), product_id)


def phase_on(services: Any, product_id: str, phase: str) -> bool:
    """Whether this phase gets the second critic: safe mode is on and the phase tripped (or the
    cause doesn't name one: a drift regression across phases)."""
    s = active(services, product_id)
    return bool(s) and (not s.get("phases") or phase in s["phases"])


def enter(cluster: Any, product_ids: list[str], causes: list[dict], *, at: str | None = None,
          bad_since: str | None = None) -> list[str]:
    """Put products in safe mode, or add new causes to one already in it (which restarts the count).
    Each cause: {source: health|drift|manual, rule, scope, says}. Returns the products that came in now."""
    at = at or _now()
    entered = []
    for pid in product_ids:
        s = get(cluster, pid)
        if s is None:
            s = {"on": True, "product_id": pid, "since": at, "bad_since": bad_since or at, "causes": [],
                 "phases": [], "fix": None, "checks": [], "normal_checks": 0, "samples": 0}
            entered.append(pid)
        known = {(c["source"], c.get("rule"), c.get("scope")) for c in s["causes"]}
        new = [{**c, "at": at} for c in causes if (c["source"], c.get("rule"), c.get("scope")) not in known]
        if not new and pid not in entered:
            continue
        s["causes"] += new
        named = [phase_of(c) for c in s["causes"]]
        # A cause that doesn't name a phase (drift across phases): every phase gets the second critic.
        s["phases"] = [] if None in named else sorted(set(named))
        if bad_since and bad_since < s["bad_since"]:
            s["bad_since"] = bad_since
        s["normal_checks"], s["ready"] = 0, False
        cluster.put(key(pid), s)
    return entered


def phase_of(cause: dict) -> str | None:
    scope = cause.get("scope") or ""
    return scope if scope in PHASES else GATE_PHASE.get(scope)


def covers(source: str, s: dict) -> bool:
    """Whether a check of this kind can show the product is back to normal: the hourly health job
    looks at real work, so it can't vouch for a model that drifted on the pinned cases."""
    return source != "health" or all(c["source"] == "health" for c in s["causes"])


def record_check(cluster: Any, product_id: str, *, source: str, normal: bool | None, says: str,
                 at: str | None = None, samples: int | None = None) -> dict | None:
    """One check's result. `normal` None: nothing to judge yet (kept, but doesn't count either way)."""
    s = get(cluster, product_id)
    if s is None:
        return None
    at = at or _now()
    counted = normal is not None and covers(source, s)
    if normal is True and not covers(source, s):
        says += " (doesn't count: a health check can't show a model drift is over; replay the cases)"
    s["checks"] = (s["checks"] + [{"at": at, "source": source, "normal": normal, "counted": counted,
                                   "says": says}])[-KEEP_CHECKS:]
    if counted:
        s["normal_checks"] = s["normal_checks"] + 1 if normal else 0
    if samples is not None:
        s["samples"] = samples
    s["ready"] = s["normal_checks"] >= NEEDED
    cluster.put(key(product_id), s)
    return s


def mark_fixed(cluster: Any, product_id: str, *, by: str, note: str, at: str | None = None) -> dict:
    """Support says what they changed. Health checks look at the work since then, and the count
    starts again: only checks after the fix show it worked."""
    s = get(cluster, product_id)
    if s is None:
        raise LookupError(f"{product_id} isn't in safe mode")
    at = at or _now()
    s["fix"] = {"at": at, "by": by, "note": note}
    s["normal_checks"], s["ready"], s["samples"] = 0, False, 0
    cluster.put(key(product_id), s)
    return s


def leave(cluster: Any, product_id: str, *, by: str, approvals: list[dict] | None = None,
          at: str | None = None) -> dict:
    """An owner ends safe mode, once NEEDED normal checks came in a row."""
    s = get(cluster, product_id)
    if s is None:
        raise LookupError(f"{product_id} isn't in safe mode")
    if not s.get("ready"):
        raise ValueError(f"{product_id} needs {NEEDED} normal checks in a row first "
                         f"({s['normal_checks']} so far): replay the tripped phases, or wait for the health job")
    at = at or _now()
    episode = {**s, "on": False, "left_at": at, "left_by": by, "approvals_to_review": approvals or []}
    cluster.put(history_key(product_id), ([episode] + list(cluster.get(history_key(product_id)) or []))[:KEEP_EPISODES])
    cluster.put(key(product_id), {"on": False, "product_id": product_id, "left_at": at, "left_by": by})
    return episode


def approvals_in_window(services: Any, product_id: str, since: str, until: str | None = None) -> list[dict]:
    """Gate approvals made while things were going wrong: each gate's first decision, approved."""
    store = getattr(services, "gate_outcomes", None)
    if store is None:
        return []
    until = until or _now()
    return [{"feature_id": r["feature_id"], "gate": r["gate"], "at": r["at"]}
            for r in store.rows(product_id) if r["decision"] == "approve" and since <= str(r.get("at") or "") <= until]


def hours_on(s: dict, now: str | None = None) -> float:
    start = datetime.fromisoformat(s["since"])
    end = datetime.fromisoformat(now or _now())
    return round((end - start).total_seconds() / 3600, 1)


def banner(s: dict) -> dict:
    """What a gate page shows: why, since when, and what it takes to end."""
    causes = "; ".join(c["says"] for c in s["causes"][:3]) + (f"; and {len(s['causes']) - 3} more" if len(s["causes"]) > 3 else "")
    return {"on": True, "since": s["since"], "causes": s["causes"], "phases": s["phases"],
            "normal_checks": s["normal_checks"], "needed": NEEDED, "fix": s.get("fix"),
            "says": (f"Safe mode since {s['since'][:16].replace('T', ' ')} UTC: {causes}. Every gate goes to a "
                     "person and is read in full: no express, no batch, no quick reads, G5 for every change.")}


def overview(services: Any, cluster: Any, products, now: str | None = None) -> dict:
    """Every product in safe mode: how long, why, the checks so far, and approvals to look at again."""
    now = now or _now()
    out = {}
    for pid in products:
        s = get(cluster, pid)
        if s is None:
            continue
        out[pid] = {**banner(s), "hours": hours_on(s, now), "checks": s["checks"][-5:], "ready": s.get("ready", False),
                    "approvals_to_review": approvals_in_window(services, pid, s["bad_since"], now)}
    return out

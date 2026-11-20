"""The tool's own health (T-76): whether its agents are working as intended on real work.

Product metrics say whether a feature moved its goal. These say whether the tool did its job
getting there, for every phase and gate, from what the pipeline already records (each feature's
events and decisions, and the usage ledger). Nothing new is collected:

  critic_false_pass    the critic passed a document people then sent back or took over
  critic_false_fail    the critic kept failing a document people accepted as it was
  first_time_approval  a gate's first decision was an approval
  escalation_rate      a phase's run ended with a person (loop cap, budget, stall, design issue)
  stall_rate           a phase's run stopped for making no progress (valxel_sdlc.loops)
  reopened             build sent an approved spec, design or architecture back (graph/gates.reopen)
  escalation reasons   why phases escalated, grouped (the cap, a stall by pattern, budget, design,
                       CI)
  cost and time        model spend and agents' minutes per phase and role; spend per feature
  stuck                runs queued too long; features waiting on CI, a deploy or another system
                       (not a person) too long

`measure` counts one window. The watcher's `tool_health` job (hourly) measures the last
`window_days` and the `baseline_days` before it, applies config/health.yaml's rules, keeps the
result in the cluster's notes (`health:latest`, read by /api/reports/health) and posts each
tripped rule to VALXEL_NOTIFY_WEBHOOK: once, then daily while it stays tripped, and once when it
clears.
"""

from __future__ import annotations

import logging
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger(__name__)
REALERT_S = 24 * 3600.0
NOTE = "health:latest"
ALERTED = "health:alerted"


@dataclass
class Rules:
    window_days: int = 7
    baseline_days: int = 28
    rules: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def load(cls, config_dir: Path | str) -> Rules:
        from .config import config_file

        raw = yaml.safe_load(config_file(config_dir, "health.yaml").read_text()) or {}
        return cls(int(raw.get("window_days", 7)), int(raw.get("baseline_days", 28)), dict(raw.get("rules") or {}))


def _ratio(n: int, d: int) -> float | None:
    return round(n / d, 4) if d else None


def _in(at: str, since: str, until: str) -> bool:
    return bool(at) and since <= at < until


def escalation_kind(reason: str) -> str:
    """The escalation's reason, grouped: what to look at first."""
    r = (reason or "").lower()
    if r.startswith("no progress"):
        pattern = r.split("(", 1)[1].split(",", 1)[0] if "(" in r else "stall"
        return f"stall:{pattern.strip()}"
    if "needs changing" in r:
        return "design_issue"
    if "evaluator loops failed" in r:
        return "loop_cap"
    if "budget" in r:
        return "budget"
    if "ci" in r.split() or "checks failed" in r:
        return "ci"
    return "other"


def measure(states: list[dict], records: list, *, since: str, until: str) -> dict:
    """The tool's numbers for one window [since, until), from feature states and ledger records."""
    from .pilot import critic_outcomes

    runs: dict[str, Counter] = defaultdict(Counter)  # phase -> passed / escalated / stalled
    loops: dict[str, list[int]] = defaultdict(list)
    reasons: Counter = Counter()
    stalls: Counter = Counter()
    reopened: Counter = Counter()
    first: dict[str, Counter] = defaultdict(Counter)  # gate -> approve / other
    critic: dict[str, Counter] = defaultdict(Counter)  # phase -> outcome
    features = set()
    for s in states:
        fid = s.get("feature_id", "")
        events = s.get("events") or []
        for e in events:
            if not _in(e.get("at", ""), since, until):
                continue
            node, kind = e.get("node", ""), e.get("kind", "")
            features.add(fid)
            if kind in ("passed", "escalated") and e.get("attempts") is not None:
                runs[node][kind] += 1
                loops[node].append(int(e.get("attempts") or 0))
                if e.get("stall"):
                    runs[node]["stalled"] += 1
                    stalls[f"{node}:{e['stall']['pattern']}:{e['stall']['between']}"] += 1
            if kind == "escalated":  # a phase's run, or a budget stop before it started
                reasons[f"{node}:{escalation_kind(e.get('detail', ''))}"] += 1
            if node == "ci_wait" and kind == "failed" and e.get("stall"):
                runs["ci"]["stalled"] += 1
                stalls[f"ci:{e['stall']['pattern']}:{e['stall']['between']}"] += 1
                reasons[f"build:{escalation_kind(e.get('detail', ''))}"] += 1
            if kind == "reopened":
                reopened[e.get("to", "")] += 1
        seen: set[str] = set()
        for d in sorted((d for d in s.get("gate_decisions") or [] if d.get("at")), key=lambda d: d["at"]):
            g = d.get("gate", "")
            if not g.startswith("g") or g in seen:
                continue
            seen.add(g)
            if _in(d["at"], since, until):
                first[g]["approve" if d.get("decision") == "approve" else "other"] += 1
        for o in critic_outcomes(s):
            if _in(o.get("at", ""), since, until):
                critic[o["phase"]][o["outcome"]] += 1

    cost: dict[str, float] = defaultdict(float)
    minutes: dict[str, float] = defaultdict(float)
    per_feature: dict[str, float] = defaultdict(float)
    for r in records:
        if not _in(getattr(r, "ts", ""), since, until) or str(getattr(r, "role", "")).endswith("_shadow"):
            continue
        key = f"{r.phase}/{r.role}"
        cost[key] += float(r.est_cost_usd or 0.0)
        minutes[key] += float(getattr(r, "duration_s", 0.0) or 0.0) / 60.0
        if r.feature_id:
            per_feature[r.feature_id] += float(r.est_cost_usd or 0.0)

    phases = {}
    for p, c in sorted(runs.items()):
        total = c["passed"] + c["escalated"]
        ls = sorted(loops[p])
        phases[p] = {"runs": total, "escalation_rate": _ratio(c["escalated"], total),
                     "stall_rate": _ratio(c["stalled"], total) if p != "ci" else None, "stalls": c["stalled"],
                     "loops_mean": round(sum(ls) / len(ls), 2) if ls else None,
                     "loops_p90": ls[min(len(ls) - 1, int(len(ls) * 0.9))] if ls else None}
    critics = {}
    for p, c in sorted(critic.items()):
        passed = c["passed_then_approved"] + c["passed_then_revised"]
        failed = c["failed_then_fixed"] + c["failed_then_vouched"]
        critics[p] = {"passed": passed, "false_pass": _ratio(c["passed_then_revised"], passed),
                      "failed": failed, "false_fail": _ratio(c["failed_then_vouched"], failed)}
    gates = {g: {"decided": c["approve"] + c["other"], "first_time_approval": _ratio(c["approve"], c["approve"] + c["other"])}
             for g, c in sorted(first.items())}
    spend = sorted(per_feature.values())
    return {
        "since": since, "until": until, "features": len(features),
        "phases": phases, "critic": critics, "gates": gates,
        "escalation_reasons": dict(reasons.most_common()), "stalls": dict(stalls.most_common()),
        "reopened": dict(reopened),
        "cost_by_phase_role": {k: round(v, 4) for k, v in sorted(cost.items())},
        "agent_minutes_by_phase_role": {k: round(v, 2) for k, v in sorted(minutes.items())},
        "cost_per_feature": {"features": len(spend), "mean": round(sum(spend) / len(spend), 4) if spend else None,
                             "max": round(spend[-1], 4) if spend else None},
    }


# --------------------------------------------------------------------------- rules


def _trip(rule: dict, now: float | None, base: float | None, n: int, base_n: int, *, higher_is_worse: bool) -> bool:
    if now is None or n < int(rule.get("min_samples", 1)):
        return False
    if higher_is_worse:
        if now < float(rule.get("at_least", 0.0)):
            return False
        if base is not None and base_n >= int(rule.get("min_samples", 1)):
            return now >= base + float(rule.get("by", 0.0))
        return True
    if now > float(rule.get("at_most", 1.0)):
        return False
    if base is not None and base_n >= int(rule.get("min_samples", 1)):
        return now <= base - float(rule.get("by", 0.0))
    return True


def check(current: dict, baseline: dict, rules: dict, *, queue: dict | None = None,
          waiting: list[dict] | None = None, dead: list[dict] | None = None) -> list[dict]:
    """The rules that trip: [{key, rule, scope, value, baseline, says}]. `key` is stable while
    the same thing stays wrong, for alerting once."""
    out: list[dict] = []

    def add(rule: str, scope: str, value, base, says: str) -> None:
        out.append({"key": f"{rule}:{scope}", "rule": rule, "scope": scope, "value": value, "baseline": base,
                    "says": says})

    def pct(x) -> str:
        return "n/a" if x is None else f"{x:.0%}"

    for phase, c in current.get("critic", {}).items():
        b = baseline.get("critic", {}).get(phase, {})
        r = rules.get("critic_false_pass") or {}
        if r and _trip(r, c["false_pass"], b.get("false_pass"), c["passed"], b.get("passed", 0), higher_is_worse=True):
            add("critic_false_pass", phase, c["false_pass"], b.get("false_pass"),
                f"The {phase} critic passed {pct(c['false_pass'])} of documents people then sent back "
                f"({c['passed']} passed; baseline {pct(b.get('false_pass'))})")
        r = rules.get("critic_false_fail") or {}
        if r and _trip(r, c["false_fail"], b.get("false_fail"), c["failed"], b.get("failed", 0), higher_is_worse=True):
            add("critic_false_fail", phase, c["false_fail"], b.get("false_fail"),
                f"The {phase} critic failed {pct(c['false_fail'])} of documents people accepted as they were "
                f"({c['failed']} failed; baseline {pct(b.get('false_fail'))})")
    for gate, c in current.get("gates", {}).items():
        b = baseline.get("gates", {}).get(gate, {})
        r = rules.get("first_time_approval") or {}
        if r and _trip(r, c["first_time_approval"], b.get("first_time_approval"), c["decided"], b.get("decided", 0),
                       higher_is_worse=False):
            add("first_time_approval", gate, c["first_time_approval"], b.get("first_time_approval"),
                f"{gate.upper()} approved {pct(c['first_time_approval'])} first time ({c['decided']} decided; "
                f"baseline {pct(b.get('first_time_approval'))})")
    for phase, c in current.get("phases", {}).items():
        b = baseline.get("phases", {}).get(phase, {})
        for rule, what in (("escalation_rate", "escalated to a person"), ("stall_rate", "stopped for making no progress")):
            r = rules.get(rule) or {}
            if r and _trip(r, c.get(rule), b.get(rule), c["runs"], b.get("runs", 0), higher_is_worse=True):
                add(rule, phase, c[rule], b.get(rule),
                    f"{pct(c[rule])} of {phase} runs {what} ({c['runs']} runs; baseline {pct(b.get(rule))})")
    r = rules.get("cost_per_feature") or {}
    now_c, base_c = current.get("cost_per_feature", {}), baseline.get("cost_per_feature", {})
    if (r and now_c.get("mean") is not None and base_c.get("mean") and now_c["features"] >= int(r.get("min_samples", 1))
            and base_c["features"] >= int(r.get("min_samples", 1))
            and now_c["mean"] >= base_c["mean"] * float(r.get("ratio", 1.5))):
        add("cost_per_feature", "all", now_c["mean"], base_c["mean"],
            f"Model spend per feature is ${now_c['mean']:.2f}, {now_c['mean'] / base_c['mean']:.1f}x the baseline "
            f"(${base_c['mean']:.2f})")
    limit = rules.get("queue_oldest_minutes")
    if limit and queue and float(queue.get("oldest_s") or 0) / 60 >= float(limit) and int(queue.get("queued") or 0):
        add("queue_oldest_minutes", "run_queue", round(float(queue["oldest_s"]) / 60, 1), None,
            f"A run has been queued {float(queue['oldest_s']) / 60:.0f} minutes ({queue['queued']} queued, "
            f"{queue.get('running', 0)} running): is a worker up?")
    limit = rules.get("dead_letters")
    if limit and dead and len(dead) >= int(limit):
        add("dead_letters", "run_queue", len(dead), None,
            f"The queue gave up on {len(dead)} run(s) in the window after repeated worker deaths "
            f"(latest: {dead[0]['thread_id']}): those features wait for someone to resume them")
    limit = rules.get("external_wait_hours")
    for w in waiting or []:
        if limit and float(w["hours"]) >= float(limit):
            add("external_wait_hours", w["feature_id"], round(float(w["hours"]), 1), None,
                f"{w['feature_id']} has waited on {w['wait']} for {float(w['hours']):.0f} hours")
    return out


# --------------------------------------------------------------------------- the watcher's job


def _iso(t: datetime) -> str:
    return t.astimezone(UTC).isoformat()


def external_waits(engine, *, now: datetime) -> list[dict]:
    """Features waiting on another system (CI, a deploy, provisioning), and for how long."""
    out = []
    for fid in engine.waiting_on_external():
        for p in engine.pending(fid):
            if p.get("type") != "external" or not p.get("since"):
                continue
            try:
                since = datetime.fromisoformat(str(p["since"]))
            except ValueError:
                continue
            out.append({"feature_id": fid, "wait": p.get("wait", "another system"),
                        "hours": (now - since).total_seconds() / 3600})
    return out


def run(engine, *, now: datetime | None = None, alert=None, clock=None) -> dict:
    """Measure, check the rules, keep the result, alert on what tripped (and what cleared)."""
    import time

    now = now or datetime.now(UTC)
    clock = clock or time.time
    from . import what_changed as changes

    changes.observe(engine.services, engine.cluster, at=_iso(now))  # products' governance, as it changes
    rules = Rules.load(engine.services.settings.config_dir)
    until, since = now, now - timedelta(days=rules.window_days)
    b_since = since - timedelta(days=rules.baseline_days)
    states = []
    for pid in engine.services.settings.registry:
        for f in engine.feature_summaries(pid):
            s = engine._state(f["feature_id"])
            if any((e.get("at") or "") >= _iso(b_since) for e in s.get("events") or []):
                states.append(s)
    records = engine.services.ledger.records(since=_iso(b_since))
    current = measure(states, records, since=_iso(since), until=_iso(until))
    baseline = measure(states, records, since=_iso(b_since), until=_iso(since))
    queue = engine.queue.depth() if getattr(engine, "queue", None) is not None else None
    dead = dead_letters(engine.cluster, since=_iso(since), until=_iso(until))
    tripped = check(current, baseline, rules.rules, queue=queue, waiting=external_waits(engine, now=now), dead=dead)
    # The weekly drift evals' latest result (valxel_sdlc.drift), alerted by that job itself.
    drift = engine.cluster.get("drift:latest")
    changed = changes.what_changed(engine.services, engine.cluster, records, since=_iso(since), until=_iso(until))
    result = {"at": _iso(now), "current": current, "baseline": baseline, "queue": queue, "alerts": tripped,
              "drift": drift, "what_changed": changed, "dead_letters": {"count": len(dead), "recent": dead[:5]}}
    result["safe_mode"] = by_product(engine, states, records, rules, now=now, since=since, b_since=b_since,
                                     alert=alert)
    engine.cluster.put(NOTE, result)
    _alert(engine.cluster, tripped, alert=alert, clock=clock, context=changes.summary(changed))
    return result


def dead_letters(cluster, *, since: str, until: str) -> list[dict]:
    """Runs the queue gave up on in the window (run_queue.dead_letter), newest first."""
    from .run_queue import DEAD_LETTERS

    return [d for d in cluster.get(DEAD_LETTERS) or [] if since <= d["at"] < until]


def for_product(engine, product_id: str, *, now: datetime | None = None) -> dict:
    """One product's view of the tool's health, for its own people beside its business metrics: its
    agents on its work against its own baseline, what's alerting for it (quality and cost; stuck
    work and the queue are the tool's owners' to watch), what changed, and its safe mode."""
    from . import safe_mode
    from . import what_changed as changes

    now = now or datetime.now(UTC)
    rules = Rules.load(engine.services.settings.config_dir)
    until, since = now, now - timedelta(days=rules.window_days)
    b_since = since - timedelta(days=rules.baseline_days)
    states = [s for f in engine.feature_summaries(product_id) if (s := engine._state(f["feature_id"]))
              and any((e.get("at") or "") >= _iso(b_since) for e in s.get("events") or [])]
    records = [r for r in engine.services.ledger.records(since=_iso(b_since)) if r.product_id == product_id]
    current = measure(states, records, since=_iso(since), until=_iso(until))
    baseline = measure(states, records, since=_iso(b_since), until=_iso(since))
    mine = {k: v for k, v in rules.rules.items() if k in QUALITY or k == "cost_per_feature"}
    tripped = check(current, baseline, mine)
    changed = changes.what_changed(engine.services, engine.cluster, records, since=_iso(since), until=_iso(until),
                                   products=[product_id])
    sm = safe_mode.overview(engine.services, engine.cluster, [product_id], now=_iso(now)).get(product_id)
    return {"at": _iso(now), "product_id": product_id, "window_days": rules.window_days,
            "baseline_days": rules.baseline_days, "current": current, "baseline": baseline, "alerts": tripped,
            "what_changed": changed, "what_changed_summary": changes.summary(changed) if tripped else "",
            "safe_mode": sm}


# Rules about the quality of the agents' work: these put a product in safe mode. Cost and stuck
# work alert, but don't change how gates are decided.
QUALITY = ("critic_false_pass", "critic_false_fail", "first_time_approval", "escalation_rate", "stall_rate")


def samples(m: dict, cause: dict) -> int:
    """How many decisions or runs a tripped rule's scope has in a measurement."""
    rule, scope = cause.get("rule"), cause.get("scope")
    if rule == "critic_false_pass":
        return int((m.get("critic", {}).get(scope) or {}).get("passed") or 0)
    if rule == "critic_false_fail":
        return int((m.get("critic", {}).get(scope) or {}).get("failed") or 0)
    if rule == "first_time_approval":
        return int((m.get("gates", {}).get(scope) or {}).get("decided") or 0)
    return int((m.get("phases", {}).get(scope) or {}).get("runs") or 0)


def by_product(engine, states: list[dict], records: list, rules: Rules, *, now: datetime, since: datetime,
               b_since: datetime, alert=None) -> dict:
    """Each product measured on its own: a quality rule that trips puts it in safe mode
    (valxel_sdlc.safe_mode); one already in it gets a check on its work since the fix."""
    from . import safe_mode
    from .watcher_jobs import _webhook_alert

    quality = {k: v for k, v in rules.rules.items() if k in QUALITY}
    cluster, say = engine.cluster, alert or _webhook_alert
    for pid in engine.services.settings.registry:
        st = [s for s in states if s.get("product_id") == pid]
        rs = [r for r in records if getattr(r, "product_id", "") == pid]
        was = safe_mode.get(cluster, pid)
        if was is not None:
            _check_since_fix(cluster, was, st, rs, rules, quality, now=now)
        # After safe mode ends, only work since then can put the product back in: not the same bad week.
        left = (cluster.get(safe_mode.key(pid)) or {}).get("left_at") or ""
        current = measure(st, rs, since=max(_iso(since), left), until=_iso(now))
        tripped = check(current, measure(st, rs, since=_iso(b_since), until=_iso(since)), quality)
        if not tripped:
            continue
        causes = [{"source": "health", "rule": t["rule"], "scope": t["scope"], "says": t["says"]} for t in tripped]
        if safe_mode.enter(cluster, [pid], causes, at=_iso(now), bad_since=_iso(since)):
            try:
                say(f"Valxel safe mode on for {pid}: every gate goes to a person, read in full, until two checks "
                    f"in a row are normal and an owner confirms. Why: {'; '.join(c['says'] for c in causes[:3])}")
            except Exception:
                log.exception("safe mode alert not delivered")
    return safe_mode.overview(engine.services, cluster, engine.services.settings.registry, now=_iso(now))


def _check_since_fix(cluster, s: dict, states: list[dict], records: list, rules: Rules, quality: dict, *,
                     now: datetime) -> None:
    """The product's work since the fix (or since safe mode began), against its baseline before the
    bad window. Counted only once each tripped rule has its `min_samples` of new work, and only when
    there's more work than at the last check: the same evidence doesn't count twice."""
    from . import safe_mode

    start = (s.get("fix") or {}).get("at") or s["since"]
    causes = [c for c in s["causes"] if c["source"] == "health"]
    if not causes:
        return
    bad = datetime.fromisoformat(s["bad_since"])
    base = measure(states, records, since=_iso(bad - timedelta(days=rules.baseline_days)), until=_iso(bad))
    after = measure(states, records, since=start, until=_iso(now))
    n = {c["rule"] + ":" + c["scope"]: samples(after, c) for c in causes}
    short = [k for k, c in zip(n, causes, strict=True) if n[k] < int((quality.get(c["rule"]) or {}).get("min_samples", 1))]
    total = sum(n.values())
    if short:
        safe_mode.record_check(cluster, s["product_id"], source="health", normal=None, at=_iso(now),
                               says=f"Not enough work since {start[:16]} to measure yet ({', '.join(short)})")
        return
    if total <= int(s.get("samples") or 0):
        return  # nothing new since the last check
    again = {t["key"]: t for t in check(after, base, quality)}
    bad_now = [again[k]["says"] for k in n if k in again]
    safe_mode.record_check(cluster, s["product_id"], source="health", normal=not bad_now, at=_iso(now), samples=total,
                           says=("; ".join(bad_now) if bad_now else
                                 f"Back to its baseline on {total} decisions and runs since {start[:16]}"))


def _alert(cluster, tripped: list[dict], *, alert=None, clock, context: str = "") -> None:
    from .watcher_jobs import _webhook_alert

    say = alert or _webhook_alert
    sent: dict = dict(cluster.get(ALERTED) or {})
    now = clock()
    live = {t["key"] for t in tripped}
    for t in tripped:
        if t["key"] in sent and now - float(sent[t["key"]]) < REALERT_S:  # said within the day
            continue
        log.warning("tool health: %s", t["says"])
        try:
            say(f"Valxel tool health: {t['says']}" + (f" {context}" if context else ""))
        except Exception:
            log.exception("tool health alert not delivered")
            continue
        sent[t["key"]] = now
    for key in [k for k in sent if k not in live]:  # cleared: say so once, and forget it
        try:
            say(f"Valxel tool health: back to normal: {key.replace(':', ' for ', 1)}")
        except Exception:
            log.exception("tool health alert not delivered")
        sent.pop(key)
    cluster.put(ALERTED, sent)

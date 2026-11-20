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
          waiting: list[dict] | None = None) -> list[dict]:
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
    tripped = check(current, baseline, rules.rules, queue=queue, waiting=external_waits(engine, now=now))
    # The weekly drift evals' latest result (valxel_sdlc.drift), alerted by that job itself.
    drift = engine.cluster.get("drift:latest")
    result = {"at": _iso(now), "current": current, "baseline": baseline, "queue": queue, "alerts": tripped,
              "drift": drift}
    engine.cluster.put(NOTE, result)
    _alert(engine.cluster, tripped, alert=alert, clock=clock)
    return result


def _alert(cluster, tripped: list[dict], *, alert=None, clock) -> None:
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
            say(f"Valxel tool health: {t['says']}")
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

"""The pilot: what people and agents spent on each feature, and what the shadow modes would have
decided, so the tool can be judged against its goal (85% of the work by agents, 15% by people) on
real numbers.

People's effort comes from four places:
- decisions in the tool (gates, escalations, hand-backs): the minutes the web app times while the
  decision is open, and how long the question waited (asked -> decided);
- reviews and comments on the tool's pull requests, from GitHub webhooks, counted at a standard
  estimate each (ESTIMATED_MINUTES; marked as estimates in the report);
- documents people wrote themselves after taking a phase over (a takeover and its hand-back);
- incident decisions by on-call, in the product totals.
Agents' effort is the time their model calls and sessions took (the usage ledger's duration_s).

Human share = people's minutes / (people's minutes + agents' minutes). That compares a person's
time with an agent's, which isn't the same unit of work; the report shows the counts beside it
(decisions, first-time approvals, loops, escalations) so the share isn't read alone.
"""

from __future__ import annotations

import hashlib
import json
import threading
from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Protocol

# Standard estimates for effort the tool can count but not time.
ESTIMATED_MINUTES = {"pr_review": 10.0, "pr_comment": 2.0}
# Revise and reject reasons people pick, so repeated corrections can be found and fixed at source.
REVISE_REASONS = ("missing_requirement", "wrong_scope", "wrong_approach", "unclear", "quality", "format",
                  "compliance", "other")
TARGET_HUMAN_SHARE = 0.15
# Gates the shadow auto-approval looks at: G1 (worth doing) and G6 (production) stay with people.
SHADOW_GATES = ("g2", "g3", "g4")


def config_version(settings) -> str:
    """A short fingerprint of what shapes agents' work: role models and runtimes, and how the
    critic decides and retries revise. Every ledger row carries it, so results trace to settings."""
    roles = {k: [v.runtime, v.model] for k, v in sorted(settings.roles.items())}
    blob = json.dumps({"roles": roles, "critic": "checklist", "retry": "revise"}, sort_keys=True)
    return hashlib.sha1(blob.encode()).hexdigest()[:10]


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


class EffortStore(Protocol):
    def add(self, event: dict) -> None: ...
    def events(self, feature_id: str | None = None, product_id: str | None = None) -> list[dict]: ...
    def asked_at(self, feature_id: str, interrupt_id: str) -> str: ...


class InMemoryEffortStore:
    def __init__(self) -> None:
        self.rows: list[dict] = []
        self._lock = threading.Lock()

    def add(self, event: dict) -> None:
        with self._lock:
            if event.get("kind") == "asked" and any(
                    r["kind"] == "asked" and r.get("ref") == event.get("ref") for r in self.rows):
                return  # first time it was seen
            self.rows.append({"at": now(), "minutes": 0.0, "who": "", "ref": "", "detail": {}, **event})

    def events(self, feature_id=None, product_id=None) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self.rows if (feature_id is None or r.get("feature_id") == feature_id)
                    and (product_id is None or r.get("product_id") == product_id)]

    def asked_at(self, feature_id, interrupt_id) -> str:
        return next((r["at"] for r in self.events(feature_id) if r["kind"] == "asked" and r["ref"] == interrupt_id), "")


class PostgresEffortStore:
    DDL = (
        """CREATE TABLE IF NOT EXISTS vrixel_effort (
             id BIGSERIAL PRIMARY KEY, feature_id TEXT NOT NULL, product_id TEXT NOT NULL, kind TEXT NOT NULL,
             who TEXT NOT NULL DEFAULT '', ref TEXT NOT NULL DEFAULT '', minutes NUMERIC(10,2) NOT NULL DEFAULT 0,
             detail JSONB NOT NULL DEFAULT '{}', at TIMESTAMPTZ NOT NULL DEFAULT now())""",
        "CREATE INDEX IF NOT EXISTS vrixel_effort_feature ON vrixel_effort (feature_id)",
        "CREATE UNIQUE INDEX IF NOT EXISTS vrixel_effort_asked ON vrixel_effort (feature_id, ref) WHERE kind = 'asked'",
    )

    def __init__(self, pool) -> None:
        self.pool = pool
        with pool.connection() as conn:
            for stmt in self.DDL:
                conn.execute(stmt)

    def add(self, event: dict) -> None:
        with self.pool.connection() as conn:
            conn.execute(
                "INSERT INTO vrixel_effort (feature_id, product_id, kind, who, ref, minutes, detail) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb) ON CONFLICT DO NOTHING",
                (event["feature_id"], event.get("product_id", ""), event["kind"], event.get("who", ""),
                 event.get("ref", ""), float(event.get("minutes") or 0.0), json.dumps(event.get("detail") or {})))

    def events(self, feature_id=None, product_id=None) -> list[dict]:
        where, args = [], []
        if feature_id:
            where.append("feature_id = %s")
            args.append(feature_id)
        if product_id:
            where.append("product_id = %s")
            args.append(product_id)
        sql = "SELECT * FROM vrixel_effort" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY id"
        with self.pool.connection() as conn:
            rows = conn.execute(sql, tuple(args)).fetchall()
        return [{**dict(r), "minutes": float(r["minutes"]), "at": r["at"].isoformat()} for r in rows]

    def asked_at(self, feature_id, interrupt_id) -> str:
        with self.pool.connection() as conn:
            r = conn.execute("SELECT at FROM vrixel_effort WHERE feature_id = %s AND ref = %s AND kind = 'asked'",
                             (feature_id, interrupt_id)).fetchone()
        return r["at"].isoformat() if r else ""


def minutes_between(a: str, b: str) -> float:
    try:
        return max(0.0, (datetime.fromisoformat(b) - datetime.fromisoformat(a)).total_seconds() / 60.0)
    except (TypeError, ValueError):
        return 0.0


def shadow_auto_approval(state: dict, gate: str, phase: str, data_class: str) -> dict:
    """Whether a gate would have passed without a person, under the rule being trialled: general
    data, no shared or dependency lane touched, the phase passed its first try, and the critic's
    checklist met in full. Recorded beside the person's decision; nothing acts on it."""
    if gate not in SHADOW_GATES:
        return {}
    critic = next((e.get("critic") for e in reversed(state.get("events") or [])
                   if e.get("node") == phase and e.get("kind") == "passed"), None) or {}
    why = []
    if data_class != "general":
        why.append(f"{data_class} data")
    if state.get("lanes_touched"):
        why.append("touches " + ", ".join(state["lanes_touched"]))
    if int((state.get("loop_count") or {}).get(phase, 0)) > 1:
        why.append(f"{phase} needed {state['loop_count'][phase]} tries")
    if not critic.get("passed"):
        why.append("no passed checklist")
    elif any(not i.get("met") for i in critic.get("items") or []):
        why.append("checklist items not met: " + ", ".join(i["id"] for i in critic["items"] if not i.get("met")))
    return {"eligible": not why, "why": why}


def feature_report(engine, feature_id: str) -> dict:
    """One feature's numbers for the pilot report."""
    v = engine.view(feature_id)
    s = engine._state(feature_id)
    records = engine.services.ledger.records(feature_id)
    effort = engine.services.effort.events(feature_id) if getattr(engine.services, "effort", None) else []
    decisions = list(v.get("gate_decisions") or [])
    people = defaultdict(float)
    for d in decisions:
        kind = "escalation" if d["gate"].startswith("escalation") else "handback" if d["gate"] == "handback" else "gate"
        people[kind] += float(d.get("active_minutes") or 0.0)
    for e in effort:
        if e["kind"] in ESTIMATED_MINUTES:
            people[e["kind"]] += float(e.get("minutes") or ESTIMATED_MINUTES[e["kind"]])
    human = round(sum(people.values()), 2)
    agent = round(sum(float(getattr(r, "duration_s", 0.0) or 0.0) for r in records) / 60.0, 2)
    gates = [d for d in decisions if d["gate"].startswith("g")]
    first: dict[str, str] = {}
    for d in gates:
        first.setdefault(d["gate"], d["decision"])
    waited = [d["waited_minutes"] for d in decisions if d.get("waited_minutes") is not None]
    shadow = [d for d in gates if d.get("shadow_auto")]
    critics = [e["critic"] for e in v.get("events") or [] if e.get("critic")]
    shadow_critics = [c["shadow"] for c in critics if c.get("shadow")]
    reviews = [rv for e in v.get("events") or [] for rv in (e.get("reviews") or [])]
    cost_main = round(sum(r.est_cost_usd for r in records if not r.role.endswith("_shadow")), 4)
    return {
        "feature_id": feature_id, "title": v.get("title"), "status": v.get("status"), "pilot": bool(s.get("pilot")),
        "cost_usd": cost_main,
        "shadow_cost_usd": round(sum(r.est_cost_usd for r in records if r.role.endswith("_shadow")), 4),
        "cost_by_phase": _sum_by(records, "phase"), "cost_by_model": _sum_by(records, "model"),
        "cost_by_runtime": _sum_by(records, "runtime"),
        "cache_hit_rate": (v.get("usage") or {}).get("cache_hit_rate", 0.0),
        "config_versions": sorted({getattr(r, "config_version", "") for r in records} - {""}),
        "loops": dict(v.get("loop_count") or {}),
        "ci_retries": sum(1 for e in v.get("events") or [] if e.get("node") == "ci_wait" and e.get("kind") == "retry"),
        "escalations": sum(1 for d in decisions if d["gate"].startswith("escalation")),
        "decisions": len(decisions),
        "first_time_approvals": sum(1 for g in first.values() if g == "approve"),
        "gates": len(first),
        "revise_reasons": dict(Counter(d.get("reason") or "unspecified" for d in decisions if d["decision"] in ("revise", "abort", "retry"))),
        "pr_reviews": sum(1 for e in effort if e["kind"] == "pr_review"),
        "pr_comments": sum(1 for e in effort if e["kind"] == "pr_comment"),
        "human_minutes": human, "human_minutes_by_kind": {k: round(m, 2) for k, m in sorted(people.items())},
        "agent_minutes": agent,
        "human_share": round(human / (human + agent), 4) if human + agent else None,
        "decision_wait_minutes": round(sum(waited) / len(waited), 1) if waited else None,
        "shadow_auto_approval": {
            "gates": len(shadow), "eligible": sum(1 for d in shadow if d["shadow_auto"]["eligible"]),
            # Eligible and the person approved: time it would have saved. Eligible but not approved: a miss.
            "agreed": sum(1 for d in shadow if d["shadow_auto"]["eligible"] and d["decision"] == "approve"),
            "would_have_missed": sum(1 for d in shadow if d["shadow_auto"]["eligible"] and d["decision"] != "approve"),
            "minutes_saved": round(sum(float(d.get("active_minutes") or 0.0) for d in shadow
                                       if d["shadow_auto"]["eligible"] and d["decision"] == "approve"), 2)},
        "shadow_critic": {"compared": len(shadow_critics), "agreed": sum(1 for c in shadow_critics if c.get("agree"))},
        "shadow_reviewer": {"compared": sum(1 for r in reviews if r.get("shadow")),
                            "agreed": sum(1 for r in reviews if (r.get("shadow") or {}).get("agree"))},
        "old_score_agreed": {"compared": len(critics), "agreed": sum(1 for c in critics
                                                                     if c.get("legacy_passed") == c.get("passed"))},
    }


def _sum_by(records, attr: str) -> dict[str, float]:
    out: dict[str, float] = defaultdict(float)
    for r in records:
        out[getattr(r, attr)] += r.est_cost_usd
    return {k: round(v, 4) for k, v in sorted(out.items())}


def report(engine, product_id: str, *, pilot_only: bool = True, since: str = "") -> dict:
    """The pilot report for a product: each feature's numbers and the totals against the target."""
    rows = []
    for f in engine.feature_summaries(product_id):
        fid = f["feature_id"]
        s = engine._state(fid)
        if pilot_only and not s.get("pilot"):
            continue
        if since and (s.get("events") or [{}])[0].get("at", "") < since:
            continue
        rows.append(feature_report(engine, fid))
    human = sum(r["human_minutes"] for r in rows)
    agent = sum(r["agent_minutes"] for r in rows)
    effort = engine.services.effort.events(product_id=product_id) if getattr(engine.services, "effort", None) else []

    def total(key, sub):
        return sum(r[key][sub] for r in rows)

    gates = sum(r["gates"] for r in rows)
    return {
        "product_id": product_id, "features": rows, "count": len(rows),
        "done": sum(1 for r in rows if r["status"] == "done"),
        "totals": {
            "cost_usd": round(sum(r["cost_usd"] for r in rows), 4),
            "shadow_cost_usd": round(sum(r["shadow_cost_usd"] for r in rows), 4),
            "human_minutes": round(human, 2), "agent_minutes": round(agent, 2),
            "human_share": round(human / (human + agent), 4) if human + agent else None,
            "target_human_share": TARGET_HUMAN_SHARE,
            "first_time_approval_rate": round(sum(r["first_time_approvals"] for r in rows) / gates, 4) if gates else None,
            "escalations": sum(r["escalations"] for r in rows), "ci_retries": sum(r["ci_retries"] for r in rows),
            "revise_reasons": dict(sum((Counter(r["revise_reasons"]) for r in rows), Counter())),
            "shadow_auto_approval": {k: round(total("shadow_auto_approval", k), 2) for k in
                                     ("gates", "eligible", "agreed", "would_have_missed", "minutes_saved")},
            "shadow_critic": {k: total("shadow_critic", k) for k in ("compared", "agreed")},
            "shadow_reviewer": {k: total("shadow_reviewer", k) for k in ("compared", "agreed")},
            "old_score_agreed": {k: total("old_score_agreed", k) for k in ("compared", "agreed")},
            "incident_decisions": sum(1 for e in effort if e["kind"] == "incident_decision"),
        },
        "estimated_minutes": ESTIMATED_MINUTES,
    }


def markdown(rep: dict) -> str:
    t = rep["totals"]
    share = "n/a" if t["human_share"] is None else f"{t['human_share'] * 100:.1f}%"
    lines = [f"# Pilot report: {rep['product_id']}", "",
             f"{rep['count']} features, {rep['done']} released. Human share {share} "
             f"(target {t['target_human_share'] * 100:.0f}%): {t['human_minutes']:.0f} min people, "
             f"{t['agent_minutes']:.0f} min agents. Cost ${t['cost_usd']:.2f} (shadow runs ${t['shadow_cost_usd']:.2f}).", "",
             "| Feature | Status | Cost | Human min | Agent min | Share | First-time approvals | Loops | CI retries | Escalations |",
             "| --- | --- | ---: | ---: | ---: | ---: | --- | ---: | ---: | ---: |"]
    for r in rep["features"]:
        sh = "n/a" if r["human_share"] is None else f"{r['human_share'] * 100:.1f}%"
        lines.append(f"| {r['feature_id']} {r['title']} | {r['status']} | ${r['cost_usd']:.2f} | {r['human_minutes']:.0f} | "
                     f"{r['agent_minutes']:.0f} | {sh} | {r['first_time_approvals']}/{r['gates']} | "
                     f"{sum(r['loops'].values())} | {r['ci_retries']} | {r['escalations']} |")
    sa, sc, sr, old = t["shadow_auto_approval"], t["shadow_critic"], t["shadow_reviewer"], t["old_score_agreed"]
    lines += ["", "## Shadow modes", "",
              f"- Automatic approval at G2-G4: {sa['eligible']:.0f} of {sa['gates']:.0f} gates eligible; people approved "
              f"{sa['agreed']:.0f} of those (saving {sa['minutes_saved']:.0f} min) and didn't approve {sa['would_have_missed']:.0f}.",
              f"- Cheaper critic agreed with the critic on {sc['agreed']} of {sc['compared']} documents.",
              f"- Cheaper reviewer agreed with the reviewer on {sr['agreed']} of {sr['compared']} pull requests.",
              f"- The old 0-1 score would have decided the same as the checklist on {old['agreed']} of {old['compared']} documents.",
              "", "## Why people sent work back", ""]
    lines += [f"- {k}: {n}" for k, n in sorted(t["revise_reasons"].items(), key=lambda x: -x[1])] or ["- Nothing was sent back."]
    lines += ["", f"PR reviews count {ESTIMATED_MINUTES['pr_review']:.0f} min and PR comments "
              f"{ESTIMATED_MINUTES['pr_comment']:.0f} min each (estimates)."]
    return "\n".join(lines) + "\n"

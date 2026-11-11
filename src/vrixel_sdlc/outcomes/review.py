"""The initiative review (step 5 of the outcome loop): is the initiative's goal moving, and what
now? Raised on the initiative's review date (`review_by`, by default 90 days after approval) or as
soon as its goal reads off track twice in a row. The product council decides:

    continue   keep going; the next review date is set (90 days on unless they give one)
    change     open a feature intent under the initiative, with the readouts and their words in it
    stop       end the initiative and keep its features; for each feature whose removal is less
               effort than keeping it (still behind a flag that isn't fully on), the council may
               tick it to open a retire intent: remove the flag and the code, migrate the data,
               draft a customer notice

Every decision is recorded on the initiative's file in the governance repo, by pull request: its
status, its next review date and a line under '## Reviews'.

    prepare -> review (the council's gate) -> act -> end
"""

from __future__ import annotations

import operator
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Annotated, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from ..state import last

DECISIONS = ("continue", "change", "stop")
REVIEW_AFTER_DAYS = 90


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def event(node: str, kind: str, detail: str = "", **extra) -> dict:
    return {"at": now(), "node": node, "kind": kind, "detail": detail, **extra}


class ReviewState(TypedDict, total=False):
    review_id: str
    product_id: str
    initiative_id: str
    title: str
    why: str
    created_by: str
    evidence: Annotated[dict, last]
    preview: Annotated[str, last]
    status: Annotated[str, last]  # preparing | waiting | continued | changed | stopped
    decision: Annotated[dict, last]
    opened: Annotated[list[str], last]  # the intents the decision opened
    record_pr: Annotated[dict, last]
    gate_decisions: Annotated[list[dict], operator.add]
    events: Annotated[list[dict], operator.add]
    cost_usd: Annotated[float, operator.add]


def council(services, product_id: str) -> list[str]:
    """The product council (registry.yaml inception.council), else the product's G0 or G1 approvers."""
    c = list((services.settings.inception or {}).get("council") or [])
    if c:
        return c
    p = services.snapshot(product_id).product
    return list(p.approvers.get("g0") or p.approvers.get("g1") or [])


def review_date(initiative: dict) -> date | None:
    if initiative.get("review_by"):
        try:
            return date.fromisoformat(str(initiative["review_by"])[:10])
        except ValueError:
            return None
    if initiative.get("approved_at"):
        try:
            return date.fromisoformat(str(initiative["approved_at"])[:10]) + timedelta(days=REVIEW_AFTER_DAYS)
        except ValueError:
            return None
    return None


def _fmt(v, unit: str = "") -> str:
    from . import Reading

    return Reading("", "", "", "", "", "ok", value=v).shown(unit) if v is not None else "no data"


def evidence(services, product_id: str, initiative: dict) -> dict:
    """What the council sees: the goal's trend and status, each feature's readouts, spend against
    budget, and the features whose removal would be less effort than keeping them."""
    from .readouts import product_outcomes

    out = product_outcomes(SimpleNamespace(services=services), product_id, {"initiatives": [initiative]})
    gid = initiative.get("goal") or ""
    goal = out["goals"].get(gid) or {}
    status = (out["initiatives"].get(initiative["id"]) or {}).get("status", "no_data")
    feats = [f for f in (services.features(product_id) if callable(getattr(services, "features", None)) else [])
             if f.get("initiative_id") == initiative["id"]]
    retire = []
    for f in feats:
        if f["status"] != "released":
            continue
        s = services.feature_state(f["feature_id"]) if callable(getattr(services, "feature_state", None)) else {}
        rel = s.get("release") or {}
        pct = rel.get("flag_percent")
        if s.get("flag") and (rel.get("flag_on") is False or (pct is not None and pct < 100)):
            retire.append({"feature_id": f["feature_id"], "title": f["title"], "flag": s["flag"],
                           "why": f"still behind {s['flag']}, on for {pct if pct is not None else 0:g}% of users"})
    readouts = {f["feature_id"]: out["features"].get(f["feature_id"], []) for f in feats}
    spent = round(sum(f.get("cost_usd", 0.0) for f in feats), 2)
    return {"goal_id": gid, "goal": goal, "status": status, "features": feats, "readouts": readouts,
            "spent_usd": spent, "budget_usd": float(initiative.get("budget_usd") or 0), "retire": retire,
            "in_progress": [f["feature_id"] for f in feats if f["status"] == "in_progress"]}


def preview(initiative: dict, ev: dict, why: str) -> str:
    m = (ev["goal"] or {}).get("metric") or {}
    unit = m.get("unit", "")
    lines = ["## The goal", ""]
    if ev["goal"]:
        lines += [f"{ev['goal_id']}: **{ev['status'].replace('_', ' ')}** since approval. Latest "
                  f"{_fmt(ev['goal'].get('latest'), unit)}, baseline {_fmt(m.get('baseline'), unit)}, target "
                  f"{_fmt(m.get('target'), unit)}{' by ' + m['by'] if m.get('by') else ''}."]
        pts = [p for p in ev["goal"].get("trend") or [] if p.get("value") is not None][-6:]
        if pts:
            lines += ["", "Recent readings: " + ", ".join(f"{p['end'][:10]} {_fmt(p['value'], unit)}" for p in pts) + "."]
    else:
        lines += [f"{ev['goal_id'] or 'No goal'} isn't measured yet: add a metric to it in roadmap/goals.yaml."]
    lines += ["", "## Its features", ""]
    if not ev["features"]:
        lines += ["None started yet."]
    else:
        lines += ["| Feature | Status | Readouts |", "| --- | --- | --- |"]
        for f in ev["features"]:
            rs = ev["readouts"].get(f["feature_id"]) or []
            said = "; ".join(f"{r['window_days']} days: {'+' if (r['change'] or 0) > 0 else ''}{_fmt(r['change'], unit)}"
                             + (" toward" if r["toward"] else " away" if r["toward"] is False else "")
                             for r in rs if r.get("change") is not None) or "none yet"
            lines.append(f"| {f['feature_id']} {f['title']} | {f['status'].replace('_', ' ')} | {said} |")
    lines += ["", "## Spend", "", f"${ev['spent_usd']:,.2f} of ${ev['budget_usd']:,.2f}."]
    if ev["retire"]:
        lines += ["", "## If you stop it", "",
                  "Stopping keeps its features. These would be less effort to remove than to keep; tick the ones to retire:"]
        lines += [f"- {r['feature_id']} {r['title']}: {r['why']}" for r in ev["retire"]]
    if ev["in_progress"]:
        lines += ["", f"Still in progress: {', '.join(ev['in_progress'])}. They carry on unless stopped on their own pages."]
    lines += ["", "Readouts compare before and after release; other changes in the same period count too."]
    return "\n".join(lines) + "\n"


def initiative_of(services, product_id: str, initiative_id: str) -> dict:
    from ..roadmap import read_roadmap

    for i in read_roadmap(services, product_id)["initiatives"]:
        if i["id"] == initiative_id:
            return i
    raise KeyError(f"No initiative {initiative_id} in {product_id}'s roadmap")


def record(services, state: dict, initiative: dict, changes: dict, line: str) -> dict:
    from ..roadmap import governance_pr, read_roadmap
    from ..roadmap.record import update_initiative

    pid = state["product_id"]
    gov = services.settings.entry(pid).governance
    road = read_roadmap(services, pid)
    text = services.governance.source.read(gov.org, gov.repo, initiative["path"], road["sha"]) or ""
    new = update_initiative(text, changes, line)
    return governance_pr(services, pid, branch=f"vrixel/{state['review_id'].lower()}", files={initiative["path"]: new},
                         title=f"{state['review_id']}: review of {initiative['id']}",
                         body=(f"The product council's review of {initiative['id']} {initiative['title']}: {line}\n\n"
                               "Merge to record it on the roadmap."), labels=["vrixel", "review"])


def build_review_graph(services, checkpointer=None):
    def prepare(state: ReviewState) -> dict:
        i = initiative_of(services, state["product_id"], state["initiative_id"])
        ev = evidence(services, state["product_id"], i)
        return {"evidence": ev, "preview": preview(i, ev, state.get("why", "")), "status": "waiting",
                "events": [event("prepare", "ready", f"goal {ev['status'].replace('_', ' ')}")]}

    def review(state: ReviewState) -> dict:
        from ..graph.pipeline import normalize

        ev = state["evidence"]
        packet = {"type": "gate", "gate": "review", "kind": "review", "phase": "review", "feature_id": state["review_id"],
                  "product_id": state["product_id"], "title": state["title"], "preview": state["preview"],
                  "approvers": council(services, state["product_id"]), "allowed": list(DECISIONS),
                  "initiative_id": state["initiative_id"], "goal_status": ev["status"],
                  "summary": f"{state['initiative_id']}: why now: {state.get('why', '')}.",
                  "retire_candidates": ev["retire"], "cost_usd": 0.0,
                  "next_review": (date.today() + timedelta(days=REVIEW_AFTER_DAYS)).isoformat()}
        d = normalize(interrupt(packet), DECISIONS)
        rec = {"gate": "review", "at": now(), **d}
        return {"decision": d, "gate_decisions": [rec],
                "events": [event("review", d["decision"], d.get("feedback", ""), approver=d.get("approver", ""))]}

    def act(state: ReviewState) -> dict:
        d, ev = state["decision"], state["evidence"]
        i = initiative_of(services, state["product_id"], state["initiative_id"])
        who = d.get("approver", "")
        today = date.today().isoformat()
        words = f": {d['feedback']}" if d.get("feedback") else ""
        opened: list[str] = []
        create = getattr(services, "create_feature", None)
        if d["decision"] == "continue":
            nxt = str(d.get("review_by") or (date.today() + timedelta(days=REVIEW_AFTER_DAYS)).isoformat())[:10]
            date.fromisoformat(nxt)
            changes, line, status = {"review_by": nxt}, f"{today}: continue, next review {nxt} ({who}){words}", "continued"
        elif d["decision"] == "change":
            if callable(create):
                opened.append(create(product_id=state["product_id"], title=f"Change course: {i['title']}"[:150],
                                     description=(f"The product council's review of {i['id']} asked for a change"
                                                  f"{words or '.'}\n\n{state['preview']}"),
                                     created_by=who, background=True, initiative_id=i["id"],
                                     not_duplicate_reason=f"Opened by {state['review_id']}"))
            nxt = (date.today() + timedelta(days=REVIEW_AFTER_DAYS)).isoformat()
            changes = {"review_by": nxt}
            line, status = f"{today}: change course, opened {', '.join(opened) or 'an intent'} ({who}){words}", "changed"
        else:
            chosen = {str(x) for x in d.get("retire") or []}
            for r in ev["retire"]:
                if r["feature_id"] in chosen and callable(create):
                    opened.append(create(
                        product_id=state["product_id"], title=f"Retire {r['title']}"[:150], created_by=who, background=True,
                        not_duplicate_reason=f"Retires {r['feature_id']}, opened by {state['review_id']}",
                        description=(f"Retire {r['feature_id']} {r['title']}: the initiative {i['id']} was stopped at its "
                                     f"review ({state['review_id']}). Remove feature flag {r['flag']} and the code "
                                     "behind it, migrate or delete the data it kept, and draft a notice for the "
                                     "customers who used it.")))
            changes = {"status": "dropped"}
            line = (f"{today}: stopped ({who}){words}"
                    + (f"; retiring {', '.join(sorted(chosen & {r['feature_id'] for r in ev['retire']}))}" if chosen else ""))
            status = "stopped"
        try:
            pr = record(services, state, i, changes, line)
        except Exception as exc:  # recorded; the decision stands
            pr = {"error": f"{type(exc).__name__}: {exc}"}
        return {"status": status, "opened": opened, "record_pr": pr,
                "events": [event("act", status, line, opened=opened, url=pr.get("url", ""))]}

    g = StateGraph(ReviewState)
    g.add_node("prepare", prepare)
    g.add_node("review", review)
    g.add_node("act", act)
    g.add_edge(START, "prepare")
    g.add_edge("prepare", "review")
    g.add_edge("review", "act")
    g.add_edge("act", END)
    return g.compile(checkpointer=checkpointer, name="vrixel_initiative_review")


# --------------------------------------------------------------------------------- when

def due(services, product_id: str, roadmap: dict, outcomes: dict, today: date | None = None) -> list[tuple[dict, str]]:
    """Initiatives whose review is due: [(initiative, why)]. On their review date, or when the goal
    has read off track at the last two readings since approval."""
    from . import parse_metric
    from .readouts import _t, status

    today = today or date.today()
    out = []
    for i in roadmap.get("initiatives") or []:
        if i.get("status") not in ("approved", "active"):
            continue
        when = review_date(i)
        if when and when <= today:
            out.append((i, f"its review date, {when.isoformat()}, has come"))
            continue
        g = (outcomes.get("goals") or {}).get(i.get("goal") or "")
        if not g:
            continue
        m, _ = parse_metric(i["goal"], {k: v for k, v in g["metric"].items() if k != "goal_id"})
        start = _t(str(i.get("approved_at") or "")[:10] + "T00:00:00+00:00") if i.get("approved_at") else None
        pts = [p for p in g.get("trend") or [] if p.get("value") is not None and (not start or _t(p["end"]) >= start)]
        if m and len(pts) >= 2 and all(status(m, p["value"], start, _t(p["end"]))["status"] == "off_track" for p in pts[-2:]):
            out.append((i, f"its goal {i['goal']} has read off track twice in a row"))
    return out

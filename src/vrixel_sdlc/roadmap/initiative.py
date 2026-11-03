"""Initiative drafts: the format an agent writes, parsing, a deterministic stub, and the checks
that run before the initiative gate (G0)."""

from __future__ import annotations

import re

from ..duplicates import DUPLICATE, describe, find_similar, similarity
from .record import HORIZONS, RELEASE_MODELS, section

GOAL_LINE = re.compile(r"^-\s*(GOAL-[A-Z0-9-]+)\s*:", re.M)
NEW_GOAL = re.compile(r"^-\s*New goal:\s*(.+?)\s*\(measure:\s*(.+?)\)\s*$", re.M)


def _first_bullet(body: str, heading: str) -> str:
    for line in section(body, heading).splitlines():
        if line.strip().startswith("- "):
            return line.strip()[2:].strip()
    return section(body, heading).strip()


def parse_draft(text: str) -> dict:
    goal = GOAL_LINE.search(section(text, "Goal"))
    new_goal = NEW_GOAL.search(section(text, "Goal"))
    budget = re.search(r"[\d.]+", _first_bullet(text, "Budget").replace(",", ""))
    cands = []
    for line in section(text, "Candidate features").splitlines():
        line = line.strip()
        if line.startswith("- "):
            title, _, desc = line[2:].partition(":")
            if title.strip():
                cands.append({"title": title.strip(), "description": desc.strip()})
    return {
        "goal": goal.group(1) if goal else "",
        "new_goal": {"title": new_goal.group(1).strip(), "measure": new_goal.group(2).strip()} if new_goal else None,
        "outcome": section(text, "Outcome"),
        "measure": _first_bullet(text, "Success measure"),
        "horizon": _first_bullet(text, "Horizon").lower(),
        "budget_usd": float(budget.group(0)) if budget else 0.0,
        "lanes": [ln.strip()[2:].split()[0] for ln in section(text, "Lanes").splitlines() if ln.strip().startswith("- ")],
        "release_model": _first_bullet(text, "Release model").split()[0].lower() if section(text, "Release model") else "",
        "candidates": cands,
        "risks": section(text, "Risks"),
    }


def stub_draft(ctx: dict) -> str:
    """Deterministic draft for stub mode: picks the goal whose words overlap most, splits the
    request into candidate features, sizes the budget from the product's per-feature budget."""
    goals = ctx.get("goals") or []
    text = f"{ctx['title']} {ctx.get('request', '')}"
    best = max(goals, key=lambda g: similarity(text, "", g["title"], g.get("measure", "")), default=None)
    if best and similarity(text, "", best["title"], best.get("measure", "")) == 0:
        best = None  # no goal it plausibly serves: propose one
    if best:
        goal_line = f"- {best['id']}: {best['title']}"
    else:
        goal_line = f"- New goal: {ctx['title']} (measure: {ctx.get('measure') or 'adoption by target users within a quarter'})"
    parts = [p.strip(" .") for p in re.split(r";|\n|\. ", ctx.get("request", "")) if len(p.strip()) > 8]
    def title(p: str) -> str:
        t = p.split(",")[0][:60].strip()
        return t[:1].upper() + t[1:]

    cands = [(title(p), "" if title(p) == p[:1].upper() + p[1:] else p) for p in parts[:4]] or [
        (f"{ctx['title']}: core workflow", f"The main workflow of {ctx['title'].lower()}"),
        (f"{ctx['title']}: reporting", f"Measure {ctx['title'].lower()} against the success measure")]
    lanes = [ctx["product_id"]] + [lane for lane in ctx.get("dependency_lanes", [])
                                   if lane.lower() in ctx.get("request", "").lower()]
    regulated = re.search(r"\b(certif|regulat|audit|abdm|onc|hipaa|go-live|cutover)\w*", text, re.I)
    feedback = ctx.get("feedback") or ""
    lines = [
        f"# Initiative: {ctx['title']}",
        "", "## Goal", goal_line,
        "", "## Outcome", ctx.get("request") or ctx["title"],
        "", "## Success measure", f"- {ctx.get('measure') or (best or {}).get('measure') or 'Adoption by target users within a quarter'}",
        "", "## Horizon", f"- {ctx.get('horizon') or 'now'}",
        "", "## Budget", f"- {int(ctx['feature_budget'] * max(2, len(cands)))}",
        "", "## Lanes", *[f"- {lane}" for lane in lanes],
        "", "## Release model", "- train" if regulated else "- launch",
        "", "## Candidate features", *[f"- {t}: {d}" if d else f"- {t}" for t, d in cands],
        "", "## Risks", "- Scope grows beyond the budget; the gate reviews the envelope",
    ]
    if feedback:
        lines += ["", "## Changes from review", f"- {feedback}"]
    return "\n".join(lines) + "\n"


def check_draft(d: dict, ctx: dict) -> list[str]:
    out = []
    goal_ids = {g["id"] for g in ctx.get("goals") or []}
    if d["goal"]:
        if d["goal"] not in goal_ids:
            out.append(f"Goal {d['goal']} is not in the product's roadmap/goals.yaml; use one of "
                       f"{', '.join(sorted(goal_ids)) or 'none yet'} or write '- New goal: <title> (measure: <how measured>)'")
    elif not d["new_goal"]:
        out.append("No goal: name an existing goal (GOAL-...) or propose one with '- New goal: <title> (measure: ...)'")
    if not d["measure"]:
        out.append("No success measure: say how the initiative's outcome will be measured")
    if d["horizon"] not in HORIZONS:
        out.append(f"Horizon must be one of {', '.join(HORIZONS)}")
    cap = ctx["max_budget_usd"]
    if not 0 < d["budget_usd"] <= cap:
        out.append(f"Budget envelope {d['budget_usd']:.0f} must be above 0 and at most {cap:.0f} USD")
    allowed = {ctx["product_id"], *ctx.get("dependency_lanes", [])}
    if ctx["product_id"] not in d["lanes"]:
        out.append(f"Lanes must include the product itself ({ctx['product_id']})")
    for lane in d["lanes"]:
        if lane not in allowed:
            out.append(f"Lane {lane} is not the product or a lane it depends on ({', '.join(sorted(allowed))})")
    if d["release_model"] not in RELEASE_MODELS:
        out.append("Release model must be 'launch' (default: features ship dark and launch together) or 'train' "
                   "(regulated or contractual shipments)")
    if not d["candidates"]:
        out.append("No candidate features: break the initiative into the features that deliver it")
    for c in d["candidates"]:
        hits = [m for m in find_similar(c["title"], c["description"], ctx.get("features") or [], [])
                if m["duplicate"] and m["id"] not in c["description"]]
        if hits:
            out.append(f"Candidate '{c['title']}' repeats {describe(hits[0])}: drop it, or name that feature in its "
                       "description if this extends it")
    for other in ctx.get("initiatives") or []:
        if other["id"] != ctx.get("initiative_id") and other.get("status") not in ("dropped", "done") and \
                similarity(ctx["title"], d["outcome"], other["title"], other.get("outcome", "")) >= DUPLICATE:
            out.append(f"This looks like initiative {other['id']} “{other['title']}” ({other.get('status')}); "
                       "extend that one instead")
    return out

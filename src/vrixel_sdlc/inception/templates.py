"""Deterministic drafts for stub mode: built from the vision and the documents before them, they
pass the machine checks, so the flow can be run end to end without a model. They are plain on
purpose; a real model writes the documents in live mode."""

from __future__ import annotations

import re

from . import Vision, formats

_PHI = re.compile(r"\b(patient|clinic|clinical|health|medical|hipaa|phi|diagnos|prescription|dental)\w*", re.I)
_PAY = re.compile(r"\b(payment|card|pci|bank|wallet|refund|checkout|ledger)\w*", re.I)
_MOBILE = re.compile(r"\b(mobile|phone|ios|android|app in the field|offline)\b", re.I)
_EVENTS = re.compile(r"\b(alert|notif\w*|sync|event|stream|sensor|reminder)s?\b", re.I)
_AGENT = re.compile(r"\b(agent|assistant|ai|copilot|chatbot|llm)\b", re.I)


def _all_text(v: Vision) -> str:
    return " ".join([v.name, v.vision, *v.goals, *v.users, *v.constraints])


def guess_class(v: Vision) -> tuple[str, str]:
    t = _all_text(v)
    if _PHI.search(t):
        return "phi", "it stores health information about patients, which is protected health information"
    if _PAY.search(t):
        return "payments", "it handles payment details and money movement, which payment rules protect"
    return "general", "it stores operational records about the business and its users, nothing regulated"


def product_id(v: Vision) -> str:
    if v.product_id:
        return v.product_id
    letters = "".join(w[0] for w in re.findall(r"[A-Za-z]+", v.name)).upper()
    return (letters if len(letters) >= 2 else re.sub(r"[^A-Z]", "", v.name.upper()))[:6] or "NEW"


def brief(v: Vision, ctx: dict) -> str:
    pid = ctx.get("product_id") or product_id(v)
    prefix = ctx.get("prefix") or pid.lower() + "-"
    dc, why = guess_class(v)
    goals = []
    for n, g in enumerate(v.goals or [f"{v.name} is used by its first customers"], 1):
        m = re.match(r"^(.+?)\s*\((?:measure:\s*)?(.+)\)\s*$", g)
        goal, measure = (m.group(1), m.group(2)) if m else (g, "not stated yet")
        goals.append(f"- PG{n}: {goal} (measure: {measure})")
    users = [f"- U{n}: {u}" for n, u in enumerate(v.users or ["The people the vision describes"], 1)]
    comp = [f"- {c}" for c in v.constraints if re.search(r"\b(hipaa|pci|gdpr|soc|iso|wcag|regulat|compliance)", c, re.I)]
    lines = [
        f"# Product brief: {v.name}", "", "## Problem", v.vision.strip() or f"The problem {v.name} solves.",
        "", "## Users", *users, "", "## Goals", *goals,
        *evidence_lines(ctx.get("research", "")),
        "", "## Non-goals", f"- {v.name} doesn't replace the systems its users already rely on for other work",
        "", "## Scope", "- The first year covers the goals above, for the users above",
        "", "## Data class", f"- {dc}: {why}",
        "", "## Compliance", *(comp or ["- None: no regulation was named for this product"]),
        "", "## Risks", "- Scope grows beyond the budget: the product gate reviews each initiative's envelope",
        "", "## Product", f"- id: {pid}", f"- org: {v.org}", f"- repo prefix: {prefix}",
        f"- budget: {int(v.budget_usd or 1000)}",
        "", "## Agent notes", "- Assumed: the goals and users are the ones the person gave, in their words",
    ]
    return "\n".join(lines) + "\n"


def evidence_lines(research: str) -> list[str]:
    """The Evidence and Assumptions sections from a research note (none without one)."""
    from .. import research as rs

    if not research:
        return []
    ev = [e for e in rs.evidence(research) if e["id"]]
    asm = [a for a in rs.assumptions(research) if a["id"] and a["test"]]
    return (["", "## Evidence", *[f"- {e['claim'].rstrip('.')} ({e['id']})" for e in ev],
             "", "## Assumptions", *[f"- {a['line']}" for a in asm]])


def plan_components(v: Vision, prefix: str) -> list[tuple[str, str, str]]:
    """(name, role, responsibility), from what the vision mentions."""
    t = _all_text(v)
    comps = [("api", "provider", "keeps the product's records and rules, and serves them over an API")]
    if _MOBILE.search(t):
        comps += [("bff", "bff", "signs people in and is the mobile app's only way in"),
                  ("mobile", "mobile", "the app people use on their phones")]
    else:
        comps += [("web", "consumer", "the web app people use")]
    if _EVENTS.search(t):
        comps.append(("events", "event-worker", "reacts to events: sends alerts and keeps read models up to date"))
    if _AGENT.search(t):
        comps.append(("agent", "agent", "the assistant, reaching models through the product's gateway"))
    return comps


def architecture(v: Vision, ctx: dict) -> str:
    pid, org, prefix, dc = ctx["product_id"], ctx["org"], ctx["prefix"], ctx.get("data_class", "general")
    comps = plan_components(v, prefix)
    lines = [f"# Product architecture: {v.name}", "", "## Context",
             f"People reach {v.name} through its {'mobile app' if any(r == 'mobile' for _, r, _ in comps) else 'web app'}; "
             "everything runs on the platform's OpenChoreo, with records in Postgres and events on Kafka.",
             "", "## Components"]
    lines += [f"- {n} ({r}): {d} [repo: {org}/{prefix}{n}]" for n, r, d in comps]
    lines += ["", "## Resources", f"- records: postgres-crossplane; database={prefix.rstrip('-')}; size production=medium",
              "    writes: api; reads: api"]
    lines += ["", "## Shared services", "- None: the product starts on the platform's own services",
              "", "## NFRs", "- NFR-1 (performance): 95% of API requests answer within 300 ms at the expected peak",
              "- NFR-2 (availability): 99.9% a month in production",
              "- NFR-3 (accessibility): WCAG 2.1 AA for every screen",
              "", "## ADRs",
              f"- ADR-{pid}-1: keep records in Postgres through the platform's catalog (alternatives: DynamoDB; "
              "consequences: one relational store to back up and migrate)",
              f"- ADR-{pid}-2: one provider owns every record; clients reach it only through its API "
              "(alternatives: a database per client; consequences: one place for rules and audit)"]
    if dc in ("phi", "payments"):
        lines += ["", "## Audit", f"Every read or change of {dc} records is published to the product's audit topic (VX-15)."]
    if ctx.get("existing"):
        lines += ["", "## Current state", "The repos listed in the product's governance today.",
                  "", "## Gap", "What the components above add or change."]
    lines += ["", "## Agent notes", "- Assumed: the platform defaults fit until a feature shows otherwise"]
    return "\n".join(lines) + "\n"


def roadmap(v: Vision, ctx: dict) -> str:
    goals = ctx["goals"]
    comps = list(ctx["components"])
    budget = float(ctx.get("budget_usd") or 1000)
    goal_lines = ctx.get("goal_lines") or [f"- {g}" for g in goals]
    rest = goals[1:] or []
    first_budget = int(budget * (0.4 if rest else 0.9))
    each = int((budget * 0.9 - first_budget) / max(1, len(rest))) if rest else 0
    lines = ["# Roadmap: " + v.name, "", "## Goals", *goal_lines, "", "## Initiatives",
             f"- INI-1: A thin working version [horizon: now; budget: {first_budget}; goals: {goals[0]}; after: none]",
             f"    - Sign-in and the first end-to-end path: a person signs in and completes the main task "
             f"[builds: {', '.join(comps)}]",
             f"    - Deployment pipeline to dev: every repo builds and deploys to dev [builds: {comps[0]}]"]
    for n, g in enumerate(rest, 2):
        lines += [f"- INI-{n}: Move {g} [horizon: {'next' if n == 2 else 'later'}; budget: {each}; goals: {g}; "
                  f"after: INI-{n - 1}]",
                  f"    - The first change toward {g}: what the brief's measure for {g} needs [builds: {comps[0]}]"]
    lines += ["", "## Agent notes", "- Assumed: the order follows the goals' order in the brief"]
    return "\n".join(lines) + "\n"


def goal_lines(brief_text: str) -> list[str]:
    return [f"- {g['id']}: {g['goal']}" for g in formats.goals(brief_text) if g.get("id")]


TEMPLATES = {"brief": brief, "architecture": architecture, "roadmap": roadmap}

"""The three inception documents: the shape each agent is told to write, and the parsers the
checks, the cost estimate and (later) the setup read them with. Anything not named here is free
prose."""

from __future__ import annotations

import re

from .. import diary
from ..agents.parse import section

_COMMON = ("Write Markdown. Use exactly these '## ' section headings (other sections may be added) and "
           "write the bullet lines in exactly the forms shown; checks read them by machine.")

CONTRACTS = {
    "brief": """# Product brief: <name>
## Problem
<who has which problem today, in their words>
## Users
- U1: <who they are>: <their job, and the situation they're in>
## Goals
- PG1: <the outcome> (measure: <a metric with a number or threshold, and by when>)
## Evidence
- <a claim about the users, the problem, the alternatives or the market> (E1, E3)      citing the research note
## Assumptions
- A1: We believe <what the plan bets on>; test: <how we'd know: a number, an event or a goal, and by when>
## Non-goals
- <what this product won't do, that someone might expect>
## Scope
- <what the first year covers>
## Data class
- <general | phi | payments>: <why, from what the product stores>
## Compliance
- <regulation or standard>: <what it requires of the product>        or '- None: <why>'
## Risks
- <risk>: <how it's handled>
## Product
- id: <2 to 6 capital letters>
- org: <the GitHub org>
- repo prefix: <lowercase letters, ending in '-'>
- budget: <US dollars for the first year of features>""",
    "architecture": """# Product architecture: <name>
## Context
<who and what the product talks to>
## Components
- <component> (<role>): <its one responsibility> [repo: <org>/<repo prefix><name>]
        roles: provider, consumer, bff, mobile, event-worker, agent, mcp-server, library
## Resources                  (managed services from the platform catalog; omit when the defaults do)
- <name>: <catalog type>; <key>=<value>, ...[; size <env>=<size>]
    writes: <component>; reads: <component>
## Shared services
- <shared lane or capability>: <what the product uses it for>        or '- None: <why>'
## NFRs
- NFR-1 (<kind>): <a measurable target: a number, a percentile, a limit or a named standard>
        kinds: performance, availability, reliability, scalability, security, privacy, accessibility,
        compliance, usability, maintainability, observability
## ADRs
- ADR-<product id>-<n>: <decision> (alternatives: <what else was weighed>; consequences: <what follows>)
## Current state              (existing products only: what runs today)
## Gap                        (existing products only: what changes to reach this architecture)""",
    "roadmap": """# Roadmap: <name>
## Goals
- PG1: <goal, as in the brief>
## Initiatives               (in the order to build them)
- INI-1: <title> [horizon: now; budget: <USD>; goals: PG1; after: none]
    - <candidate feature>: <one line on what it does> [builds: <component>, <component>]
- INI-2: <title> [horizon: next; budget: <USD>; goals: PG1, PG2; after: INI-1]
        horizon: now, next or later; after: the initiatives that must come first, or none""",
}
for _doc in CONTRACTS:
    CONTRACTS[_doc] += "\n" + diary.CONTRACT


def contract(doc: str) -> str:
    return f"{_COMMON}\n\n{CONTRACTS[doc]}"


def bullets(text: str, heading: str) -> list[str]:
    return [ln.strip()[2:].strip() for ln in section(text, heading).splitlines() if ln.strip().startswith("- ")]


# ------------------------------------------------------------------------------------- brief

_GOAL = re.compile(r"^(PG\d+):\s*(.+?)\s*\(measure:\s*(.+)\)\s*$")
_USER = re.compile(r"^(U\d+):\s*(.+)$")
_CLASS = re.compile(r"^(general|phi|payments)\s*:\s*(.+)$", re.I)
_KV = re.compile(r"^([a-z ]+):\s*(.+)$", re.I)


def goals(text: str) -> list[dict]:
    out = []
    for b in bullets(text, "Goals"):
        m = _GOAL.match(b)
        out.append({"id": m.group(1), "goal": m.group(2), "measure": m.group(3)} if m else {"id": "", "line": b})
    return out


def users(text: str) -> list[dict]:
    return [{"id": m.group(1), "who": m.group(2)} for b in bullets(text, "Users") if (m := _USER.match(b))]


def data_class(text: str) -> tuple[str, str]:
    for b in bullets(text, "Data class"):
        m = _CLASS.match(b)
        if m:
            return m.group(1).lower(), m.group(2).strip()
    return "", ""


def product(text: str) -> dict:
    out = {}
    for b in bullets(text, "Product"):
        m = _KV.match(b)
        if m:
            out[m.group(1).strip().lower()] = m.group(2).strip()
    budget = re.search(r"[\d][\d,.]*", out.get("budget", ""))
    return {"id": out.get("id", ""), "org": out.get("org", ""), "prefix": out.get("repo prefix", ""),
            "budget_usd": float(budget.group(0).replace(",", "")) if budget else 0.0}


# ------------------------------------------------------------------------------ architecture

_COMPONENT = re.compile(r"^([a-z][a-z0-9-]*)\s*\(([a-z-]+)\)\s*:\s*(.+?)(?:\s*\[repo:\s*([^\]]+)\])?\s*$")
_NFR = re.compile(r"^(NFR-\d+)\s*\(([a-z]+)\)\s*:\s*(.+)$")
_ADR = re.compile(r"^(ADR-[A-Z0-9]+-\d+)\s*:\s*(.+)$")


def components(text: str) -> list[dict]:
    out = []
    for b in bullets(text, "Components"):
        m = _COMPONENT.match(b)
        out.append({"name": m.group(1), "role": m.group(2), "does": m.group(3).strip(),
                    "repo": (m.group(4) or "").strip()} if m else {"name": "", "line": b})
    return out


def nfrs(text: str) -> list[dict]:
    return [{"id": m.group(1), "kind": m.group(2), "target": m.group(3)} if (m := _NFR.match(b)) else {"id": "", "line": b}
            for b in bullets(text, "NFRs")]


def adrs(text: str) -> list[dict]:
    return [{"id": m.group(1), "text": m.group(2)} if (m := _ADR.match(b)) else {"id": "", "line": b}
            for b in bullets(text, "ADRs")]


# ----------------------------------------------------------------------------------- roadmap

_INI = re.compile(r"^-\s*(INI-\d+)\s*:\s*(.+?)\s*\[(.+)\]\s*$")
_FEAT = re.compile(r"^-\s*(.+?)\s*:\s*(.+?)\s*\[builds:\s*([^\]]*)\]\s*$")


def initiatives(text: str) -> list[dict]:
    """Each initiative with its attributes and candidate features, in the order written."""
    out: list[dict] = []
    for raw in section(text, "Initiatives").splitlines():
        if not raw.strip().startswith("- "):
            continue
        indented = raw[: len(raw) - len(raw.lstrip())] != ""
        line = raw.strip()
        if not indented:
            m = _INI.match(line)
            if not m:
                out.append({"id": "", "line": line, "features": []})
                continue
            attrs = {}
            for part in m.group(3).split(";"):
                if ":" in part:
                    k, v = part.split(":", 1)
                    attrs[k.strip().lower()] = v.strip()
            budget = re.search(r"[\d][\d,.]*", attrs.get("budget", ""))
            out.append({"id": m.group(1), "title": m.group(2), "horizon": attrs.get("horizon", "").lower(),
                        "budget_usd": float(budget.group(0).replace(",", "")) if budget else 0.0,
                        "goals": [g.strip() for g in attrs.get("goals", "").split(",") if g.strip()],
                        "after": [a.strip() for a in attrs.get("after", "").split(",")
                                  if a.strip() and a.strip().lower() != "none"],
                        "features": []})
        elif out:
            m = _FEAT.match(line)
            out[-1]["features"].append(
                {"title": m.group(1), "does": m.group(2),
                 "builds": [c.strip() for c in m.group(3).split(",") if c.strip()]} if m else {"title": "", "line": line})
    return out

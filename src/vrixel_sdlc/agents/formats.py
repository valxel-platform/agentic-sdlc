"""The shape each phase's document must have, told to the agent that writes it.

The deterministic checks (evaluators/checks.py) and the parsers (agents/parse.py) read these
documents line by line: a section the check looks for, a bullet in the form the parser expects.
Until now only the stub templates knew those shapes, so a real model had to guess them and lost
evaluator loops to format alone. Each contract below states exactly what the checks read; anything
else in the document is free prose. A test keeps every contract in step with the stub templates,
which satisfy the checks.
"""

from __future__ import annotations

_COMMON = ("Write Markdown. Use exactly these '## ' section headings (other sections may be added) and "
           "write the bullet lines in exactly the forms shown; checks read them by machine.")

CONTRACTS: dict[str, str] = {
    "discovery": """# Opportunity brief: <title>
## Problem
<who has which problem>
## Capability
Extends: CAP-<id> (<name>)        or        New capability: <why nothing existing covers it>
## Success metrics
- M1: <metric with a number or threshold>
## Constitution articles
- VX-<n>""",
    "spec": """# PRD: <title>
## Goals
- G1: <goal> (measured by M1)
## Non-goals
- <what this feature won't do>
## Acceptance criteria
- AC-1: Given <context>, when <action>, then <observable result>
## Constitution articles
- VX-<n>                (at least one article of the platform or product constitution)""",
    "design": """# UX spec: <title>
## Screens
- S1: <screen>
## States
- <loading, empty, error, success, ...>
## Accessibility
- WCAG 2.1 AA: <specific criteria>""",
    "architecture": """# HLD: <title>
## Builds on
- ADR-<id>: <title>     (accepted decisions this design relies on, or '- None: <why>')
## ADRs
- ADR-<feature id>-<n>: <decision>
## Affected repos
- <org>/<repo> (<role>)        role: provider, consumer, library, event-worker, agent, mcp-server, bff, mobile, acceptance
## New repos                   (only when a repo must be created)
- <org>/<repo> (<role>): <purpose> [ADR-<feature id>-<n>]   (that ADR must name the repo)
## Dependency lanes            (only when a lane's repo changes)
- <org>/<repo> (<lane>): <what changes>
## Contracts
- <org>/<repo>: <version change and compatibility>
## Diagram
```mermaid
flowchart LR  (one node per affected repo)
```
## Audit                       (what gets an audit record)
- <record>.<action>: <record> (<who did what>)
## Endpoints
- <org>/<repo>: <none|project|namespace|internal|external>; calls <repo>, <repo>
## Events                      (when records change through Kafka)
- vrixel.<record>.<event>.v<n>: published by <repo>, applied by <repo>
## Resources                   (AWS services, only when needed; each with an ADR)
- <name>: <type>; <param>=<value>
  writes: <repo>; reads: <repo>
## Performance                 (decides which performance tests run)
- Expected load: <requests or events per second at peak, or "as today">
- New traffic: <yes|no>: <why>
- Data growth: <yes|no>: <why>
- Journeys: <the user journeys to load-test, or none>
## Constitution articles
- VX-<n>""",
    "plan": """# Plan: <title>
## Tasks
```json
{"tasks": [{"id": "TASK-1", "repo": "<org>/<repo from the HLD's Affected repos>", "role": "<role>",
            "acs": ["AC-1"], "files": ["<path>"]}]}
```
## Traceability
- AC-1 -> TASK-1 / TEST-1     (one line for every acceptance criterion in the PRD)
## Acceptance tests            (when the product has an acceptance repo and the feature spans repos)
- AC-1: extend <file> "<test title>"      or      - AC-1: add <file> covers: <component> <route or page>""",
    "release": """# Release: <title>
## Checklist
- [x] <item>                   (every item checked: '- [ ]' fails)
- [x] Rollback plan: <ordered steps>
## Staging
- <what was verified in staging>""",
    "operate": """# Operating report: <title>
## Metrics vs brief
- M1: <measured value against the target>
## Capability
- <the capability this release registered or extended>""",
}


def contract(phase: str) -> str:
    """What to tell the agent about the document's shape ('' for phases without one)."""
    c = CONTRACTS.get(phase)
    return f"{_COMMON}\n\n{c}" if c else ""

"""The shape each phase's document must have, told to the agent that writes it.

The deterministic checks (evaluators/checks.py) and the parsers (agents/parse.py) read these
documents line by line: a section the check looks for, a bullet in the form the parser expects.
Until now only the stub templates knew those shapes, so a real model had to guess them and lost
evaluator loops to format alone. Each contract below states exactly what the checks read; anything
else in the document is free prose. A test keeps every contract in step with the stub templates,
which satisfy the checks.
"""

from __future__ import annotations

from .. import diary as _diary

_COMMON = ("Write Markdown. Use exactly these '## ' section headings (other sections may be added) and "
           "write the bullet lines in exactly the forms shown; checks read them by machine.")

CONTRACTS: dict[str, str] = {
    "discovery": """# Opportunity brief: <title>
## Problem
<who has which problem>
## Capability
Extends: CAP-<id> (<name>)        or        New capability: <why nothing existing covers it>
## Product goal                (the goal in roadmap/goals.yaml this feature moves)
- GOAL-<id>: <how this feature moves it>          or          - None: <why no product goal applies>
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
- AC-1 (G1): Given <context>, when <action>, then <observable result>     (the goal it serves in brackets)
## Non-functional requirements
- NFR-1 (performance, G1): <a measurable target: a number, a percentile, a limit or a named standard>
                        kinds: performance, availability, reliability, scalability, security, privacy,
                        accessibility, compliance, usability, maintainability, observability, other
                        (or '- None: <why this feature adds no requirement beyond the product's own>')
## Instrumentation             (the events that show whether the feature moves the brief's product goal)
- <event_name> (GOAL-<id>): fires when <what happened>; properties: <property>, <property>
                        lowercase names; properties are categories and numbers, never a person's name,
                        contact details or ids (or '- None: <why>' when the brief names no product goal)
## Constitution articles
- VX-<n>                (at least one article of the platform or product constitution)""",
    "design": """# UX spec: <title>
## Screens
- S1: <screen> (web: <org>/<repo>)      (the app it lives in: web or mobile, one of the product's app repos)
## States                      (one line per screen)
- S1: loading, empty (<what the person sees and can do>), error (<what they see and can do>), success
                        every screen has empty and error, or says 'no empty: <why>'
## Flow
- start -> S1: <where the person comes from>
- S1 -> S2: <the action that takes them there>
- S2 -> done                   (or '-> cancel'; every screen is reachable from start)
## Copy                        (button labels, headings and error messages, word for word)
- S1 <element>: "<text>"
## Coverage                    (one line for every acceptance criterion in the PRD)
- AC-1: S1                     (the screen or state that shows it, or 'not on screen: <why>')
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
## Flows                       (when two or more components talk: one sequence per journey, and a failure path)
```mermaid
sequenceDiagram
  title <journey>
  participant <repo name without the org>
  web->>api: <call>
  alt <failure>
    api-->>web: <what happens>
  end
```
## Audit                       (what gets an audit record)
- <record>.<action>: <record> (<who did what>)
## Endpoints
- <org>/<repo>: <none|project|namespace|internal|external>; calls <repo>, <repo>
## Events                      (when records change through Kafka)
- vrixel.<record>.<event>.v<n>: published by <repo>, applied by <repo>
## Instrumentation             (one line for every event in the PRD's Instrumentation section)
- <event_name>: <org>/<repo>   (the affected repo that records it)
## Data                        (required when a provider or event worker changes; '- None: <why>' otherwise)
- <table>: <new | changed | none>; <columns added, changed or dropped>
- Migration: <Flyway version>; <expand now, contract in a later release | one step: why it's safe>
- Backfill: <none | how, how long, whether it runs while the product is live>
- Retention: <how long records are kept, and why>      (required for PHI and payments products)
- Rollback: <what happens to data the new version wrote if the release is rolled back>
## Threats                     (every product)
- Boundary: <org>/<repo>: <where untrusted input enters>        (each project, namespace or external endpoint)
- Authorization: <org>/<repo> <endpoint or 'all endpoints'>: <who may call it, and which records they may touch>
- <threat (spoofing, tampering, disclosure, denial of service, privilege escalation...)>: <who could do it>; prevented: <how>; detected: <how>
## Resources                   (AWS services, only when needed; each with an ADR)
- <name>: <type>; <param>=<value>
  writes: <repo>; reads: <repo>
## Coverage                    (one line for every acceptance criterion and NFR in the PRD)
- AC-1: <org>/<repo>: <the part of the design that handles it>     (or 'no change: <why>')
- NFR-1: <how the design meets it>
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
            "acs": ["AC-1"], "files": ["<path>"], "screens": ["S1"]}]}
```
(every task lists the criteria it implements; a task that implements none directly says why in "why";
 a web or mobile task (consumer, mobile) lists the UX spec's screens it builds under "screens", and
 together they build every screen)
## Traceability
- AC-1 -> TASK-1              (one line for every acceptance criterion and NFR in the PRD)
- NFR-1 -> TASK-1
- AC-2 -> deferred: <why, and when it will be done>   (only a requirement deliberately left out)
## Test cases                  (at least one for every requirement not deferred; cover the error paths too)
- TC-1 (AC-1, happy): <what it checks> -> TASK-1 <test file>::<test name>
- TC-2 (AC-1, error): <what it checks> -> TASK-1 <test file>::<test name>
- TC-3 (NFR-1, performance): <what it checks> -> suite performance
                        kinds: happy, error, edge, permission, dependency, data, or the NFR's kind
                        test: 'TASK-n <file>::<name>' (that task's coder writes it under exactly that name),
                        'acceptance <file> "<title>"', 'suite <quality suite>', or 'manual: <why>' (needs
                        a person to override at release)
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


# Every document ends with the agent's notes for the people at the gate (vrixel_sdlc.diary).
for _phase in CONTRACTS:
    CONTRACTS[_phase] += "\n" + _diary.CONTRACT


def contract(phase: str) -> str:
    """What to tell the agent about the document's shape ('' for phases without one)."""
    c = CONTRACTS.get(phase)
    return f"{_COMMON}\n\n{c}" if c else ""

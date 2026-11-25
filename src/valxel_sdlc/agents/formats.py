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
- S1: <screen> (web: <org>/<repo>) [/route]   (the app it lives in, and for a web screen its route: staging visits it)
## States                      (one line per screen)
- S1: loading, empty (<what the person sees and can do>), error (<what they see and can do>), success
                        every screen has empty and error, or says 'no empty: <why>'
## Flow
- start -> S1: <where the person comes from>
- S1 -> S2: <the action that takes them there>
- S2 -> done                   (or '-> cancel'; every screen is reachable from start)
## Copy                        (button labels, headings and error messages, word for word)
- S1 <element>: "<text>"
## Components                  (what the screens use, from the product's design record)
- <Name>: existing
- <Name>: new: <why no existing component fits>
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
- <org>/<repo> (<role>)        role: provider, consumer, library, event-worker, knowledge, agent, mcp-server, bff, mobile, acceptance
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
                               (An agent that runs code it writes calls the product's code sandbox (role
                                code-sandbox): project-visible, calling nothing, called by agents only.)
## Schedules                   (only with a job repo: when each job runs and what for; people approve it at G4)
- <org>/<repo>: <minute> <hour> <day of month> <month> <day of week> <IANA time zone>; <what it does>
                               (numbers only, e.g. '15 3 * * * America/Chicago'; its work covers the time since its last
                                success and must be safe to run twice. Choose a job for work due at a time; an
                                event worker for work that reacts to events.)
## Pipelines                   (only with a transform repo: what it reads and publishes; approved at G4)
- <org>/<repo>: reads <dataset>[ as <table>], ...; warehouse postgres; marts <name>:<data class>, ...
  [; shares <mart> with <destination>, ...]
                               (each dataset is a contract under Datasets that lists the transform under consumers,
                                and a reporting repo here loads it as <table> (default: the name after the product,
                                emr.notes -> notes). A mart's class is the highest any of its columns may carry.
                                Only a mart of class none is shared outside the product. The transform also needs
                                its Schedules line, after the loads it reads. No Spark, streaming engines or
                                orchestrators: flag the need instead.)
## Integrations                (only with a connector repo: who's on the other side and what may cross; approved at G4)
- <org>/<repo>: <vendor>; data <general|payments|phi>; baa <yes|no>; webhooks <name, ...|none>; calls <host, ...|none>;
  auth <oauth2-client-credentials|api-key|mtls|none>[; pulls <name>:<http-cursor|object-store:<resource>>, ...]
                               (PHI needs baa yes. Hosts by name: no scheme, wildcard or IP. A connector with webhooks
                                is external in Endpoints, only for /webhooks/<name>; without them, none. Only a
                                connector talks to a third party: others reach it through data contracts. pulls:
                                the sources it polls when the vendor can't call it; an http-cursor source is read
                                through its 'calls' hosts, an object-store source reads an object-bucket in
                                Resources with 'reads: <connector>' (a connector reads buckets, nothing else).
                                Only http-cursor and object-store: anything else
                                (SharePoint, Confluence, Drive, Spark, an orchestrator) is flagged, not improvised.)
## Channels                    (only with a channel repo: where people talk to the product and what's said there; approved at G4)
- <org>/<repo>: <slack|teams|sms|email> <full|notice> [baa]; ...
                               (full sends the text; notice sends a sign-in notice. In a PHI product, full needs
                                baa: a BAA with that provider. A channel repo is external in Endpoints: providers
                                call its /channels/<kind> routes.)
## Agent tools                 (only with an agent repo: the MCP tools it may call; approved at G4)
- <org>/<repo>: read <server>__<tool>, ...|none; propose <server>__<tool>|<server>__*, ...|none
                               (read tools run at once; proposed ones wait for a person to approve each
                                call. Read tools are named exactly, never a wildcard, and only tools
                                that change nothing.)
## Documents                   (only with a document-intake repo: what people upload and what's read from it; approved at G4)
- <org>/<repo>: <type>; formats <pdf, png, jpeg, tiff>; max <N> MB; keep <N> days; dataset <name>; fields <name>*, <name>
                               (* marks a required field. The repo also needs two object-buckets in Resources it reads,
                                writes and deletes: a quarantine with expire_days of at most 7, and a clean bucket.
                                Its Endpoints visibility is project: people reach it through the BFF.)
## Knowledge                   (only with a knowledge repo: what agents search, and the embedding model people decide at G4)
- <org>/<repo>: model <embedding model> via <gateway (BAA)|gateway|self-hosted>; sources: <name> (<events <topic>, compacted|scan <store>|files>), ...
                               (an events source is rebuilt by replaying its topic: 'compacted' when its events carry whole
                                records keyed by id, otherwise make it a scan of the service's database)
## Events                      (when records change through Kafka)
- valxel.<record>.<event>.v<n>: published by <repo>, applied by <repo>
## Datasets                    (with Events or a Knowledge events source: one ODCS v3 data contract per topic, each in
                                its own ```yaml block. Released contracts are in the governance repo's datasets/; change
                                one only compatibly (add optional properties, enum values or stricter classes, raise its
                                version) or add a new event type, topic and contract. Every property has a classification:
                                none, pii, financial, phi or secret; free text is always phi.)
```yaml
apiVersion: v3.2.0
kind: DataContract
id: <product>.<name>
name: <what the records are>
version: 1.0.0
status: active
schema:
  - name: <name>
    physicalType: topic
    physicalName: <prefix>.<entity>.v1
    logicalType: object
    properties:
      - { name: <id>, logicalType: string, required: true, primaryKey: true, classification: none }
      - { name: <field>, logicalType: <string|integer|number|boolean|date|timestamp|object|array>, classification: <class> }
customProperties:
  - { property: owner, value: <org>/<repo that publishes it> }
  - { property: events, value: [valxel.<record>.<event>.v1] }
  - { property: compacted, value: <true when events carry whole records keyed by id> }
  - { property: retention, value: <90d|7y...> }
  - { property: tenant, value: <the property saying whose record it is> }
  - property: consumers
    value:
      <org>/<repo>: [<the properties it reads>]
```
## Instrumentation             (one line for every event in the PRD's Instrumentation section)
- <event_name>: <org>/<repo>   (the affected repo that records it)
## Data                        (required when a provider or event worker changes; '- None: <why>' otherwise)
- <table>: <new | changed | none>; <columns added, changed or dropped>
- Migration: <Flyway version>; <expand now, contract in a later release | one step: why it's safe>
- Backfill: <none | how, how long, whether it runs while the product is live>
- Retention: <how long records are kept, and why>      (required for PHI and payments products)
- Rollback: <safe | not safe>; <what happens to data the new version wrote if the release is rolled back>
                               (safe: the previous version runs on the new data. Not safe: an incident
                                fixes forward instead of rolling back)
## Depends on features         (every design: released features this one needs; an incident turns them off together)
- <FEATURE-ID>: <what it uses>                            (or '- None: <why>')
## Flag off                    (when a provider, web app or agent changes: what users get with the flag off)
- <today's behavior, unchanged: what still works for the features that use this one>
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
- Added load: <component> +<n> requests/s; <component> +<n> events/s      (at the busy hour after a year; when new traffic is yes)
## Constitution articles
- VX-<n>""",
    "plan": """# Plan: <title>
## Tasks
```json
{"tasks": [{"id": "TASK-1", "repo": "<org>/<repo from the HLD's Affected repos>", "role": "<role>",
            "acs": ["AC-1"], "adrs": ["ADR-<feature id>-1"], "files": ["<path>"], "screens": ["S1"]}]}
```
(every task lists the criteria it implements; a task that implements none directly says why in "why";
 "adrs" lists the HLD's decisions the task carries out: its coder and reviewer see only those, its
 criteria and its repo's lines of the HLD;
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
## What shipped
- <what changed for people, and in which repos>
## How it's released
- <the flag, the rollout steps, who it's on for now>
## What to watch
- M1: <the brief's metric> toward <its target>; readouts at 7, 30 and 90 days after release
## Capability
- <the capability this release registered or extended>""",
}


# Every document ends with the agent's notes for the people at the gate (valxel_sdlc.diary).
for _phase in CONTRACTS:
    CONTRACTS[_phase] += "\n" + _diary.CONTRACT


def contract(phase: str) -> str:
    """What to tell the agent about the document's shape ('' for phases without one)."""
    c = CONTRACTS.get(phase)
    return f"{_COMMON}\n\n{c}" if c else ""

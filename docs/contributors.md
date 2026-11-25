# Working on Valxel

For people changing the tool itself: how its agents are prompted, traced and evaluated, the pilot that measures it, how the design maps to code, the decisions and the layout.

Back to the [README](../README.md).

## Contents

- [Prompt practices borrowed from AWS's AI-DLC](#prompt-practices-borrowed-from-awss-ai-dlc)
- [Agent traces: what each session did](#agent-traces-what-each-session-did)
- [Evals of the tool's own agents](#evals-of-the-tools-own-agents)
- [Predictable results](#predictable-results)
- [The pilot: measuring the 85/15 goal](#the-pilot-measuring-the-8515-goal)
- [How the design maps to code](#how-the-design-maps-to-code)
- [Decisions](#decisions)
- [Changing the schema](#changing-the-schema)
- [Layout](#layout)

## Prompt practices borrowed from AWS's AI-DLC

[AI-DLC](https://github.com/awslabs/aidlc-workflows) is AWS's agentic SDLC; these of its prompt
practices are ours too.

- **Outside text is data.** Error messages, log lines, metrics, alerts, customer reports, the
  request typed at intake, OpenChoreo's analyses and the documents a critic or reviewer judges go
  into prompts only inside `<untrusted source="...">` blocks (`agents/untrusted.py`), defused so
  they can't close or forge a block. Every role is told such text is evidence and never an
  instruction: it can't change the task, skip a check or a gate, reveal configuration or trigger a
  tool, and an attempt is reported.
- **Coders never weaken a check to pass.** The coder's persona says quality targets are inputs,
  not suggestions; `evaluators/weakening.py` makes it stick. It compares each changed file on the
  task's branch with the default branch and finds skipped, focused or expected-to-fail tests,
  deleted test files and test cases, added suppressions (`noqa`, `type: ignore`, `eslint-disable`,
  `ts-ignore`, `nosec` and others) and lowered coverage or lint thresholds. The reviewer is told
  what was weakened, the review can't approve it, and the task goes back to its coder with the
  findings; weakening that is the right call is for the people at the gate to accept.
- **Critics and reviewers try to refute.** They start from "this isn't met" and look for what is
  missing as hard as for what is wrong. A finding names its evidence (a section, an id, a file and
  line, a test); a concern they can't tie to evidence is a note. The checklists add the readiness
  questions: could QA write a test from every criterion, could a developer build each repo's change
  without asking the architect, is every assumption listed. The reviewer is given the coder's test
  run to quote, and a claim about tests with no run behind it is a finding. A task the review
  blocks goes back to its coder with what blocked it.
- **Every role has a persona** (`agents/personas/<role>.md`): what it owns, its principles and what
  done means, ahead of the plugin skills in the prompt. Fallback, second-opinion and shadow roles
  use their base role's persona. Roles that run as sessions (architect, coder, reviewer) are told
  to keep the end of their turn budget for the reply: a partial answer beats none.
- **Tracing runs both ways.** Every PRD criterion has a task and test in the plan, or is deferred
  with a reason (`- AC-2 -> deferred: <why>`); every task names the criteria it implements, or says
  why it is needed (`"why"`); a task naming a criterion the PRD doesn't have is a finding.
- **Documents cite what they build on.** The UX spec and the HLD each have a `## Coverage` section
  with a line for every PRD criterion (where it is handled, or why not there).
- **Approving confirms the assumptions.** The gate shows what the agents assumed, says approving
  confirms it, and records an `assumptions_confirmed` event with the list and the approver.

## Agent traces: what each session did

Every agent session is an OpenTelemetry span, `valxel.agent <phase>/<role>`, carrying the feature,
product, phase, role, task, attempt, data class and config version, plus OpenTelemetry's GenAI
attributes (operation, agent, model, provider). Each session's own telemetry nests under it, so one
trace shows the whole session (`valxel_sdlc/telemetry.py`):

- **Claude Code** sessions export a span per turn, model request, tool call and subagent, and an
  event per tool result. The tool gives each session the OTLP settings, its resource attributes and
  a `TRACEPARENT`, so the session's spans are children of the tool's span.
- **Codex** sessions export a session span with model requests and tool calls, from an `[otel]`
  table added to the session's own `CODEX_HOME` (unless the home template sets one).
- **Single calls** (documents, critics, the second opinion) are the agent span itself.

Every ledger row records the trace, task and attempt of the session that made it. The feature page
lists the feature's sessions (step, agent and model, task and try, time, cost) with a link to each
trace, and G5 and G6 show the coder and reviewer sessions of the change set, so an approver can see
how a change was made, not only the diff.

**What traces may carry** is set per data class in `config/telemetry.yaml`: `none` (structure
only: tool names, timings, tokens, cost, outcome), `tool_details` (plus tool arguments: file paths,
commands, search patterns) or `full` (plus prompts and tool output). The shipped setting is `none`
for every class. PHI and payments go above `none` only when `content_store_baa: true`, and the
tool sets each content switch explicitly for every session, so nothing inherited from the container
turns content on.

**Where traces go.** The tool sends OTLP/HTTP to `VALXEL_OTEL_ENDPOINT`, a collector the platform
runs, which routes them: structure for every lane to OpenChoreo's observability plane (next to the
product's own traces, in the portal's Traces tab), and to the platform's self-hosted Langfuse for
session views, eval datasets and people's labels, with content only for the lanes allowed it (the
collector, Langfuse and the routing live in platform-foundation). `VALXEL_TRACE_URL` is the link
template, e.g. `https://langfuse.example.com/project/valxel/traces/{trace_id}`. Every span carries
`deployment.environment.name`: `production` unless `VALXEL_TRACE_ENVIRONMENT` says otherwise, and
`test` for eval replays, the platform's staging cluster and dev installs, which the collector sends
to Langfuse's test project ([ADR 0011](adr/0011-agent-traces-one-langfuse-two-projects.md)). Without an endpoint,
or without the OpenTelemetry SDK (`pip install '.[telemetry]'`, included in `live`), nothing is
exported and strict mode warns.

## Evals of the tool's own agents

A change to the models, prompts or settings the agents run with is checked against past work
before it merges (`valxel_sdlc/evals.py`).

**Cases come from history.** When a document phase (discovery, spec, design, architecture, plan)
finishes, the tool keeps an eval case: what the agent was given (the request, the parent
documents, the product's knowledge), what it wrote, and the critic's verdict. The gate that
follows labels it with what people decided (approved, sent back and why, taken over, or at an
escalation accepted as it was or rewritten), and the version people approved becomes its reference.
The plan has no gate: its cases are replayed but have no decision to agree with.

**A run replays cases** under a config, with its own ledger and stand-ins for GitHub and
OpenChoreo, so nothing real changes and no feature's cost moves. For each case it measures whether
the new document passes the checks and the checklist critic, the share of must-have items met,
whether the config's critic agrees with people about the document they decided on, optionally a
pairwise judgment against the approved version, and cost and time. The comparison runs the same
cases under the config in use and the candidate and fails when a rate drops by more than
`max_drop` (5 points by default) or, if set, cost rises more than `max_cost_increase`.

**Incident triage, the coder and the reviewer** are evaluated too: they are the agents that act
on production and write and approve code.

- **Triage** cases are kept for each incident (logs only for `general` data) and labeled with what
  fixed it. Replayed, a case passes when the first flag off or rollback triage proposes is one that
  fixed the incident. Any rise in wrong first actions fails the comparison: the bar is zero.
  Autonomous remediation also waits for triage's live record (shadow mode, `ops/remediation.py`).
- **Coder** cases are kept on each task's first attempt and labeled, when the change set merges,
  with the merge commit and the tests the pull request added or changed. Replayed from the merge
  commit's parent with the same instructions, the coder passes when those tests pass on its code
  (the repository's own test command, with no secret in its environment); a change that weakens a
  check (a skipped test, a lowered threshold) counts against it.
- **Reviewer** cases are replayed on the merged diff twice: as merged, where blocking it is a false
  alarm, and with one bug planted (a flipped condition, a removed assertion, a skipped test, a key in
  the code), where approving it is a miss. The comparison fails when the catch rate drops, or false
  alarms or weakened changes rise, by more than `max_drop`.
- **Intake** cases are kept at each submission: what the person said, in order, and the draft
  they confirmed. Replayed through a fresh intake, a case passes when the agent picks the person's
  product and size and puts no number in the success measure that the person never said. A rise
  in invented targets fails the comparison.
- **Prompt injection.** Every comparison, the config gate's included, also runs a fixed suite of
  synthetic cases (`valxel_sdlc/injection.py`) that plant an instruction where an agent reads
  outside text: a code comment and a pull request description (reviewer), a monitoring signal and
  a log line (triage), a parent document (spec). Each asks for something visible in the reply
  (approve a change that removes an access check, roll back an unrelated component, add a marker
  word), so following it is a fact. The bar is zero: a candidate that follows any injection the
  config in use resisted fails. `--no-injection` leaves it out of a command-line run.
- **Coder and reviewer cases run only in a disposable runner**, because a replay runs model-written
  code against a clone of the product's repository: `VALXEL_EVAL_RUNNER=1 valxel eval run --phase
  coder --phase reviewer --config path/to/candidate/config`, as a one-off job with the tool's
  database (read-only is enough), model credentials and the GitHub App, which it uses for a
  read-only token per repository. No code is kept: a case holds the repository, the merge commit and
  test paths, and the clone is deleted when the replay ends. The config gate below replays the
  other phases and leaves these out. Each coder case is a full coding session: size `--limit` and
  `--budget` for it.

- **Pull requests to the config repo** (`VALXEL_CONFIG_REPO`, e.g. `valxel-platform/sdlc-config`): the
  GitHub App's `pull_request` webhook makes the tool replay the newest labelled cases (5 per phase,
  `VALXEL_EVAL_CASES_PER_PHASE`; up to `VALXEL_EVAL_BUDGET_USD`, 20 by default, per config) under
  the PR's config and its base, and post `valxel / evals` on the head commit. Make it a required
  check. The run happens where the cases and the model keys are, never in the config repo's CI.
  `GET /api/evals/runs/{id}` has the full comparison; `POST /api/evals/runs` starts one for the
  teams in `VALXEL_EVAL_APPROVERS`.
- **From the command line:** `valxel eval export --product FARM --out cases.jsonl` and
  `valxel eval run --config path/to/candidate/config [--baseline-config ...] [--pairwise]` (cases
  from the database, `DATABASE_URL`, or `--cases FILE`; exits 1 on a regression).
- **Data classes.** Only `general` cases are used unless you ask for others
  (`--data-class`, `VALXEL_EVAL_DATA_CLASSES`); a regulated case still reaches only the providers its
  data class allows.
- **Langfuse.** With `VALXEL_LANGFUSE_URL`, `VALXEL_LANGFUSE_PUBLIC_KEY` and
  `VALXEL_LANGFUSE_SECRET_KEY` (the keys of Langfuse's test project, ADR 0011: replays are
  trials, traced as `test`), each run writes one dataset per phase (`valxel/<phase>`), links the
  run's traces to the items and scores them; documents go to Langfuse only for `general` cases.

### Trying a config on some features first: the canary

A model, prompt or setting change that passed its evals can run on a share of new features before it
replaces the config in use. Point `VALXEL_CANARY_CONFIG_DIR` at the candidate config directory (laid
out like `config/`) and set `VALXEL_CANARY_PERCENT` (1 to 50). Each new feature is assigned once, by
its id, and keeps its config to the end; hotfixes, and lanes outside `VALXEL_CANARY_DATA_CLASSES`
(`general` by default), always use the config in use. A canary feature's agents, critic and
fallbacks all run under the candidate, and every model call, ledger row and trace carries its config
version, so the pilot report's **By configuration** table compares the canary with the rest on
first-time approvals, loops, the critic's false passes, fixes before and after merge, incidents,
cost and the human share. The feature page says when a feature is on the canary. To roll the change
out, merge it into the config and drop the canary; to stop it, set the percent to 0. The candidate
must define every role the config in use has, or the tool refuses to start.

## Predictable results

The pipeline reads agents' work by machine, so the shapes are fixed and every reply is checked:

- **Format contracts.** Each phase's agent is told exactly which sections and bullet forms its
  document needs (`agents/formats.py`): the ones the deterministic checks and parsers read.
  Everything else is free prose. A test keeps each contract in step with the offline mode's documents,
  which pass the checks.
- **Replies in a fixed shape.** The critic, the reviewer and the coder answer in JSON with a fixed
  schema (`agents/structured.py`). Claude Code enforces it as structured output, and every reply
  is validated whatever the runtime. A reply that doesn't fit is asked for once more with what was
  wrong, then counts as a failure. The coder's reply carries its own test run (command, passed,
  failed tests); CI on the pull request is the final word.
- **A checklist critic.** Each phase has yes/no criteria (`evaluators/rubrics.py`), each answered
  with a short quote from the document. The tool checks the quote is really in the document, so a
  "yes" can't be invented, and the document passes when every must-have item is met. The critic's
  overall 0-1 score is still recorded, with what the old `critic_threshold` would have decided,
  so the pilot can compare the two.
- **The reviewer approves or blocks.** A PR passes review when the reviewer approves with nothing
  blocking; its score is kept for reporting.
- **Retries revise.** When the checks or the critic send a document back, the agent gets its
  previous draft with the findings and returns only the sections it changes; the rest is kept as
  it was (`merge_sections`). It writes less, and what was already right stays right.
- **Documents in one call.** Discovery, spec, design and planning run on the generic runtime: one
  call each with the cached prompt layers. Only work that reads or changes a repo runs as a Claude
  Code session (architect, coder, reviewer).
- **Session caps.** Every Claude Code session has a turn cap and a dollar cap: the role's
  (`max_turns`, `max_call_usd` in `config/models.yaml`), and never more than what's left of the
  feature's budget. A runaway session stops itself, and the phase sees the failure.

## The pilot: measuring the 85/15 goal

Valxel's goal is agents doing 85% of the work and people 15%. A feature created with **Pilot
feature** ticked (intake, or `pilot: true` on `POST /api/features`) is measured for that, and the
trial rules run beside it in shadow: they decide nothing, and what they would have decided is
recorded next to what really happened.

**What's counted** (`pilot.py`):

| | Where it comes from |
| --- | --- |
| People's time | Minutes the web app times while a decision is open (gates, escalations, hand-backs); a standard estimate per review (10 min) and review comment (2 min) people leave on the tool's pull requests, from GitHub webhooks; on-call's incident decisions, in the product totals |
| How long people were asked | When each question was first asked, and when it was answered |
| Why work came back | The reason picked when sending work back: something missing, wrong scope, wrong approach, unclear, quality, format, compliance, other |
| Agents' time and cost | Each model call's duration and cost from the usage ledger, by phase, model and runtime, with the cache hit rate. Each row carries a fingerprint of the settings that produced it (`config_version`: role models and runtimes, checklist critic, revising retries) |
| Agents' rework | Tries per phase, CI retries, escalations, first-time gate approvals |

Human share is people's minutes divided by people's and agents' minutes. An agent's minute isn't a
person's minute of work, so the report shows the counts beside the share (decisions, first-time
approvals, rework, escalations) rather than the share alone.

**Shadow modes** (pilot features only):
- **Automatic approval at G2, G3 and G4.** At each of those gates the tool records whether the
  trial rule would have approved without a person: general data, no dependency lane touched, the
  phase passed on its first try, and every critic checklist item met. The report shows how often it
  would have applied, the minutes it would have saved, and how often people sent back something it
  would have passed. G1 and G6 stay with people.
- **A cheaper critic and reviewer.** The `critic_shadow` and `reviewer_shadow` roles in
  `config/models.yaml` (Sonnet) judge the same documents and pull requests as the real ones; only
  whether they agree is kept. Their cost is tagged and reported apart from the feature's. Remove
  the roles to turn them off.
- **The old critic score.** Every checklist verdict records what the 0-1 score and
  `critic_threshold` would have decided.

**The report.** The web app's Pilot page, `GET /api/reports/pilot?product_id=FARM` (JSON, or
`&format=markdown`), or `valxel pilot-report FARM` against the tool's database.

**Running it.** Pick a product with general data and no shared lanes, and take 5 to 10 features of
different sizes through to production, ticking Pilot feature on each. Decide in the web app (so the
time is counted), pick a reason whenever you send work back, and review the tool's pull requests on
GitHub as usual. Then read the report: the human share against 15%, where people's time went, why
work came back, and the shadow modes' agreement, which decide the next changes (cheaper models for
low-risk work, automatic approval, learning from repeated corrections).

### How far the critic can be trusted, and what each configuration led to

The report judges the agents by what happened next, using data the pipeline already keeps
(`pilot.critic_outcomes`, `pilot.outcomes`):

- **Critic accuracy, per phase.** Each of the critic's verdicts on a document is matched with the
  people's next decision: passed and approved, passed and sent back (a false pass, with the reason
  people gave), failed until it escalated and people asked for more or rewrote it (the critic was
  right), or failed and people accepted the draft as it was (a false fail).
- **Outcomes, per feature.** Failures the agents fixed before the merge (in CI or the change set's
  own environment) and after it (build, dev, staging), delivery escalations, a traceability
  override, a stopped rollout, and production incidents on what the feature changed.
- **By configuration.** Every model call carries the config version (models, runtimes, critic and
  retry settings). The report groups features by the version most of their calls ran under, with
  first-time approvals, loops, the critic's false-pass rate, fixes before and after merge,
  incidents, cost and the human share, so a model or prompt change is judged on results.

## How the design maps to code

| Design element | Where | Notes |
| --- | --- | --- |
| Supervisor, phases, gates | `graph/pipeline.py` (wiring), `graph/gates.py`, `graph/routing.py` | Gates and escalations are `interrupt()` nodes; Design and Architecture run in parallel and join before Plan |
| Phase subgraphs | `graph/phase.py` | Each phase is a compiled subgraph: produce, deterministic checks, critic, loop up to `max_loops`, else escalate |
| Build fan-out | `graph/phase.py` | One coder agent per task through `Send`; a reviewer agent per PR; only failing tasks are retried |
| Agent roster and skills | `agents/roles.py` | Plugin skills per phase; critic skills double as evaluators |
| Performance tests | `performance.py`, `config/quality.yaml` | Budgets over industry defaults, the rules that pick none, smoke or full per repo, the PR labels and the staging request |
| Takeover and lessons | `graph/gates.py`, `lessons.py` | Gate history and revision limits; the person's version kept on hand-back; corrections recorded, lessons distilled and proposed as governance PRs, then pinned per feature into prompts and the critic's checklist |
| Fixing failures before production | `graph/merge_train.py` (`fix_or_ask`, `failing_repos`) | Failures in CI, the build, dev and staging go back to the coders of the repos they point at, on a fix branch after merge; people decide when the tries run out |
| Workflow profiles | `profiles.py`, `graph/briefs.py` | Feature and express routes; the profile brief, the gates a profile decides, and the guard that keeps architecture, G5 and G6 |
| Agent notes | `diary.py`, `graph/phase.py` | The `## Agent notes` section and the coder's notes, kept per phase and shown at the gates and in PR descriptions |
| Traceability | `traceability.py` | Metrics, goals, criteria, tasks, PRs, acceptance results, deployments and incidents in one view, with gaps; reverse lookup by repo or PR |
| Pilot | `pilot.py` | People's effort, the shadow automatic-approval rule, and the report against the 15% target |
| Evaluators | `evaluators/` | Deterministic checks run first; the checklist critic runs in a fresh context (judge independence, tier 1) and its quotes are verified against the document |
| Model gateway | `gateway/gateway.py` | Role-to-model registry (`config/models.yaml`), data-class allowlist (VX-7), one ledger record per call |
| Cache-first prompt layout | `gateway/prompt.py` | Six layers, breakpoints after L2, L4 and L5, chained prefix hashes recorded on every call |
| Usage ledger | `gateway/ledger.py` | In-memory or Postgres; `summarize()` gives cache-hit rate, write share, cost by phase and model |
| Runtimes | `runtime/` | `GatewayRuntime` (generic, any model) and `ClaudeCodeRuntime` (Claude Agent SDK, live mode) |
| Product isolation | `config/registry.yaml`, each org's `product.yaml` | Registry: lane kind, org, data class, workspace. Manifest: repos, approvers, budget, thresholds, high-risk paths, runtime, `depends_on` |
| Shared services | `governance/loader.py`, `graph/gates.py` | Dependency lanes pinned at intake; their constitutions apply; G4 to G6 need each touched lane; shared repos publish packages (`integrations/packages.py`) |
| Constitutions | `governance/` | Read from the tools org and each product org, validated, pinned by commit SHA per feature |
| Risk-tiered merge | `graph/phase.py`, `graph/merge_train.py` | PHI and payments are `regulated`; changes to high-risk paths, dependency manifests or files coding agents read as instructions (`CLAUDE.md`, `.claude/`, `AGENTS.md`, `.mcp.json`, Cursor and Copilot rules: planned, or in what the PRs changed) are `high`; both go through G5 |
| Multi-repo change set | `graph/merge_train.py`, `graph/delivery.py` | One branch name across repos; the merge train runs libraries, then providers, then consumers |
| CI/CD | `integrations/openchoreo.py` | Agents promote to staging only; production promotion at G6 is human (VX-3). A production rollback is a person's, or the tool's under the remediation policy |
| Knowledge write-back | `graph/phase.py` | After Operate, the capability is registered in the catalog |
| Durability | `api/app.py` | `PostgresSaver` checkpointer; threads survive restarts and wait at gates for days; after each run a thread keeps only its latest checkpoint (`engine.py` `_prune`). A regulated lane's (PHI, payments) history is archived first to `VALXEL_CHECKPOINT_ARCHIVE_BUCKET` under `checkpoints/<workspace>/<product>/` (KMS with the workspace and product in each file's encryption context, the same as tags a reader's workspace must match, Object Lock compliance mode for `VALXEL_CHECKPOINT_ARCHIVE_RETENTION_DAYS`, default 2190), or kept unpruned when it can't be (`checkpoint_archive.py`) |
| Replicas | `cluster.py`, `events.py`, `intake.py` | Advisory lock per thread, the elected watcher, claims for GitHub requests, shared notes; live updates over NOTIFY; intake chats in Postgres |

## Decisions

The web interface is the usual way to decide. Over the API, every pending item in `/api/inbox` carries an `interrupt_id` and its `allowed` decisions.

| Item | Decisions | Effect |
| --- | --- | --- |
| Gate (G1 to G6) | `approve` · `revise` (with `feedback`) · `takeover` · `abort` | Revise re-runs the phase with the feedback (not offered after `max_gate_revisions`). Takeover parks the gate until a `handback` |
| Hand-back | `handback` (optionally `artifact_content`, the person's version) · `abort` | The person's version becomes the phase's document and counts as the gate approval |
| Escalation | `resolved` (optionally `artifact_content`) · `retry` (optionally `budget_usd`) · `abort` | Resolved stands in for that phase's gate approval |

Any of them can carry `reason`, and `remember: true` or `lesson` to propose a lesson from it.

Record `active_minutes` on each decision. The feature view sums them to track the human share of
effort against the 15% target.

**Approving several at once** (`POST /api/inbox/approve`) takes only low-risk gates (G1, G2, G3 or
G5; general data; critic passed; nothing new created; never sent back) that the approver has read:
the web app reports the time a decision is open and in view (`POST .../decisions/{id}/reading`,
counted per person and never faster than time passes), and a gate joins a batch after
`VALXEL_BATCH_MIN_READ_S` (20) seconds of it, or at once when the feature runs the express profile.
Every gate decision records `viewed_s`, the approver's reading time, and `batch` when approved with
others; the pilot report's `approvals_read` counts approvals and those under the minimum, so
rubber-stamping shows against the 85/15 goal.

**G3 and G4 are decided as a pair.** They wait in the same graph step, so a `revise` at one takes
effect once the other is also decided. An earlier approval of the other gate still counts.

## Changing the schema

Every release has to run with the database the next one leaves behind, so that reverting an image
is the whole rollback ([ADR 0012](adr/0012-every-release-rolls-back-one-image.md)). The tool
brings its schema up to date as it starts, so:

- Add, don't change: new tables, columns that allow NULL or have a default, indexes, all with
  `IF NOT EXISTS`. New fields in stored JSON are optional, and code reads past ones it doesn't know.
- Drop, rename, retype or tighten something only in a later release than the one that stopped
  using it, and list the statement in `config/schema-contractions.yaml` with that release.
- `tests/test_schema_rollback.py` fails on anything else in `src/`.


```
config/            knowledge.yaml (design knowledge sources), repos.yaml (golden path for new repos), quality.yaml (test and security suites, floors per data class), tools.yaml (MCP servers, tools allowed per data class), registry.yaml (governance repo per product, platform limits), models.yaml (role registry)
examples/governance/  sample governance repos for the tools org and each product org
src/valxel_sdlc/
  governance/      sources (local, GitHub), GitHub App tokens, manifest validation, pinned loader
  graph/           pipeline.py wires the graph; routing, briefs, gates, provision, infra, merge_train,
                   delivery and rollout hold its nodes; phase.py the phase subgraphs
  agents/          roster, prompt-layer builder, offline templates, section parsers (parse.py: tolerant of
                   '*', '1.', bold ids, typographic arrows and heading variants)
  evaluators/      deterministic checks, rubric critic
  gateway/         gateway, providers, prompt layers, pricing, ledger
  runtime/         generic and Claude Code runtimes
  integrations/    GitHub, OpenChoreo, capability catalog, MCP client, diagrams, Jira, OpenProject
  engine.py        start, inbox, decide, view; runs threads one replica at a time
  engine_parts/    the engine's other areas: watching (waits, the watcher), roadmap, operations, costs
  waits.py         checks CI, builds and deployments for waiting features
  provisioning.py  new repos: standards, proposal checks, provisioner
  repo_roles.py    repo roles (library, provider, event-worker, mcp-server, agent, consumer, bff, acceptance) and merge order
  acceptance.py    product acceptance tests: plan checks, overlap, test selection, run reports
  quality/         test and security suites: policy, sources, gate
  knowledge/       design knowledge: architecture record, contracts, design check, write-back
  worksync.py      keeps the work tracker in step with each feature
  api/app.py       FastAPI service under /api; serves web/dist
  api/context.py   what every route group shares: the engine, sign-in, who may do what
  api/routers/     one module per route group (auth, signals, features, workspaces, intake, ...)
  api/models.py    request bodies
  web/auth.py      GitHub OAuth, signed sessions, approver matching
  intake.py        guided intake agent
  duplicates.py    finds features and capabilities a new request repeats
  roadmap/         goals, initiatives (G0) and releases: record, workflows, views
  ops/             production signals, incidents, hotfixes and intent changes
  events.py        live-update bus
  cli.py           valxel demo
web/               React app: decisions, features, intake
tests/             pipeline, gateway, governance, API, Postgres durability
```

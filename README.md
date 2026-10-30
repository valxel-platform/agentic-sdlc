# Vrixel agentic SDLC

A LangGraph implementation of the Vrixel agentic SDLC design. Agents built from the Product,
Design and Engineering plugin skills take a feature from idea to production. Humans decide at
six gates, and an escalation path takes over a phase when agents are stuck.

This release wires the **whole pipeline end to end with stub agents**: all eight phases, all six
gates, evaluator loops, escalations, the model gateway, the usage ledger and product
constitutions. It runs offline and deterministically. Each stub sits behind an interface you
replace with the real integration (see [Replacing the stubs](#replacing-the-stubs)).

```
discovery ─G1─ spec ─G2─┬─ design ───────G3─┬─ join ─ plan ─ build ─[G5 if high risk]─ merge train ─ release ─G6─ operate
                        └─ architecture ─G4─┘
any phase ── 3 failed evaluator loops or budget spent ──> escalation (human takeover)
```

## Quick start

### Docker compose (Postgres-backed, durable threads)

```bash
docker compose up --build
open http://localhost:8080/docs        # interactive API
```

Walk one feature through the gates with curl:

```bash
curl -s -X POST localhost:8080/features -H 'content-type: application/json' \
  -d '{"product_id":"EMR","title":"Consent check on intake","description":"Block intake until consent is recorded"}'

curl -s localhost:8080/inbox            # pending gates and escalations, all features

curl -s -X POST localhost:8080/features/EMR-FEAT-1/decisions/<interrupt_id> \
  -H 'content-type: application/json' \
  -d '{"decision":"approve","approver":"emr-product-owner","active_minutes":5}'

curl -s localhost:8080/features/EMR-FEAT-1          # status, artifacts, PRs, cost, timeline
curl -s localhost:8080/features/EMR-FEAT-1/usage    # one ledger record per model call
```

### Without Docker

```bash
pip install -e ".[dev]"
pytest                                   # 25 tests in memory; set VRIXEL_TEST_DATABASE_URL for the Postgres test
vrixel demo --product EMR --fault spec=1 --revise
uvicorn vrixel_sdlc.api.app:app --reload # in-memory unless DATABASE_URL is set
```

`vrixel demo` runs one feature through every phase, auto-approving gates. `--fault spec=1` makes
the first PRD defective, so the evaluator loop fixes it, and `--revise` sends the design back once
at G3.

## How the design maps to code

| Design element | Where | Notes |
| --- | --- | --- |
| Supervisor, phases, gates | `graph/pipeline.py` | Gates and escalations are `interrupt()` nodes; Design and Architecture run in parallel and join before Plan |
| Phase subgraphs | `graph/phase.py` | Each phase is a compiled subgraph: produce, deterministic checks, critic, loop up to `max_loops`, else escalate |
| Build fan-out | `graph/phase.py` | One coder agent per task through `Send`; a reviewer agent per PR; only failing tasks are retried |
| Agent roster and skills | `agents/roles.py` | Plugin skills per phase; critic skills double as evaluators |
| Evaluators | `evaluators/` | Deterministic checks run first; the critic runs in a fresh context (judge independence, tier 1) |
| Model gateway | `gateway/gateway.py` | Role-to-model registry (`config/models.yaml`), data-class allowlist (VX-7), one ledger record per call |
| Cache-first prompt layout | `gateway/prompt.py` | Six layers, breakpoints after L2, L4 and L5, chained prefix hashes recorded on every call |
| Usage ledger | `gateway/ledger.py` | In-memory or Postgres; `summarize()` gives cache-hit rate, write share, cost by phase and model |
| Runtimes | `runtime/` | `GatewayRuntime` (generic, any model) and `ClaudeCodeRuntime` (Claude Agent SDK, live mode) |
| Product isolation | `config/products.yaml` | Per product: repos, data class, API workspace, budget, thresholds, gate approvers, high-risk paths |
| Constitutions | `constitutions/` | Platform plus product, versioned; the version is stamped on every artifact |
| Risk-tiered merge | `graph/phase.py`, `graph/pipeline.py` | PHI and payments are `regulated`; changes to high-risk paths are `high`; both go through G5 |
| Multi-repo change set | `graph/pipeline.py` | One branch name across repos; the merge train runs libraries, then providers, then consumers |
| CI/CD | `integrations/openchoreo.py` | Agents promote to staging only; production promotion at G6 is human (VX-3) |
| Knowledge write-back | `graph/phase.py` | After Operate, the capability is registered in the catalog |
| Durability | `api/app.py` | `PostgresSaver` checkpointer; threads survive restarts and wait at gates for days |

## Decisions

Every pending item in `/inbox` carries an `interrupt_id` and its `allowed` decisions.

| Item | Decisions | Effect |
| --- | --- | --- |
| Gate (G1 to G6) | `approve` · `revise` (with `feedback`) · `takeover` · `abort` | Revise re-runs the phase with the feedback. Takeover parks the gate until a `handback` |
| Escalation | `resolved` (optionally `artifact_content`) · `retry` (optionally `budget_usd`) · `abort` | Resolved stands in for that phase's gate approval |

Record `active_minutes` on each decision. The feature view sums them to track the human share of
effort against the 15% target.

**G3 and G4 are decided as a pair.** They wait in the same graph step, so a `revise` at one takes
effect once the other is also decided. An earlier approval of the other gate still counts.

## Modes

- **stub** (default): `StubProvider` returns each phase's deterministic output and simulates prompt
  caching (exact-prefix match at breakpoints, TTL refreshed on hits, isolated per workspace). Use
  `simulate_faults` on intake, for example `{"spec": 1, "build": 1}`, to exercise loops and
  escalations.
- **live**: `pip install -e ".[live]"`, then set `VRIXEL_MODE=live` and `ANTHROPIC_API_KEY`. Roles
  with `runtime: claude_code` run through the Claude Agent SDK; others call the Claude API through
  the gateway. Set `VRIXEL_SKILLS_DIR` to a folder of installed plugins
  (`<plugin>/skills/<skill>/SKILL.md`) to load the real skills. Live mode is not covered by the
  offline tests. Run it against a sandbox product first.

## Replacing the stubs

| Stub | Replace with | Interface |
| --- | --- | --- |
| `StubGitHub` | A GitHub App with repo-scoped tokens: open PRs, required checks, merge | `integrations/github.py` |
| `StubOpenChoreo` | The OpenChoreo API: WorkflowRuns, DeploymentPipeline promotion, a watcher that resumes threads | `integrations/openchoreo.py` |
| `InMemoryCatalog` | The Backstage catalog in the OpenChoreo portal | `integrations/catalog.py` |
| Phase templates | Real agent output in live mode; keep the section structure the checks read | `agents/templates.py` |
| Build tasks | Coder agents in sandboxed containers with a checkout of one repo each | `runtime/claude_code.py` |

Open items carried over from the design: confirm OpenChoreo GitHub status reporting, RBAC that
limits production promotion to humans, and per-change-set integration environments. Have
compliance counsel review the regulatory constitution articles.

## Layout

```
config/            products.yaml (product lanes), models.yaml (role registry, data-class allowlist)
constitutions/     platform plus one per product
src/vrixel_sdlc/
  graph/           pipeline (supervisor, gates, escalations, merge train), phase subgraphs
  agents/          roster, prompt-layer builder, stub templates, section parsers
  evaluators/      deterministic checks, rubric critic
  gateway/         gateway, providers, prompt layers, pricing, ledger
  runtime/         generic and Claude Code runtimes
  integrations/    GitHub, OpenChoreo, capability catalog
  engine.py        start, inbox, decide, view
  api/app.py       FastAPI service
  cli.py           vrixel demo
tests/             pipeline, gateway, API, Postgres durability
```

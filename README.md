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
pytest                                   # 38 tests in memory; set VRIXEL_TEST_DATABASE_URL for the Postgres test
vrixel demo --product EMR --fault spec=1 --revise
uvicorn vrixel_sdlc.api.app:app --reload # in-memory unless DATABASE_URL is set
```

`vrixel demo` runs one feature through every phase, auto-approving gates. `--fault spec=1` makes
the first PRD defective, so the evaluator loop fixes it, and `--revise` sends the design back once
at G3.

## Organizations and product governance

The tool lives in the tools org. It holds no product constitutions or product settings: each
product owns a governance repo in its own GitHub organization, and the tool reads it.

```
vrixel-tools/                          tools org
  agentic-sdlc                         this repo: registry.yaml points at every governance repo
  sdlc-governance                      platform/constitution.md (VX- articles)
vrixel-emr/                            one org per product (names here are placeholders)
  governance                           product.yaml · constitution.md · context.md · CODEOWNERS
  emr-intake-api, emr-web, ...         product code; PRs are opened here
vrixel-agtech/ · vrixel-pos/ · vrixel-medspa/ · vrixel-apx/   same layout
```

**What the tool keeps** (`config/registry.yaml`): for each product, the org and governance repo
to read, its data class (which decides the model providers that may see its data) and its API
workspace. It also keeps the platform limits a product may tighten but never loosen.

**What each product org keeps** (its `governance` repo):

| File | Contents | Read into |
| --- | --- | --- |
| `product.yaml` | Product id and name, repos (`<org>/<repo>` with role), gate approvers, budget, critic threshold, loop cap, high-risk paths | Product lane: routing, G5 risk tier, gate packets, budgets |
| `constitution.md` | Product articles, versioned in front matter | L3 prompt layer for every agent and critic; stamped on every artifact |
| `context.md` (optional) | Product glossary, architecture summary, conventions | L4 prompt layer |
| `CODEOWNERS` | Product owners | Amendments are PRs the product owner approves |

`examples/governance/` holds one sample per org. Copy each folder into its org as the starting
`governance` repo.

**Validation.** At feature intake the tool rejects a manifest or constitution that:
- names a different `product_id` than the registry
- changes its data class
- lowers the critic threshold below the platform minimum
- raises the loop cap or budget above the platform maximum
- leaves a gate without approvers
- lists a repo outside its own org
- redefines a platform `VX-` article

A product whose governance repo is unreachable or invalid can't start features. `GET /products`
shows each product's governance status.

**Pinning.** At intake the tool resolves the platform and product refs (default `main`) to commit
SHAs and stores them on the feature thread. Every phase, gate and critic reads governance at
those SHAs, so a feature runs under one constitution from intake to release. Amendments merged
while it runs apply to the next feature. `constitution_version` records both commits, for example
`platform@1.0.0#ba05394+emr@1.0.0#a1b2c3d`.

**Access.** Set `VRIXEL_GOVERNANCE_SOURCE=github` and choose one of these:
- *GitHub App (recommended).* Install one app in the tools org and every product org, with
  read access to Contents. Set `VRIXEL_GITHUB_APP_ID` and `VRIXEL_GITHUB_APP_PRIVATE_KEY_FILE`.
  The tool mints a short-lived `contents:read` token per org.
- *Tokens.* `VRIXEL_GITHUB_TOKEN__<ORG>` per org (upper case, `-` as `_`), or one
  `VRIXEL_GITHUB_TOKEN`.

The same app can later open PRs in product repos, scoped per org.

**Adding a product:**
1. Create its org and a `governance` repo from an example.
2. Install the GitHub App in the org.
3. Add one line to `config/registry.yaml`.

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
| Product isolation | `config/registry.yaml`, each org's `product.yaml` | Registry: org, data class, workspace. Manifest: repos, approvers, budget, thresholds, high-risk paths |
| Constitutions | `governance/` | Read from the tools org and each product org, validated, pinned by commit SHA per feature |
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
config/            registry.yaml (governance repo per product, platform limits), models.yaml (role registry)
examples/governance/  sample governance repos for the tools org and each product org
src/vrixel_sdlc/
  governance/      sources (local, GitHub), GitHub App tokens, manifest validation, pinned loader
  graph/           pipeline (supervisor, gates, escalations, merge train), phase subgraphs
  agents/          roster, prompt-layer builder, stub templates, section parsers
  evaluators/      deterministic checks, rubric critic
  gateway/         gateway, providers, prompt layers, pricing, ledger
  runtime/         generic and Claude Code runtimes
  integrations/    GitHub, OpenChoreo, capability catalog
  engine.py        start, inbox, decide, view
  api/app.py       FastAPI service
  cli.py           vrixel demo
tests/             pipeline, gateway, governance, API, Postgres durability
```

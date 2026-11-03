# Vrixel agentic SDLC

A LangGraph implementation of the Vrixel agentic SDLC design. Agents built from the Product,
Design and Engineering plugin skills take a feature from idea to production. Humans decide at
six gates, and an escalation path takes over a phase when agents are stuck.

This release wires the **whole pipeline end to end with stub agents**: all eight phases, all six
gates, evaluator loops, escalations, the model gateway, the usage ledger and product
constitutions. It runs offline and deterministically. Each stub sits behind an interface you
replace with the real integration (see [Replacing the stubs](#replacing-the-stubs)).

```
discovery ─G1─ spec ─G2─┬─ design ───────G3─┬─ join ─ provision new repos ─ plan ─ build ─ CI ─[G5 if high risk]─ merge train
                        └─ architecture ─G4─┘    ─ build, deploy dev and staging ─ release ─G6─ production ─ operate
any phase ── 3 failed evaluator loops or budget spent ──> escalation (human takeover)
```

## Quick start

### Docker compose (Postgres-backed, durable threads)

```bash
docker compose up --build
open http://localhost:8080             # web interface
open http://localhost:8080/docs        # interactive API
```

Compose starts with development sign-in (`VRIXEL_AUTH=dev`): type any GitHub login and tick the
teams to act as, for example `vrixel-emr/product-owners`. Use GitHub sign-in for anything shared
(see [Web interface](#web-interface)).

### Without Docker

```bash
pip install -e ".[dev]"
pytest                                   # in memory; set VRIXEL_TEST_DATABASE_URL for the Postgres test
vrixel demo --product EMR --fault spec=1 --revise

(cd web && npm ci && npm run build)      # build the web interface into web/dist
VRIXEL_AUTH=dev VRIXEL_SESSION_SECRET=$(openssl rand -hex 32) \
  uvicorn --factory vrixel_sdlc.api.app:create_app --port 8080 --reload
```

For frontend work, run the API as above and `cd web && npm run dev`. Vite serves the app on
port 5173 and proxies `/api` to port 8080.

`vrixel demo` runs one feature through every phase, auto-approving gates. `--fault spec=1` makes
the first PRD defective, so the evaluator loop fixes it, and `--revise` sends the design back once
at G3.

## Web interface

Three screens, all updated live over server-sent events (`/api/events`):

- **Decisions** lists the gates and escalations waiting on you, or on everyone. Each opens with
  the artifact under review, the evaluator score, attempts and spend, and the decisions you can
  take: approve, request changes (with feedback), take over, or stop. Escalations offer mark
  resolved, let agents retry, or stop.
- **Features** shows every feature as a row on the pipeline rail, grouped by product, with what
  it is waiting for. A feature's page adds the timeline, artifacts, pull requests, decisions and
  cost.
- **New intent** is a chat with the intake agent. It asks for the product, the problem, the users
  and a success measure, drafts the intent alongside the conversation, and checks the capability
  catalog for features it extends. You can edit the draft directly and submit it when it is
  complete. Submitting starts discovery.

### Sign-in and who can decide

GitHub sign-in uses an OAuth app owned by `vrixel-tools`:

1. In the org's settings, open Developer settings, OAuth Apps, New OAuth App. Set the callback
   URL to `<VRIXEL_PUBLIC_URL>/api/auth/callback`.
2. Set `VRIXEL_AUTH=github`, `VRIXEL_GITHUB_OAUTH_CLIENT_ID`, `VRIXEL_GITHUB_OAUTH_CLIENT_SECRET`,
   `VRIXEL_PUBLIC_URL` and a random `VRIXEL_SESSION_SECRET` of at least 32 characters.
3. Each product org approves the OAuth app (org settings, Third-party access) so team membership
   is visible.

The app requests `read:org` only to read the user's orgs and teams. It keeps them in a signed,
HttpOnly session cookie and discards the GitHub token.

- **Submitting an intent** requires membership of the product's GitHub org.
- **Deciding a gate** requires matching one of the gate's approvers in that product's
  `governance/product.yaml`. An approver is a team (`vrixel-emr/product-owners`) or a login.
  Cross-lane gates (G4, G5, G6) ask each touched lane in turn, using that lane's approvers.
- **Resolving an escalation** uses the approvers of the gate that follows the stuck phase.

The API records the signed-in login as the approver; a request body cannot override it.

Run one replica. Live updates and intake drafts are held in process. Pipeline state is durable in
Postgres, so a restart loses only open intake chats.

## Organizations and product governance

The tool lives in the tools org. It holds no product constitutions or product settings: each
product owns a governance repo in its own GitHub organization, and the tool reads it.

```
vrixel-tools/                          tools org
  agentic-sdlc                         this repo: registry.yaml points at every governance repo
  sdlc-governance                      platform/constitution.md (VX- articles)
vrixel-emr/                            one org per product; each owns its runtime
  governance                           product.yaml · constitution.md · context.md · CODEOWNERS
  emr-intake-api, emr-web, ...         product code; PRs are opened here
vrixel-agtech/ · vrixel-pos/ · vrixel-medspa/ · vrixel-apx/   same layout
vrixel-shared-services/                shared lane: communication, audit framework, security
  governance                           same files, but no runtime
  ss-communication, ss-audit-framework, ss-security   libraries and frameworks
```

### Lanes: products and shared services

Every org is a **lane** in `config/registry.yaml`, with a `kind`:

- **`product`** lanes own their runtime. `product.yaml` declares `runtime.openchoreo_project`
  and its environment pipeline, and their services deploy there.
- **`shared`** lanes (`vrixel-shared-services`) own no runtime and ship versioned libraries and
  frameworks. Each consuming product runs them in its own runtime. A shared manifest that
  declares a runtime is rejected.

A product lists the lanes it builds on in `depends_on` (the examples use `[SS, APX]`). A lane
may depend on shared lanes; a shared lane may not depend on a product. The tool pins every
dependency's governance at intake along with the product's own. When a feature changes a
dependency's code:

- **Change set.** The architect may add the dependency's repos. Libraries merge first.
- **Constitutions.** The dependency's constitution is part of the feature's constitution and
  applies to changes in its repos. For example, `SS-6` puts every security change through G5.
- **Approvals.** G4, G5 and G6 need a decision from each lane the change set touches: the
  product owner, then each dependency owner. Either one can send the work back. At G5 each lane
  reviews only the PRs in its own org.
- **Risk.** The shared lane's data class is `phi` because it serves regulated products, so any
  change set touching shared code is regulated and goes through G5.
- **Release.**
  - *Shared repos* publish a release candidate on merge, and G6 marks it stable. Nothing deploys.
  - *Product repos* build in the product's own OpenChoreo project, and G6 promotes them to
    production there.

A feature started in the shared lane itself runs the same pipeline, but its release only
publishes packages. Consuming products adopt the new version through their own features.

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

## New repositories

When a feature needs a repo that doesn't exist, the pipeline creates it. The architect proposes
it in the HLD; G4 approves it with the architecture; the provision step creates it after G3 and
G4, before planning, with the platform's settings. CI, the quality suites, the merge train and
OpenChoreo delivery then work on it from its first pull request, with no manual setup.

```
## ADRs
- ADR-EMR-FEAT-4-2: Create vrixel-emr/emr-consent-registry-service: consent has its own data and release cadence
## New repos
- vrixel-emr/emr-consent-registry-service (provider): Owns patient consent records [ADR-EMR-FEAT-4-2]
```

**Checked before G4.** The repo must be in the product's own org, follow the naming rule (the
product's prefix, lowercase words joined by "-"), have a role with a template, not exist
already, and have an ADR that names it. G4 shows exactly what approval will create.

**Applied by the provision step** (`config/repos.yaml`, the golden path):

| Area | Setting |
| --- | --- |
| Source | Generated from the role's template (`vrixel-tools/template-service`, `-web`, `-library`, `-mcp-server`, `-agent`), private |
| Merging | Squash only, branches deleted after merge |
| Security | Dependency graph, Dependabot alerts and security updates, secret scanning with push protection, CodeQL default setup |
| Branch ruleset | Pull requests only; 1 approval (2 for PHI and payments); code owner review; stale approvals dismissed; conversations resolved; linear history; no force pushes or deletion; required checks per role; merges blocked on new code-scanning alerts of high or above |
| Ownership | CODEOWNERS from the gate approvers (tech leads own everything; security owns the high-risk paths) |
| Access | Teams from the approvers: tech leads maintain, architects and security push, product owners read |
| Labels | Topics, and org custom properties (`vrixel_managed`, `vrixel_product`, `vrixel_data_class`) that org-level rulesets can target |
| Runtime | An OpenChoreo component in the product's project (none for libraries), with auto-build and auto-deploy off so every change goes through the gates |
| Governance | A pull request to the product's governance repo adding the repo to `product.yaml` |

Until that governance PR merges, the feature carries its new repos itself, so its plan, PRs,
risk tier, builds and deployments include them. Every step is idempotent. If one fails, the
architects (G4 approvers) can retry, finish it by hand and carry on, or stop the feature.

Products tighten in `governance/product.yaml`: their own templates, extra required checks, more
approvals, broader team access. Lowering approvals or turning off a security feature is rejected.

```yaml
repo_standards:
  templates: { provider: vrixel-emr/template-fhir-service }
  extra_required_checks: [fhir-conformance]
  required_approvals: 3
```

**Setup once per org.**
1. Publish the templates named in `config/repos.yaml` to `vrixel-tools` and mark each one a
   template repository: `template-service` (Java 21, Spring Boot), `template-web` (React,
   TypeScript), `template-library` (Java), `template-mcp-server` and `template-agent` (Python).
   Their CI jobs are named for the required checks
   (`unit`, `contract`) and post `unit / report`-style statuses the quality suites read; their
   `post-deploy.yml` runs integration, e2e and smoke suites when the pipeline dispatches
   `vrixel-deployed` after a deployment is ready.
2. Create the custom properties `vrixel_managed`, `vrixel_product` and `vrixel_data_class` in each
   product org (optional, for org-level rulesets).
3. Install the GitHub App on all repositories of each product org (so it sees repos it creates)
   with administration, contents, pull requests, security events, secret scanning alerts,
   Dependabot alerts and custom properties (write), and members (read). The provision step asks
   for these in its own token; delivery tokens stay narrower.
4. The app is a ruleset bypass actor for pull requests only, so the merge train can merge once
   the pipeline's gates pass; people still can't push to the default branch.

## Agentic features in products

Products build their own agentic features with two more repo roles, so an HLD can propose them
like any other repo and the provision step creates them on the golden path:

| Role | Template | Required checks | What it is |
| --- | --- | --- | --- |
| `mcp-server` | `vrixel-tools/template-mcp-server` | `unit`, `CodeQL` | Product capabilities as MCP tools over Streamable HTTP: OAuth scopes, annotated tools, idempotent writes, audit, PHI-minimal results |
| `agent` | `vrixel-tools/template-agent` | `unit`, `evals`, `CodeQL` | A durable LangGraph workflow agent: MCP tools on an allowlist, models through the product's LLM gateway, a person approves every change |

Roles merge and deploy in dependency order: library, provider, mcp-server, agent, consumer.
The `evals` quality suite reads the agent's `evals / report` status on every PR (recorded model
responses); after a deploy to dev the agent's post-deploy job runs the same evals against the
real model and reports `integration / report`. Platform articles VX-12 to VX-14 make these
rules: models only through the gateway, people approve changes, evals gate every PR.

Models are reached through `ss-llm-gateway`, a shared-services package each product deploys in
its own OpenChoreo project (runtime stays with the product). Its policy follows the product's
data class: PHI only to providers under a BAA, payments with card data blocked, per-caller keys,
budgets and model allowlists, and a usage ledger.

## Delivery: GitHub and OpenChoreo

From build to production, the pipeline works in the product's own GitHub org and OpenChoreo
project, and pauses on CI, builds and deployments the way it pauses on people:

```
join ─ provision ─ plan ─ build ─ ci_wait ─[G5]─ merge_train ─ build_wait ─ deploy_dev ─ dev_wait ─ deploy_staging ─ staging_wait
      ─ release ─ G6 (binds production for the approver) ─ production_wait ─ operate
```

| Step | GitHub | OpenChoreo |
| --- | --- | --- |
| Build | A change note per task (`.vrixel/changes/<feature>/<task>.md`) on `feature/<id>`; one PR per repo, found again by branch on retries | |
| ci_wait | Waits until every check run and status on each PR head is green | |
| merge_train | Merges in dependency order (libraries, providers, consumers); branch protection still applies | Triggers a WorkflowRun on each merge commit; shared-lane repos publish packages instead |
| build_wait | Posts `openchoreo/build` on the merge commit | Waits for the WorkflowRuns |
| deploy and wait | Posts `openchoreo/dev`, `openchoreo/staging`, `openchoreo/production` | Binds the latest release to the first environment, then promotes the same release; waits until each ReleaseBinding is ready |

Waits are interrupts with `type: "external"`. They never reach the decision inbox. The engine
checks them after every step, on a timer (`VRIXEL_WATCH_INTERVAL_S`), and when GitHub sends a
webhook. When one fails or times out:
- **Red CI** sends the build back to its escalation: let agents retry, or vouch for it.
- **A failed build or deployment** asks the release managers (G6 approvers) to try again, carry
  on anyway, or stop. Retrying production means asking for G6 again.

Agents bind only dev and staging (VX-3). Production is bound after G6, recorded under the
approver's name, and OpenChoreo's own authorization is the backstop.

**Product runtime** (`governance/product.yaml`):

```yaml
runtime:
  openchoreo_project: agtech
  pipeline: [dev, staging, production]   # OpenChoreo environment names, first to last
  namespace: vrixel                      # optional; defaults to config/tools.yaml
  components:                            # optional; defaults to the repo name
    vrixel-agtech/agtech-mobile: field-app
```

**Going live** (`VRIXEL_DELIVERY=live`):
1. Grant the GitHub App, in each product org: contents, pull requests and commit statuses
   (write), checks (read). Subscribe it to check runs, check suites, statuses and pull requests,
   with the webhook URL `<VRIXEL_PUBLIC_URL>/api/webhooks/github` and `VRIXEL_GITHUB_WEBHOOK_SECRET`.
2. In OpenChoreo, give the confidential service client (`service_mcp_client` by default) a role
   that can trigger workflow runs and manage release bindings in each product's project. Set
   `VRIXEL_OPENCHOREO_MCP_URL` (`<api host>/mcp`), `VRIXEL_OPENCHOREO_TOKEN_URL` and
   `VRIXEL_OPENCHOREO_CLIENT_SECRET`.
3. Create each product's OpenChoreo project, components and deployment pipeline to match
   `runtime`. Try a sandbox product first: with stub agents, the change notes alone make real PRs.

The adapters match tool and argument names against OpenChoreo's published tool schemas, and
the startup check names any missing setting.

## Tests and security scans

Testing and security are policy, not code. `config/quality.yaml` lists **suites** (what must be
verified, at which stage, how strictly). Each suite names a **source** (where its results are
read from) and a `match` (which tool or check counts). The pipeline evaluates the suites of a
stage when that stage's wait finishes, and records the results as evidence.

**Recommended profile.** A common industry baseline, following the test pyramid and the OWASP
SAMM and NIST SSDF practices:

| Stage | Suite | Recommended tool | Read from | Default rule |
| --- | --- | --- | --- | --- |
| Pull request | Unit tests | Your test runner, reporting counts and coverage | Check runs `unit*`, `test` | Required, line coverage 90% for every data class (VX-11) |
| Pull request | Contract tests | Pact | Check runs `contract*`, `pact*` | Optional; required where a lane says so |
| Pull request | Code scanning (SAST) | CodeQL, or Semgrep (SARIF) | GitHub code scanning | Required, blocks high and above |
| Pull request | Dependency review (SCA) | GitHub dependency review, Dependabot | Dependency diff of the PR | Required, blocks high (general may relax to critical) |
| Pull request | Secret scanning | GitHub secret scanning with push protection | Secret-scanning alerts | Required, any exposed secret blocks |
| Pull request | Infrastructure as code | Trivy or Checkov (SARIF) | GitHub code scanning | Optional, blocks high |
| Build | Image scan | Trivy step in the OpenChoreo build workflow | OpenChoreo build steps | Required for PHI and payments |
| Dev | Integration tests | The change set, all repos together | Check runs `integration*` on the merge commit | Required |
| Staging | End-to-end tests | Playwright | Check runs `e2e*`, `playwright*` | Required |
| Staging | Dynamic scan (DAST) | OWASP ZAP baseline | Check runs `zap*`, `dast*` | Required for PHI and payments, blocks high |
| Production | Smoke tests | A post-deploy job | Check runs `smoke*` | Required |

Tool output reaches the pipeline through two open formats: check runs (any CI) and SARIF in
GitHub code scanning (any scanner). That is what keeps tools swappable.

**What happens on a result**
- **Failed tests, low coverage, or a required suite that never reports:** before merge, the build
  goes back to its escalation (agents fix, or a person vouches). After merge, the release
  managers decide.
- **Findings at or above `block_on`:** the lane's security approvers decide (`approvers.security`
  in `product.yaml`, else the G5 approvers). They can have agents fix them (before merge; agents
  get the findings as feedback), scan again (after merge), accept the risk, or stop the feature.
- **Accepting a risk** creates a waiver for each finding. It records the reason (required), the
  approver and an expiry of at most 90 days. Waived findings stop blocking until the waiver
  expires; they are listed under "Accepted risks" in the release document and on the G6 panel.
- **G5 and G6** show every suite run so far. The release document carries the same evidence for
  audit.

**Changing it later, without rewriting anything**

| Change | Where |
| --- | --- |
| Swap a tool (Semgrep for CodeQL, Vitest for Jest, Snyk for Dependabot) | `match` of the suite: tool names or check names |
| Tighten a product (coverage 95%, block on medium, make DAST required) | `quality:` in the product's `governance/product.yaml` |
| Add a suite (license scan, performance budget, accessibility) | A new entry with `kind`, `stage`, `source`, `match` |
| Raise or lower a floor for a data class | `floors` in `config/quality.yaml` (tools org) |
| A tool that reports somewhere new (a SonarQube API, a test-results bucket) | One class with `collect()` in `quality/sources.py` or your own package, registered under `sources` |

Products can only tighten: the platform rejects a manifest that lowers coverage, raises a
blocking severity, or makes a required suite optional for its data class.

```yaml
# governance/product.yaml
approvers:
  security: [vrixel-emr/security]
quality:
  sast: { match: { tools: [CodeQL, Semgrep] }, block_on: medium }
  unit: { match: { checks: ["vitest*"] }, min_coverage: 95 }
  license:
    kind: security
    stage: pr
    source: github-checks
    match: { checks: ["license*"] }
    required: true
    block_on: high
```

In stub delivery mode every suite passes with sample numbers; tests inject results through
`services.quality.sources["stub"].results`.

## Diagrams and work tracking

Each product chooses its tools in `governance/product.yaml`. The tools org runs the connections
and holds the credentials (`config/tools.yaml`), and limits which tools may receive content from
each data class:

```yaml
tools:
  diagrams:
    provider: lucid          # lucid | drawio
  work_tracking:
    provider: jira           # jira | openproject
    project: AG              # Jira project key or OpenProject project identifier
    # optional: epic_type, story_type, task_type, statuses: {in_progress, done, cancelled}
```

| Data class | Allowed by default | Note |
| --- | --- | --- |
| general | lucid, drawio, jira, openproject | |
| phi, payments | drawio, openproject | Add a SaaS tool once a vendor agreement (BAA, DPA) covers it |

**Architecture diagrams.** The architect writes one Mermaid flowchart in the HLD's `## Diagram`
section. The evaluator checks that it parses and shows every affected repo. When the phase
passes, the diagram is published and linked from the HLD, so G4 reviewers open it from the gate.

- **Lucid**: imported through the Lucid MCP server (`lucid_create_diagram_from_mermaid`),
  tagged with the feature ID, and shared view-only within the Lucid account.
- **draw.io**: converted to a native `.drawio` file and committed to the feature branch of the
  product's provider repo, so it is reviewed and merged with the code. The link opens it in the
  draw.io editor. draw.io's MCP servers run beside a person's editor and store nothing, so the
  pipeline writes the file itself.

If the change touches a lane whose data class doesn't allow the tool (an AgTech change to the
PHI shared-services lane, say), the diagram stays in the HLD and isn't published. A failed
publish never fails the phase.

**Work tracking.** After every step of a feature, the engine reconciles the product's Jira or
OpenProject project with the feature.

| When | In the tracker |
| --- | --- |
| G1 approved | Epic `[AG-FEAT-1] title`, linked to the feature page |
| G2 approved | One story per acceptance criterion, under the epic; epic in progress |
| Plan passed | One task per planned repo change; tasks in a restricted lane carry no detail |
| Build and merge | A task is in progress once it has a PR and done when merged; stories done at merge |
| Every decision | A comment on the epic with gate, decision, approver and feedback; diagram links too |
| Released or stopped | Epic done, or open items cancelled |

Sync is idempotent: each item carries a marker (a Jira label, an OpenProject subject prefix), so
a retry finds what it created. A tracker outage shows on the feature page and never stops agents
or gates; the next step, or **Sync now**, catches up.

Adapters resolve tool and argument names from each server's published tool schemas, so they keep
working when a server renames a parameter. Without credentials a provider runs as an offline
stand-in, and `/api/products` reports `mode: stub`. Unattended agents can't complete an OAuth
consent screen, so each server needs an API key or token issued to a service account.

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
| Product isolation | `config/registry.yaml`, each org's `product.yaml` | Registry: lane kind, org, data class, workspace. Manifest: repos, approvers, budget, thresholds, high-risk paths, runtime, `depends_on` |
| Shared services | `governance/loader.py`, `graph/pipeline.py` | Dependency lanes pinned at intake; their constitutions apply; G4 to G6 need each touched lane; shared repos publish packages (`integrations/packages.py`) |
| Constitutions | `governance/` | Read from the tools org and each product org, validated, pinned by commit SHA per feature |
| Risk-tiered merge | `graph/phase.py`, `graph/pipeline.py` | PHI and payments are `regulated`; changes to high-risk paths are `high`; both go through G5 |
| Multi-repo change set | `graph/pipeline.py` | One branch name across repos; the merge train runs libraries, then providers, then consumers |
| CI/CD | `integrations/openchoreo.py` | Agents promote to staging only; production promotion at G6 is human (VX-3) |
| Knowledge write-back | `graph/phase.py` | After Operate, the capability is registered in the catalog |
| Durability | `api/app.py` | `PostgresSaver` checkpointer; threads survive restarts and wait at gates for days |

## Decisions

The web interface is the usual way to decide. Over the API, every pending item in `/api/inbox` carries an `interrupt_id` and its `allowed` decisions.

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
| `InMemoryCatalog` | The Backstage catalog in the OpenChoreo portal | `integrations/catalog.py` |
| Phase templates | Real agent output in live mode; keep the section structure the checks read | `agents/templates.py` |
| Build tasks | Coder agents in sandboxed containers with a checkout of one repo each | `runtime/claude_code.py` |

Open items carried over from the design: per-change-set integration environments, and a
package registry for shared-lane releases (`PackageRegistry` is still a stub). Have
compliance counsel review the regulatory constitution articles.

## Layout

```
config/            repos.yaml (golden path for new repos), quality.yaml (test and security suites, floors per data class), tools.yaml (MCP servers, tools allowed per data class), registry.yaml (governance repo per product, platform limits), models.yaml (role registry)
examples/governance/  sample governance repos for the tools org and each product org
src/vrixel_sdlc/
  governance/      sources (local, GitHub), GitHub App tokens, manifest validation, pinned loader
  graph/           pipeline (supervisor, gates, escalations, merge train), phase subgraphs
  agents/          roster, prompt-layer builder, stub templates, section parsers
  evaluators/      deterministic checks, rubric critic
  gateway/         gateway, providers, prompt layers, pricing, ledger
  runtime/         generic and Claude Code runtimes
  integrations/    GitHub, OpenChoreo, capability catalog, MCP client, diagrams, Jira, OpenProject
  engine.py        start, inbox, decide, view; resolves CI and OpenChoreo waits
  waits.py         checks CI, builds and deployments for waiting features
  provisioning.py  new repos: standards, proposal checks, provisioner
  repo_roles.py    repo roles (library, provider, mcp-server, agent, consumer) and merge order
  quality/         test and security suites: policy, sources, gate
  worksync.py      keeps the work tracker in step with each feature
  api/app.py       FastAPI service under /api; serves web/dist
  web/auth.py      GitHub OAuth, signed sessions, approver matching
  intake.py        guided intake agent
  events.py        live-update bus
  cli.py           vrixel demo
web/               React app: decisions, features, intake
tests/             pipeline, gateway, governance, API, Postgres durability
```

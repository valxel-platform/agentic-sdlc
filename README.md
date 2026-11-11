# Vrixel SDLC

Vrixel SDLC is an agentic software development lifecycle built on LangGraph. Agents built from
the Product, Design and Engineering plugin skills take a feature from idea to production. Humans
decide at six gates, and an escalation path takes over a phase when agents are stuck.

This release wires the **whole pipeline end to end with stub agents**: all eight phases, all six
gates, evaluator loops, escalations, the model gateway, the usage ledger and product
constitutions. It runs offline and deterministically. Each stub sits behind an interface you
replace with the real integration (see [Replacing the stubs](#replacing-the-stubs)).

**[Architecture](docs/architecture.md)**: how the parts fit together, how a feature and an
incident move through the tool, and the design rules.

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
teams to act as, for example `acme-clinic/product-owners`. Use GitHub sign-in for anything shared
(see [Web interface](#web-interface)).

### Without Docker

```bash
pip install -e ".[dev]"
pytest                                   # in memory; set VRIXEL_TEST_DATABASE_URL for the Postgres test
vrixel demo --product CLN --fault spec=1 --revise

(cd web && npm ci && npm run build)      # build the web interface into web/dist
VRIXEL_AUTH=dev VRIXEL_SESSION_SECRET=$(openssl rand -hex 32) \
  uvicorn --factory vrixel_sdlc.api.app:create_app --port 8080 --reload
```

For frontend work, run the API as above and `cd web && npm run dev`. Vite serves the app on
port 5173 and proxies `/api` to port 8080. To fill a fresh stub-mode server with features at different stages, run
`python web/scripts/seed.py` (it signs in as `demo` through development sign-in).

`vrixel demo` runs one feature through every phase, auto-approving gates. `--fault spec=1` makes
the first PRD defective, so the evaluator loop fixes it, and `--revise` sends the design back once
at G3.

## Web interface

Three screens, all updated live over server-sent events (`/api/events`):

- **Decisions** lists the gates and escalations waiting on you, or on everyone. Each opens with
  the artifact under review, the evaluator score, attempts and spend, and the decisions you can
  take: approve, request changes (with feedback), take over, or stop. Escalations offer to finish
  the document yourself, let agents retry, or stop. A phase sent back repeatedly shows what was
  asked before and suggests taking it over.
- **Features** shows every feature as a row on the pipeline rail, grouped by product, with what
  it is waiting for. A feature's page adds the timeline, artifacts, pull requests, decisions and
  cost.
- **New intent** is a chat with the intake agent. It asks for the product, the problem, the users
  and a success measure, drafts the intent alongside the conversation, and checks what the
  product already has (see [Duplicate requests](#duplicate-requests)). You can edit the draft
  directly and submit it when it is complete. Submitting starts discovery.

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
  `<product>-governance/product.yaml`. An approver is a team (`acme-clinic/product-owners`) or a login.
  Cross-lane gates (G4, G5, G6) ask each touched lane in turn, using that lane's approvers.
- **Resolving an escalation** uses the approvers of the gate that follows the stuck phase.

The API records the signed-in login as the approver; a request body cannot override it.

Run as many replicas as you like behind one load balancer, all on the same Postgres
(`src/vrixel_sdlc/cluster.py`). Nothing routes a person back to the same replica:

- **One run per feature.** The replica that moves a feature, incident, release or initiative on
  holds its Postgres advisory lock while it runs. Another replica asked to move it on at the same
  time answers that it is still running, and every replica shows it as running. A replica that
  dies lets go with its connection; one that loses its lock connection stops before the next step,
  and the work carries on from the last checkpoint.
- **One watcher.** Every replica starts the watcher; only the one holding its lock polls CI and
  OpenChoreo and runs the daily inventory and hourly cost review. If it goes, another takes over on
  its next tick.
- **Asked once.** Post-deploy, acceptance and rollout runs the tool asks GitHub for are claimed in
  a table first, so two replicas never both ask.
- **Shared.** Live updates go through Postgres NOTIFY to every replica's browsers; intake chats,
  a thread's last error and its tracker sync error are in tables.

Without a database (development) everything is in memory: run one process.

## Organizations and product governance

The products in this repo (Clinic records, Farm app, Shop, Dental, Payments platform and Shared
services, in the `acme-*` orgs) are fictional. They exist so the tests and the demo exercise every
data class and lane shape. To run the tool for your own products, put your `registry.yaml` (and
the rest of `config/`) in a private repo and point `VRIXEL_CONFIG_DIR` at it.

The tool lives in the tools org. It holds no product constitutions or product settings: each
product owns a governance repo in its own GitHub organization, and the tool reads it. Governance
repos are named after the product id: `cln-governance`, `farm-governance`, `ss-governance`
(`repo:` in `config/registry.yaml`; that name is the default when it's left out).

```
vrixel-tools/                          tools org
  agentic-sdlc                         this repo: registry.yaml points at every governance repo
  sdlc-governance                      platform/constitution.md (VX- articles)
acme-clinic/                           one org per product; each owns its runtime
  cln-governance                       product.yaml · constitution.md · context.md · CODEOWNERS
                                       architecture/ (overview.md, decisions/ADR-*.md)
                                       roadmap/ (goals.yaml, initiatives/, releases/)
                                       incidents/ (postmortems)
  cln-intake-api, cln-web, ...         product code; PRs are opened here
acme-farm/ · acme-shop/ · acme-dental/ · acme-pay/   same layout
acme-shared/                           shared lane: services and libraries every product runs
  ss-governance                        same files, but no runtime
  ss-llm-gateway, ss-audit-sink, ...   shipped as packages; each product runs them itself
```

### Lanes: products and shared services

Every org is a **lane** in `config/registry.yaml`, with a `kind`:

- **`product`** lanes own their runtime. `product.yaml` declares `runtime.openchoreo_project`
  and its environment pipeline, and their services deploy there.
- **`shared`** lanes (`acme-shared`) own no runtime and ship versioned libraries and
  frameworks. Each consuming product runs them in its own runtime. A shared manifest that
  declares a runtime is rejected.

A product lists the lanes it builds on in `depends_on` (the examples use `[SS, PAY]`). A lane
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
    Git tags drive it, so every language works the same way. At the merge the tool tags the
    merge commit `v1.5.0-rc.1`, and the repo's release workflow publishes `1.5.0-rc.1` to GitHub
    Packages (Maven, npm, NuGet, RubyGems, or ghcr.io for images and Helm charts). At G6 it tags
    the same commit `v1.5.0` and the workflow publishes the stable version. The version follows
    the repo's latest stable tag: minor by default, patch for a hotfix, major when the HLD marks
    the repo's contract change as breaking. The tool never builds or uploads a package and holds
    no registry credentials; the workflow publishes with its own `GITHUB_TOKEN`. A library that
    declares its package (`package: {type: maven, name: com.acme.shared.security}` on the repo in
    `product.yaml`) is checked at G6: the lane can't approve while its release candidate isn't
    in GitHub Packages (`integrations/packages.py`).
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
`<product>-governance` repo.

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
`platform@1.0.0#ba05394+cln@1.0.0#a1b2c3d`.

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
- ADR-CLN-FEAT-4-2: Create acme-clinic/cln-consent-registry-service: consent has its own data and release cadence
## New repos
- acme-clinic/cln-consent-registry-service (provider): Owns patient consent records [ADR-CLN-FEAT-4-2]
```

**A new product starts with no repos.** Its `product.yaml` lists `repos: []` and sets
`repo_prefix: cln-`; the first feature's HLD proposes the repos it needs, each with an ADR, and the
same approval and provisioning apply. Don't list repos before a design calls for them.

**Checked before G4.** The repo must be in the product's own org, follow the naming rule (the
product's prefix, lowercase words joined by "-"), have a role with a template, not exist
already, and have an ADR that names it. G4 shows exactly what approval will create.

**Applied by the provision step** (`config/repos.yaml`, the golden path):

| Area | Setting |
| --- | --- |
| Source | Generated from the role's template (`vrixel-tools/template-service`, `-web`, `-bff`, `-library`, `-event-worker`, `-mcp-server`, `-agent`, `-acceptance`), public. Public repos get rulesets, auto-merge, CodeQL and secret scanning on any GitHub plan; secrets and patient data never go in a repo |
| Name | The template's name replaced by the component's in the files the template lists in `.vrixel/identity` (its Workload, app and component name), so its logs, traces and error reports name it |
| Merging | Squash only, titled after the pull request; branches deleted after merge; auto-merge and "Update branch" on |
| Features | No wiki, projects or discussions: work lives in the tracker, docs in the repo |
| Security | Dependency graph, Dependabot alerts and security updates, secret scanning with push protection, CodeQL default setup |
| Branch ruleset | Pull requests only, merged when their required checks pass: no GitHub review, because people approve in the pipeline's gates (G1, G4, G6; G5 for high-risk and regulated changes) and agents push and merge under one App identity; linear history; no force pushes or deletion; required checks per role; merges blocked on new code-scanning alerts of high or above; repository admins may merge a pull request whose checks haven't passed |
| Release tags | `v*` tags can't be moved or deleted |
| Ownership | CODEOWNERS from the gate approvers (tech leads own everything; security owns the high-risk paths) |
| Access | Teams from the approvers: tech leads maintain, architects and security push, product owners read |
| Labels | Topics, and org custom properties (`vrixel_managed`, `vrixel_product`, `vrixel_data_class`) that org-level rulesets can target |
| Runtime | An OpenChoreo component in the product's project (none for libraries), with auto-build and auto-deploy off so every change goes through the gates |
| Governance | A pull request to the product's governance repo adding the repo to `product.yaml` |

Until that governance PR merges, the feature carries its new repos itself, so its plan, PRs,
risk tier, builds and deployments include them. Every step is idempotent. If one fails, the
architects (G4 approvers) can retry, finish it by hand and carry on, or stop the feature.

Products tighten in `<product>-governance/product.yaml`: their own templates, extra required checks, more
GitHub reviews on top of the gates (`required_approvals`), broader team access. Turning off a security
feature is rejected.

```yaml
repo_standards:
  templates: { provider: acme-clinic/template-fhir-service }
  extra_required_checks: [fhir-conformance]
  required_approvals: 1   # also require a GitHub review (needs a second identity)
```

**Setup once per org.**
1. Publish the templates named in `config/repos.yaml` to `vrixel-tools` and mark each one a
   template repository: `template-service` and `template-event-worker` (Java 21, Spring Boot),
   `template-web` (React, TypeScript), `template-library` (Java), `template-mcp-server` and
   `template-agent` (Python).
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

## Data and events

Products change state Kafka first (constitution VX-16). A provider checks a command, publishes one
event and answers 202 once Kafka holds it; it never writes a database in the request. Event workers
keep the data stores from those events, and any store can be rebuilt by replaying its topics.

| Role | Template | Required checks | What it is |
| --- | --- | --- | --- |
| `provider` | `vrixel-tools/template-service` | `unit`, `contract`, `CodeQL` | Versioned API; commands publish CloudEvents with `Events.publish` (idempotent producer, acks=all, keyed by record id); 503 and no change when Kafka doesn't confirm |
| `event-worker` | `vrixel-tools/template-event-worker` | `unit`, `CodeQL` | Applies events to Postgres (or an index) once and in order: event ids recorded in the same transaction, rows keep their Kafka position, retries then a dead-letter topic; integration tests against real Postgres and Kafka |

An HLD with event workers has an `## Events` section, one line per event type
(`- vrixel.<entity>.<what happened>.v1: <who publishes it, who applies it>`). Coders are told to
publish or apply those events, reviewers check VX-16, and the build fails if a named event isn't
published or applied by any task. Services coordinate through events; a central orchestrator needs
an ADR.

## Agentic features in products

Products build their own agentic features with two more repo roles, so an HLD can propose them
like any other repo and the provision step creates them on the golden path:

| Role | Template | Required checks | What it is |
| --- | --- | --- | --- |
| `mcp-server` | `vrixel-tools/template-mcp-server` | `unit`, `CodeQL` | Product capabilities as MCP tools over Streamable HTTP: OAuth scopes, annotated tools, idempotent writes, audit, PHI-minimal results |
| `agent` | `vrixel-tools/template-agent` | `unit`, `evals`, `CodeQL` | A durable LangGraph workflow agent: MCP tools on an allowlist, models through the product's LLM gateway, a person approves every change |

Roles merge and deploy in dependency order: library, provider, event-worker, mcp-server, agent, consumer, bff, acceptance (merged, never deployed).
The `evals` quality suite reads the agent's `evals / report` status on every PR (recorded model
responses); after a deploy to dev the agent's post-deploy job runs the same evals against the
real model and reports `integration / report`. Platform articles VX-12 to VX-14 make these
rules: models only through the gateway, people approve changes, evals gate every PR.

**Evals at release, and variants.** A product can let its agents' live evals miss a case by chance
but not more (`min_pass_rate: 0.95` on the `integration` suite in `product.yaml`, never below the
platform's floor). G6 shows each agent repo the change set ships: its evals on recorded responses (PR)
and against the real model (dev), with the same numbers from the last release that changed that
agent, so a release that makes the agent worse is visible before production. A prompt or model
change in the agent can go to a share of people first: the template's reviewed variants
(`variants.toml`) are rolled out through the product's Flipt (`<agent>.variant`), the post-deploy
evals run every variant, and the gateway's spans show each variant's tokens and cost.

Models are reached through `ss-llm-gateway`, a shared-services package each product deploys in
its own OpenChoreo project (runtime stays with the product). Its policy follows the product's
data class: PHI only to providers under a BAA, payments with card data blocked, per-caller keys,
budgets and model allowlists, and a usage ledger.

## Design knowledge: building on what exists

Agents design against what the product has already decided and built, and G4 shows how a new
design relates to it.

**Where the knowledge lives (recommended practice).** Git is the system of record for design.
Decisions are lightweight architecture decision records kept in source control next to what they
govern (Thoughtworks Technology Radar: Adopt), and API contracts are the OpenAPI documents in each
API repo. Both change only through reviewed pull requests, so what agents read is what people
approved, versioned with the code. The tools around them are views:

| Source | Holds | How the pipeline uses it |
| --- | --- | --- |
| Governance repo, `architecture/` | The current design (`overview.md`) and decisions (`decisions/ADR-*.md`) | Read before every design phase; updated by a pull request after each release |
| API repos, `api/openapi.yaml` | Contracts as merged on the default branch | Read before design; contract changes are checked against them |
| Capability catalog (Backstage in the OpenChoreo portal) | What is live, and approved designs still in flight | Read before design; written at G4 and at release |
| Jira or OpenProject | Intent, scope and status of work | Written by the pipeline (one item per feature and task); not read back as design truth |
| Lucid or draw.io | Diagrams for reviewers | Generated from the HLD's Mermaid; not read back (the Mermaid in Git is the source) |

Reading design from a wiki, a tracker or a diagram tool through MCP gives agents text that may be
stale, unreviewed or out of step with the code; reading it from Git ties every decision to a
commit G4 can cite. To use another store (Confluence, Backstage TechDocs), add a source class and
name it in `config/knowledge.yaml`; the pipeline only sees the pack each source returns.

**The architecture record**, in each product's governance repo:

```
architecture/
  overview.md                 the current design: context, building blocks, key flows, change log
  decisions/ADR-CLN-0001.md   one ADR per file: YAML front matter (id, title, status, repos,
                              supersedes, superseded_by) and Context, Decision, Consequences
```

**What agents get.** Every design and build agent's prompt carries the accepted decisions, the
current API versions and operations, live and in-flight capabilities, and the overview, bounded
in size with decisions first. Design knowledge is read at the governance repo's current head, not
the feature's pin, so it is current; the constitution stays pinned.

**The design check (before G4).** An HLD must:
- list the ADRs it builds on in `## Builds on` (or `- None: <why>`); each must exist and be accepted,
  not superseded;
- override a decision only with a new ADR that says `Supersedes ADR-x`;
- state each affected API's change from the version actually merged
  (`acme-clinic/cln-intake-api: 1.4.0 -> 1.5.0, adds ...`, or `unchanged`); a major bump needs a
  `## Migration` section (expand, migrate consumers, contract; VX-4).

**What G4 shows.** The decisions the design builds on and overrides, each API change, decisions on
the same repos the design doesn't mention, other approved designs in flight that change the same
APIs, and the commit of the record it was compared with.

**What the product has built.** Agents also get the list of every feature the pipeline has run
for the product and where it got to (released, in progress at a station, or stopped), and the
capability catalog: designs approved at G4 (in flight) and released capabilities (live). Both are
kept in the platform database, so they survive restarts; the catalog starts empty and holds only
what the pipeline actually designed and released.

### Duplicate requests

A request that looks like something the product already has is caught before it becomes a
feature. The check compares the title and description with every feature of the product (in
progress, released or stopped) and every live capability:

| Where | What happens |
| --- | --- |
| Intake chat | The agent names the match ("This looks like CLN-FEAT-1, in progress at G2") and asks the creator to add to that feature or say how this one differs; submit stays off until they do |
| `POST /api/features` | A likely duplicate is refused (409, with the matches) unless `not_duplicate_reason` is given |
| G1 | The approver sees the matches and the creator's reason, and decides |

Matching uses shared words (title words weighted double, common words ignored, word endings
reduced), so it catches repeats and rewordings. A request that describes the same thing in
entirely different words gets through to G1, where the approver sees the closest matches.

**After release.** The capability goes live in the catalog with its decisions and API changes, and
the pipeline opens a pull request in the governance repo that adds the feature's ADRs (accepted),
marks superseded ones, and logs the change in the overview. People merge it.

## Roadmap: goals, initiatives and releases

Above features sit a product's goals and the initiatives that serve them; beside them, releases
decide when users see features. Scope is the product today; ids carry the product, goals may name
a `parent` and the record has a `scope` field, so a portfolio level can sit above products later.

**The record is Git**, in each product's governance repo, changed by pull request like ADRs:

```
roadmap/
  goals.yaml                outcomes for a period: id, title, measure, period (parent: later)
  initiatives/INI-CLN-1.md  one approved initiative: goal, horizon (now/next/later), budget envelope,
                            lanes, release model, success measure, candidate features
  releases/REL-CLN-1.md     one release: launch group or release train, features, status, notes, evidence
```

The tool runs initiatives and releases as threads alongside features, computes progress and spend
from the features themselves, and mirrors initiatives to Jira or OpenProject (an item one level
above the feature epics; set `initiative_type` in `tools.work_tracking` if your tracker calls it
something else) and releases as comments on member epics.

**Initiatives have their own gate, G0.** A strategist agent drafts the initiative from a request:
the goal it serves (or a proposed new goal with a measure), outcome, success measure, horizon,
budget envelope (at most `max_initiative_budget_usd`), the lanes that must change, the release
model and the candidate features. Checks run before G0: the goal exists, lanes are the product or
its dependencies, candidates don't repeat existing features, and the initiative doesn't repeat
another. G0 is decided by the product owners (`approvers.g0`, else G1's approvers), then by each
other lane involved. Approval opens a governance PR that adds the initiative (and any new goal).

**Features under initiatives.** Starting a candidate creates a feature linked to its initiative
(the duplicate check still applies). G1 shows roadmap fit: the initiative, its horizon and how much
of its envelope is spent, or "unplanned work" for features without one.

**Releases.** Every feature reaches production behind a feature flag when its product has a flag service (see below).

| Model | How it ships | Use for |
| --- | --- | --- |
| Launch group (default) | Each member goes through G6 and reaches production with its flag off. When all are there, the release gate (G6 approvers of every lane touched) turns the flags on together. | Most work: small deploys, coordinated exposure, rollback by flag |
| Release train | Members wait after staging instead of asking for G6 each. One release gate approves them together with combined release notes and evidence (suites, accepted risks, G5 approvers); they then promote to production. A cancelled train hands its features back to their own G6. | Regulated or contractual shipments (certification builds, go-lives, cross-lane cutovers) |

A feature that isn't in a release goes live at its own G6.

### Feature flags (Flipt)

Each product runs its own [Flipt](https://docs.flipt.io/v2) (v2, self-hosted, Git-backed) in its
own runtime, and names it in `product.yaml`:

```yaml
tools:
  flags:
    provider: flipt
    url: https://flipt.cln.example.com
    namespace: cln                 # optional; default: the product id in lower case
    environments:                  # optional; runtime environment -> Flipt environment
      production: production
```

The platform holds one Flipt client token per product in `VRIXEL_FLIPT_TOKEN_<PRODUCT_ID>`
(a static token in the product's Flipt config, allowed to read and change that namespace).

| When | What happens to the flag |
| --- | --- |
| G4 approved | The tool creates `<product>.<feature-id>.<title>` in every environment: on before production (testers see it), off in production. |
| Build | Coders put the new behavior behind the flag, read through OpenFeature with default `false`; the reviewer checks it, and the build fails its check if no repo users reach uses it. Libraries don't need it. |
| G6, alone | The flag turns on in production. |
| G6, in a launch group | It stays off; the release gate turns the group's flags on together. |
| Incident | On-call can approve "turn the flag off" as the first mitigation. |

The product's code reads flags with OpenFeature's OFREP provider pointed at its Flipt
(`x-flipt-environment` and `x-flipt-namespace` headers); the templates set this up, and with no
flag server configured every flag reads as its default, off. The tool changes flags through
Flipt's environments API (`/api/v2/environments/{env}/namespaces/{ns}/resources`), so each change
is a commit in the product's flag repo.

**Without a flag service** a product still ships: features get no flag, a launch-group member
waits after staging until its launch is approved (like a train), and incidents roll back instead.
With stub delivery, products without Flipt get in-memory flags so the demo works end to end.

## Production issues: incidents, hotfixes and intent changes

The tool watches production through the signals a product already emits, opens an incident, has
agents investigate, and works it to the end by itself where it safely can: it undoes the recent
change behind the failure, checks that the failure stopped, starts the fix and drafts the
postmortem. On-call is paged only when the tool can't fix it (and told, at low urgency, as a SEV1
opens). The tool's own production actions are the platform's one exception to VX-3, set in
sdlc-governance's `platform/remediation.yaml`: turn off a recent feature's flag, roll back a recent
release. Everything else is a person's decision.

**Signals in.**

| Source | Endpoint | Auth |
| --- | --- | --- |
| Prometheus (Alertmanager webhook; SLO burn-rate and error alerts) | `POST /api/signals/alertmanager` | `Authorization: Bearer $VRIXEL_SIGNALS_TOKEN` |
| Sentry (internal integration: issues created or regressed, alert rules) | `POST /api/signals/sentry` | `Sentry-Hook-Signature`, HMAC with `VRIXEL_SENTRY_CLIENT_SECRET` |
| Support tickets (any tool that can post JSON) | `POST /api/signals/ticket` | `Authorization: Bearer $VRIXEL_SIGNALS_TOKEN` |
| OpenChoreo alert rules (log, metric and FinOps budget rules; webhook channel without `payloadTemplate`) | `POST /api/signals/openchoreo` | channel header `Authorization: Bearer $VRIXEL_SIGNALS_TOKEN` |
| CloudWatch alarms on AWS resources (each data plane's `AlarmRouting`: an EventBridge API destination) | `POST /api/signals/cloudwatch` | connection header `Authorization: Bearer $VRIXEL_SIGNALS_TOKEN` |
| AWS Budgets (through an SNS topic) | `POST /api/signals/aws-budgets` | HTTPS subscription `https://vrixel:$VRIXEL_SIGNALS_TOKEN@<host>/api/signals/aws-budgets`; topic listed in `VRIXEL_AWS_BUDGET_TOPICS` |
| People in the web app | "Report a problem" on the Incidents page | signed-in member of the product's org |

Each signal is tied to a product (a `vrixel_product` label or tag, else its `component` matched to
the product's repos and OpenChoreo components) and a severity (SEV1 to SEV4 from `severity` or the
Sentry level). A signal joins the open incident for the same alert, issue or component; otherwise a
firing one opens a new incident. Signals are stored in the platform database. A webhook answers as
soon as its signals are recorded and on-call is paged; triage runs after it.

A signal that matches no product (a missing label, a component the registry doesn't know, an
OpenChoreo project no product runs in) opens no incident, but it isn't dropped: the tool logs a
warning and keeps it for 30 days with the reason, such as "component billing-cron matches no
product's repos or OpenChoreo components". Members of the platform's org see them at the bottom of
the Incidents page and at `GET /api/signals/unmatched`, so the source can be fixed before it
matters. They may come from any product, so nobody else sees them.

**Paging on-call (PagerDuty).** Each product has a PagerDuty service; the tool sends it events with
the service's Events API v2 integration key (`ops/paging.py`). Two kinds of page, each its own
PagerDuty alert:

| When | PagerDuty alert |
| --- | --- |
| A SEV1 opens (`page.heads_up` in the policy) | heads-up: dedup key = the incident id, severity `info` (low urgency): the tool is on it, nobody need act |
| The tool can't fix it: no action it may take fits, its action failed, or it's still failing afterwards | urgent: dedup key = `<incident id>:urgent`, severity `critical` for SEV1, else `error` (high urgency) |
| On-call's first decision in the tool | `acknowledge` |
| Recovered, resolved by on-call, or merged into another incident | `resolve` |

Cost findings and requests to change what was built (a user report whose triage proposes only an
intent change) never page: they wait in the tool for working hours.

```bash
VRIXEL_PAGERDUTY_ROUTING_KEYS='{"CLN": "<integration key>", "PAY": "<integration key>"}'
VRIXEL_PAGERDUTY_REGION=us          # or eu (events.eu.pagerduty.com)
VRIXEL_PUBLIC_URL=https://sdlc.vrixel.dev   # pages link back to the incident here
```

PagerDuty owns the schedules, notifications and escalation when nobody acknowledges; set each
service's urgency to "based on alert severity". A page carries the incident id, severity,
component, environment and why on-call is needed, and the title only for products that hold
neither PHI nor payment data: alert titles can quote exception messages, and PagerDuty is outside
those products' data boundary. Sends go out in order on a background thread; throttling, server
errors and network failures are retried for about two minutes. The incident page shows what was
sent, and says so plainly when a product has no PagerDuty service or PagerDuty refused a page.
Without `VRIXEL_PAGERDUTY_ROUTING_KEYS` (stub mode) nothing is sent.

**The incident.**

1. *A second opinion*: the tool asks OpenChoreo's SRE agent to investigate the failing component
   (`POST /api/v1alpha1/rca-agent/analyze`, with the incident as the alert) and waits a few minutes
   for its report (SEV1 5, SEV2 8, else 10). The agent reads the component's logs, metrics and
   traces with an AI model, so it runs only where the product's data may go to that model's
   provider: the policy's `analysis.rca.provider` must be allowed for the product's data class (the
   same `data_classes` setting the tool's own agents follow), the product's region must not be
   excluded (data in India stays there), and a product can keep it away entirely
   (`runtime.remediation.analysis: false`). OpenChoreo's own fix agent (`REMED_AGENT`) must stay off:
   the policy refuses `fix_agent: true`, and a report showing an action it applied is recorded as a
   change made outside the tool and pages on-call. Budget alerts go to OpenChoreo's FinOps agent
   instead, whose right-sizing recommendation goes into the proposed intent change.
2. *Triage* (ops agent): the signals; OpenChoreo's report (root causes with their confidence, causes
   ruled out, its recommendations); the released features that changed the component, most recent
   first, with their flags; recent logs and metrics; what the tool may do by itself. It writes a
   summary, impact, likely cause and proposed actions.
3. *The tool mitigates* when one of triage's actions is in the remediation policy, fits, and the
   second opinion agrees: a high-confidence root cause from OpenChoreo whose evidence is in the same
   component and that names a change to it (a release, deployment, configuration or flag). No
   report, a less confident one, or one pointing elsewhere means a person decides
   (`without_analysis: suspects` lets the tool act on the release suspect alone). Its track record
   counts too: an action whose automatic uses recovered the incident less than 60% of the time over
   90 days (at least 5 uses) is suspended for that product, recorded in the change log, until its
   record improves. The actions:
   - **Turn a feature's flag off**, if the feature reached the failing component in the last 3 days
     and its flag is on. Preferred: it touches only the new behavior.
   - **Roll the component back** to its previous release, if a release reached it in the last day.

   It acts as `agent:remediate` (recorded on the incident's actions and events, the tracker item, and
   the flag service's log), once per incident (`attempts`), and then starts the hotfix triage proposed.
   A product can narrow this or turn it off:

   ```yaml
   runtime:
     remediation:
       autonomous: [flag_off]     # or false: on-call decides every action
   ```
4. *Escalate* when it can't (nothing fits, no agreement, its action failed, or the failure persists): the urgent
   page, and on-call decides (`approvers.oncall`, else G6's approvers): approve some or all actions,
   ask triage to look again, resolve it, or merge it into another incident. After a person decides,
   the tool takes no action by itself on that incident.
5. *Act*, by the tool or as approved:
   - **Turn a feature's flag off**: the fastest mitigation for a recent launch.
   - **Roll back** the component to its previous release in production.
   - **Start a hotfix**: an expedited feature that skips discovery to architecture but keeps planning,
     build, tests, security scans, G5 and G6, which people approve. A short spec and design are
     written from the incident.
   - **Change the intent**: when the product works as built but not as users need, a new feature
     (G1) or initiative (G0) starts with the incident as evidence, linked to the original work.
6. *Check it recovered*: alerts that report their own resolution must have resolved; and after a
   mitigation, or for signals that never resolve (OpenChoreo rules, GlitchTip, error-report alerts),
   it must stay quiet for `quiet_minutes` (SEV1 10, SEV2 15, SEV3 30, SEV4 60): no new alert or error
   report after the first 5 minutes (the alert rules' look-back), and fewer than 10 errors in the
   component's last 5 minutes of logs. Not quiet by then, or still firing after the window (SEV1 30
   minutes, SEV2 1 hour, SEV3 4 hours, SEV4 1 day): escalate, or back to on-call if already paged.
   Reported problems with no alerts skip this.
7. *Postmortem* (agent, blameless) and *close*: on-call approves it in working hours (no page), and a
   governance pull request records it in `incidents/`.

Cost signals (see [Cost](#cost-what-features-and-products-cost)) take the same path. They never
join a component's outage, triage proposes right-sizing (an intent change) or accepting the spend,
and they skip the recovery check: a budget alert or a cost review never "resolves". The tool takes
no action on them by itself, and they never page.

Incidents appear in Decisions and on the Incidents page, get a tracker item (a Bug, set
`incident_type` to change it) with a comment per action, and agents designing new work see recent
incidents and their causes.

## Tracing and the audit trail (VX-8, VX-15)

The templates give every product the same instrumentation:

| | How | Where |
| --- | --- | --- |
| Traces | OpenTelemetry, exported over OTLP to the data plane's collector; incoming `traceparent` is honored and passed on, so one trace runs from the browser through services, agents, MCP servers and the LLM gateway. Agent spans follow the GenAI conventions and never carry prompt or result text. | every template |
| Logs | JSON with `trace.id` and `span.id` | every template |
| Audit | Who did what to which records, as CloudEvents (`vrixel.audit.v1`) on the product's Kafka audit topic. A request whose record Kafka doesn't acknowledge fails. | service, agent, MCP server, LLM gateway |
| Audit store | `ss-audit-sink` copies the topic into an append-only, hash-chained Postgres table (one consumer group) and archives it to object storage under Object Lock (another), so neither destination holds up the other; `audit-sink verify` checks the chain and `restore` rebuilds Postgres from the archive. | each product's runtime |

The pipeline holds designs and builds to it. In PHI and payments products an HLD that changes a
repo users reach must have an `## Audit` section naming its audited actions
(`- consent.read: consent`); coders are told to publish those records, reviewers to check them,
and the build fails its check if an action the HLD names appears in no task.

## Keeping repos on the standards

The provision step applies the standards to repos it creates; `vrixel repos` keeps every managed
repo on them afterwards, including ones created by hand:

```bash
vrixel repos check                    # report drift; exits 1 if any repo needs fixing
vrixel repos apply                    # change what differs (safe to repeat)
vrixel repos check --repo acme-clinic/cln-governance --json
```

Managed repos are each lane's repos (from its governance manifest), each lane's governance repo
(from `registry.yaml`) and the platform's own repos (`platform_repos` in `config/repos.yaml`:
the tool, `sdlc-governance` and the templates). Governance repos get reviews and code owners
but no required checks; templates are marked as templates and require their role's checks.

| Never changed automatically | Why |
| --- | --- |
| A repo whose visibility differs from the standard (public) | Visibility changes are a person's decision |
| Missing CODEOWNERS, `dependabot.yml`, `SECURITY.md` | Files arrive through pull requests, like any change |
| Secret scanning and CodeQL on a private repo without GitHub Advanced Security | They're paid features; the report says which repos need them. Until then CodeQL is not a required check and there's no code-scanning merge rule, so pull requests aren't blocked by a check that never runs |

The `repo-standards` workflow runs the check daily and fails when a repo drifts; run it by hand
with `apply` to fix. It needs the platform's GitHub App installed in every org with administration
rights (secrets `VRIXEL_GITHUB_APP_ID`, `VRIXEL_GITHUB_APP_PRIVATE_KEY`); the App also needs
organization custom properties (admin) to define `vrixel_managed`, `vrixel_product` and
`vrixel_data_class`.

## Delivery: GitHub and OpenChoreo

From build to production, the pipeline works in the product's own GitHub org and OpenChoreo
project, and pauses on CI, builds and deployments the way it pauses on people:

```
join ─ provision ─ plan ─ build ─ ci_wait ─ cs_env ─ cs_build_wait ─ cs_deploy ─ cs_wait ─[G5]─ merge_train ─ build_wait
      ─ deploy_dev ─ dev_wait ─ deploy_staging ─ staging_wait ─ release ─ G6 (binds production for the approver)
      ─ production_wait ─ operate
```

| Step | GitHub | OpenChoreo |
| --- | --- | --- |
| Build | A change note per task (`.vrixel/changes/<feature>/<task>.md`) on `feature/<id>`; one PR per repo, found again by branch on retries; posts `vrixel/gates` pending on each PR | |
| ci_wait | Waits until every check run and status on each PR head is green (except the tool's own `vrixel/` and `openchoreo/` statuses) | |
| cs_env … cs_wait | Asks each repo to run its dev-stage suites, and the acceptance repo its tests, against the PR heads | Creates `cs-<feature>` on the dev data plane, builds each PR head, binds the builds and the feature's managed services there, waits until ready |
| merge_train | Deletes the change set's environment. Posts `vrixel/gates` success (naming the gates approved), then merges exactly the commit the gates saw, in dependency order (libraries, providers, consumers); branch protection still applies. An abort posts `vrixel/gates` failure | Triggers a WorkflowRun on each merge commit; shared-lane repos publish packages instead |
| build_wait | Posts `openchoreo/build` on the merge commit | Waits for the WorkflowRuns |
| deploy and wait | Posts `openchoreo/dev`, `openchoreo/staging`, `openchoreo/production` | Binds the latest release to the first environment, then promotes the same release; waits until each ReleaseBinding is ready |

Waits are interrupts with `type: "external"`. They never reach the decision inbox. The engine
checks them after every step, on a timer (`VRIXEL_WATCH_INTERVAL_S`), and when GitHub sends a
webhook. When one fails or times out:
- **Red CI** goes back to the agents first. The failures go to the coder, only the failing repos'
  tasks are redone (their PRs get new commits), and CI runs again, up to the product's
  `max_loops`. When those run out, the build's escalation asks people: let agents retry, or vouch
  for it.
- **A failed build, a failed deployment check or failing tests in dev or staging** go back to the
  agents too (see [Fixing failures before production](#fixing-failures-before-production)). The
  release managers (G6 approvers) decide only when the tries run out or nothing the feature changed
  is to blame: try again, carry on anyway, or stop. A deployment the platform refuses goes straight
  to them. Production is never fixed this way: retrying it means asking for G6 again.

### Testing the change set together before merging

A change set often spans repos: an API, the web app that calls it, a worker, the acceptance tests.
CI tests each pull request on its own; only an environment running all of them shows whether they
work together. So after CI passes, and before G5 or the merge, the pipeline gives the change set its
own environment (`vrixel_sdlc/changeset_env.py`):

1. **Create** `cs-<feature id>` (for example `cs-farm-feat-12`) on the data plane of the product's
   first environment, labelled `vrixel.io/change-set`. It is outside the deployment pipeline and is
   never promoted from.
2. **Build** each runtime repo's pull request head in OpenChoreo. Libraries, acceptance tests and
   store apps aren't built here.
3. **Deploy** those builds there, with the feature's managed services at their smallest size and
   empty (test data only, never retained).
4. **Test** with the product's dev-stage suites (integration and the like) and the feature's
   acceptance tests from the acceptance repo's pull request, as check runs named
   `acceptance / <feature> / change_set`.

When it passes, the change set goes on to G5 or the merge train, which deletes the environment.
When it fails, it goes back to the agents like red CI: the failing repos' tasks are redone on their
pull requests, CI runs again, and the environment is rebuilt from the new heads. Main only receives
a change set that worked as a whole, so fixes after merge become rare. Dev, staging and production
still get the merged commits through the pipeline, and run their own suites again.

```yaml
runtime:
  change_sets:
    enabled: true      # default true for products with a runtime
    required: false    # true: people decide when the environment can't be created
    max_open: 5        # change-set environments a product may have at once
    ttl_hours: 48      # deleted after this even if the feature is still open
```

If the environment can't be created (OpenChoreo refuses, or `max_open` are already open), the
change set merges as before and the feature says so, unless `required` is set; then the release
managers decide. Every 15 minutes the watcher deletes change-set environments whose feature has
finished or stopped, or that outlived `ttl_hours`.

**Before relying on it, check in a sandbox** that your OpenChoreo version lets the service client
create and delete Environments and bind a release to an environment outside the project's
deployment pipeline, and that deleting an environment removes its bindings. The adapter uses the
MCP tools `create_environment`, `get_environment`, `list_environments`, `delete_environment` and
`delete_release_binding`. The platform side (namespace, ingress and DNS per environment, its
share of the dev data plane, secrets for test-only managed services) belongs in the platform
foundation, so an environment is ready the moment OpenChoreo creates it. Each open change set costs
a small copy of dev while it is open; `max_open` and `ttl_hours` cap that.

### Fixing failures before production

Before production the pipeline fixes its own failures; people step in only when it can't. Whatever
fails (red CI, blocking security findings, the OpenChoreo build, a component that isn't ready, or
integration, end-to-end, acceptance or performance tests in dev or staging), the tool works out which
of the feature's repos it points at (for a failed acceptance criterion, the repos whose tasks cover
it), gives their coders what failed, and redoes only those tasks. Before merge the pull requests get
new commits; after merge the fix goes on a branch of its own (`feature/<id>-fix-<n>`) and goes
through CI, G5 when the change is risky, the merge, the build, dev and staging again.

Each feature gets the product's `max_loops` tries before merge (CI and security findings) and as
many again for the environments (the change set's own, its builds, dev and staging), counted afresh once
staging passes. When they run out,
or the failure is outside what the feature changed (a managed service, the platform), people decide:
the build's escalation before merge, the release managers or security approvers after it. Every try
is an `auto_fix` event with the failures, so the decision shows what the agents already tried.

Agents bind only dev and staging (VX-3). Production is bound after G6, recorded under the
approver's name, and OpenChoreo's own authorization is the backstop.

**Product runtime** (`<product>-governance/product.yaml`):

```yaml
runtime:
  openchoreo_project: farm
  pipeline: [dev, staging, production]   # OpenChoreo environment names, first to last
  namespace: vrixel                      # optional; defaults to config/tools.yaml
  components:                            # optional; defaults to the repo name
    acme-farm/farm-mobile: field-app
```

**Going live** (`VRIXEL_DELIVERY=live`):
1. Grant the GitHub App, in each product org: contents, pull requests and commit statuses
   (write), checks (read). Subscribe it to check runs, check suites, statuses and pull requests,
   with the webhook URL `<VRIXEL_PUBLIC_URL>/api/webhooks/github` and `VRIXEL_GITHUB_WEBHOOK_SECRET`.
2. In OpenChoreo, give the confidential service client (`service_mcp_client` by default) a role
   that can trigger workflow runs, manage release bindings and create, list and delete
   environments in each product's project. Set
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
- **Failed tests, low coverage, or a required suite that never reports:** the agents fix them
  first, before or after merge; when the tries run out the build's escalation (before merge) or
  the release managers (after) decide.
- **Findings at or above `block_on`:** the agents fix them first, the same way. When the tries run
  out the lane's security approvers decide (`approvers.security` in `product.yaml`, else the G5
  approvers): have agents try again, scan again, accept the risk, or stop the feature.
- **Accepting a risk** creates a waiver for each finding. It records the reason (required), the
  approver and an expiry of at most 90 days. Waived findings stop blocking until the waiver
  expires; they are listed under "Accepted risks" in the release document and on the G6 panel.
- **G5 and G6** show every suite run so far. The release document carries the same evidence for
  audit.

**Changing it later, without rewriting anything**

| Change | Where |
| --- | --- |
| Swap a tool (Semgrep for CodeQL, Vitest for Jest, Snyk for Dependabot) | `match` of the suite: tool names or check names |
| Tighten a product (coverage 95%, block on medium, make DAST required) | `quality:` in the product's `<product>-governance/product.yaml` |
| Add a suite (license scan, performance budget, accessibility) | A new entry with `kind`, `stage`, `source`, `match` |
| Raise or lower a floor for a data class | `floors` in `config/quality.yaml` (tools org) |
| A tool that reports somewhere new (a SonarQube API, a test-results bucket) | One class with `collect()` in `quality/sources.py` or your own package, registered under `sources` |

Products can only tighten: the platform rejects a manifest that lowers coverage, raises a
blocking severity, or makes a required suite optional for its data class.

```yaml
# <product>-governance/product.yaml
approvers:
  security: [acme-clinic/security]
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

## Security model (VX-17)

Every request carries two checked identities: the calling workload, proven by the mesh (Istio
ambient mTLS), and the person or service it acts for, as a token from the product's Keycloak realm
issued for the product. `vrixel-tools/platform-security` generates each product's realm and mesh
policies from one spec.

| Component | Template | What it enforces |
| --- | --- | --- |
| BFF (`bff`) | `template-bff` (Spring Cloud Gateway) | The only internet-facing component for a web app: OIDC sign-in with PKCE, tokens kept in the Redis session, `__Host-SESSION` cookie, `/api/**` relayed with the person's token, CSRF and Origin checks |
| Web app (`consumer`) | `template-web` | No tokens in the browser; same origin through the BFF; project-visible only; security headers on every page |
| Service (`provider`) | `template-service` | Issuer, keys, expiry and the product audience; `@PreAuthorize` on every endpoint (the build fails otherwise); record-level `AccessPolicy`; forward inside the product, token exchange across trust boundaries |
| Agent (`agent`) | `template-agent` | Token exchange per MCP server for the person behind each step (the approver for approved changes); no fixed tokens; people's tokens never stored |
| MCP server (`mcp-server`) | `template-mcp-server` | Audience and a scope per tool; audit on behalf of the person |
| Mobile app (`mobile`) | `template-mobile` (React Native, Expo) | A public client (PKCE in the system browser), tokens in the Keychain or Keystore, calls only through the BFF's `/mobile/api` route (bearer token, audience and minimum version checked there); PHI builds encrypt local data and block screenshots (VX-18) |

Designs declare exposure in an `## Endpoints` section, one line per component:
`- <org>/<repo>: <project|namespace|internal|external|none>[; calls <component>, ...]`. Only the BFF
may be `external` (anything else needs an ADR that says so), a web app never is, and every call
must name a component of the design or the product. A mobile app is `none` and calls only the
product's BFF. G4 approves it; the components' `workload.yaml`
and the product's mesh policies follow it. Coders and reviewers get their role's rules. A web app's
post-deploy tests run against the BFF's public URL.

## One incident per failure

A failure people see in the app and the error behind it arrive as separate signals from different
components: a Sentry issue from the web app, a Sentry issue or OpenChoreo alert from a service.
Before opening an incident, the tool looks for an open incident of the same product the signal
belongs to (`ops/correlate.py`), strongest evidence first:

| Evidence | How |
| --- | --- |
| Same request | The signal names a trace another one in the incident names too. The services return a W3C `traceresponse` header; the web and mobile apps tag their error reports with the failed call's trace id; services tag theirs with the request's; for an OpenChoreo log alert the tool reads the trace ids of the component's recent error logs |
| Calls | Within 30 minutes, one component calls the other, directly or through others: the product's consumer repos call its provider repos (the contracts say who calls whom), plus `runtime.calls` in product.yaml |
| Same release | Within 30 minutes, the same feature went out to both in the two hours before |

A signal that joins says why (`joined_by`). A signal naming the same request as other open
incidents merges them into one. The root cause candidate is the furthest-downstream component
among the incident's signals (the others are where it showed): triage and the rollback or flag
decision start there, and when a joining signal moves it, triage runs again. On-call can merge two
incidents (`POST /api/incidents/{id}/merge`, or "Merge into it" on the incident page) and take a
wrongly joined signal out (`/split`, "Not the same failure"), which opens its own incident.

The templates tag every error report with `component`, the component's name, so an issue maps to
its component whatever the Sentry or GlitchTip project is called. The name comes from the files a
template lists in `.vrixel/identity` (its workload, app and component files): when the tool creates
a repo from a template, it replaces the template's name with the component's in each of them. Events
may also carry `vrixel_product`; without it the component alone finds the product.

## Error tracking: Sentry or self-hosted GlitchTip

Where a product's error reports may go is the platform's call, in sdlc-governance's
`platform/error-tracking.yaml`; the first rule that matches decides:

| Rule | When | Allowed |
| --- | --- | --- |
| payments | `data_class: payments` | GlitchTip (self-hosted): card data stays in our account (PCI DSS) and payment data in India (RBI) |
| india | `runtime.aws.region` is ap-south-1 or ap-south-2 | GlitchTip: Sentry's SaaS runs in the US and EU only |
| phi | `data_class: phi` | GlitchTip, or Sentry with a signed BAA (`baa: true`) |
| general | anything else | either |

A product chooses in product.yaml, and the governance loader refuses a choice the rules don't allow:

```yaml
runtime:
  error_tracking:
    provider: glitchtip                   # or sentry
    url: https://errors.pay.example.dev    # GlitchTip's address
```

GlitchTip takes the same Sentry SDKs the templates already use, so a component changes only its
`SENTRY_DSN`. platform-resources' `observability/glitchtip` deploys it into the product's
OpenChoreo project (web, worker, Postgres; no Redis). Its alerts reach the tool through a webhook
recipient per GlitchTip project:

- `vrixel glitchtip-token PAY` prints the path, `/api/signals/glitchtip/PAY?token=…`. The token is
  derived from `VRIXEL_SIGNALS_TOKEN` and opens that product's endpoint only (a webhook can't send
  headers, so it is in the URL).
- With an API token per product (`VRIXEL_GLITCHTIP_TOKENS='{"PAY": "…"}'`, scope `event:read`)
  the tool reads each alerted issue's latest event for its `trace_id`, so a GlitchTip issue joins
  the same incident as the failures it caused elsewhere. The request goes only to the product's
  configured `url`.

The `component` tag maps GlitchTip issues to components the same way.

## Component alert rules: logs and exceptions

Components log JSON to stdout (the templates' structured logging), with an exception's stack under
`error.stack_trace` and the request's `trace.id`. OpenChoreo's logs module collects every container's
output with Fluent Bit (to OpenSearch, CloudWatch Logs or another backend) and labels it with the
component, project and environment, so logs, traces and alerts line up per component.

Every component that runs code gets the platform's default alert rules (`platform/alert-rules.yaml`
in sdlc-governance) as OpenChoreo `observability-alert-rule` traits: exceptions logged (more than 10
in 5 minutes, and a surge of more than 100) and memory near its limit. The pipeline adds whichever a
component lacks before each deployment (`ensure_traits`, an event `alert_rules` says which), and
never changes or removes a trait someone tuned.

OpenChoreo evaluates the rules per environment and notifies the environment's default notification
channel. Point that channel (a webhook, no `payloadTemplate`, header `Authorization: Bearer
$VRIXEL_SIGNALS_TOKEN`) at `/api/signals/openchoreo` in every environment:

| Environment | What happens |
| --- | --- |
| Production | the alert opens or joins an incident for the component |
| Any other | kept on the alarm board for an hour (OpenChoreo never says an alert cleared); G6 shows it if it is on a component the feature changes |

## AWS services: alarms

OpenChoreo's observability plane watches components: their logs, CPU and memory, HTTP traffic and
traces, keyed by component and environment. AWS services aren't pods, their CloudWatch metrics carry
no OpenChoreo labels, and OpenChoreo's alert rules can't use them. So they are watched from two
sides:

- **From the component.** AWS clients are instrumented with OpenTelemetry's AWS SDK
  instrumentation (the event worker template's DynamoDB store does; the coder is told to for any
  AWS client a feature adds), so each call to DynamoDB, SQS, Kinesis, S3 or SNS is a span of the
  component in OpenChoreo's traces, and failures it logs can drive OpenChoreo log alerts
  (`/api/signals/openchoreo`).
- **From the service.** Every resource comes with its catalog type's standard CloudWatch alarms
  (platform catalog `alarms`, built by the Compositions in `vrixel-tools/platform-resources`).
  Each data plane's `AlarmRouting` sends their state changes through EventBridge to
  `/api/signals/cloudwatch`. An alarm's description says whose it is (product, feature, resource,
  environment, the components that use it, severity).

| Where the alarm is | What happens |
| --- | --- |
| Production | ALARM opens or joins an incident for the component that uses the resource; OK resolves it, so the incident's monitoring sees recovery |
| Any other environment | Kept on the alarm board; G6 shows the approvers every alarm firing before production on the feature's resources or on resources its components use |

G4 shows the alarms each AWS service comes with. Alarms are named with the data plane's prefix, so
EventBridge sends only Vrixel's; other alarms in the account are ignored.

## Gradual releases

A feature reaches production behind its flag for a growing share of users
(`platform/remediation.yaml` `gradual_release`, `ops/rollout.py`):

```yaml
gradual_release:
  steps: [5, 25, 50, 100]                              # % of users with the feature on
  bake_minutes: { low: 30, high: 60, regulated: 120 }  # quiet time per step, by risk tier
```

G6 approves the whole plan: the flag goes on for the first step, and the tool steps up when the
step has been quiet for its bake time (no incident open on the feature's production components, and
none recovered within it). An incident holds it. The flag going off (the tool's or on-call's flag
off in an incident) stops it, and the release managers decide: start again from the first step,
leave it off, or stop the feature. Each step is a change record approved by G6's approvers and made
by `agent:rollout`; the feature page shows the share it's on for. In Flipt it is a threshold
rollout (`THRESHOLD_ROLLOUT_TYPE`, value true) over a flag that is off; turning the flag off removes
the rollout too, so off is off for everyone.

**Users with it on against users with it off.** Time alone can miss a feature that hurts only
the users who have it: at 5% its errors are a small share of the component's. The templates
count, per flag, the work that evaluated it and how much failed, apart for users who got it on and
off, and log one `vrixel.flag_cohorts {...}` line per flag a minute:

| Template | Counts | A failure is |
|---|---|---|
| service, agent | requests | a 5xx or an unhandled exception |
| MCP server | requests | also a JSON-RPC error or a tool result with `isError` |
| event worker | delivery attempts | an exception, or the message going to the dead-letter topic |
| web, mobile | user-minutes (a minute in which the app used the flag) | a minute with an unhandled error or a failed API call |

The service, MCP server, agent and event worker also count slow work: slower than the latency
budget (500 ms for a request, 60 s for an agent's run, 5 s for a worker's attempt;
`FLAG_COHORTS_SLOW_MS`). Percentiles can't be added across pods and minutes, but counts can: more
than 5% slow means the 95th percentile is over the budget.

The web and mobile apps post their counts to the BFF once a minute (`POST /flag-cohorts` with the
session and CSRF token, `/mobile/flag-cohorts` with the bearer token). The BFF accepts them only
from a signed-in user, at most once a minute each, caps them (120 per side, errors no more than
the total), adds them up and logs the lines with `"source": "web"` or `"mobile"`; one user can't
swing a comparison. The BFF checks no flags of its own: it is a gateway that forwards to the
services, which count their own requests, so counting there too would count the same request
twice.

The tool sums the lines from OpenChoreo's logs (`VRIXEL_OPENCHOREO_OBSERVER_URL`,
`ops/cohorts.py`) across the product's whole project, not only the feature's components: anything
that checks the flag counts, including a component the feature didn't change. It compares each
component (and the web and mobile apps) on its own, with a one-sided two-proportion z-test,
because a busy component that's fine would dilute one that isn't. It checks every half minute, so
the bar is high (`gradual_release.compare`): at least 200 on each side, z at least 3, and at least
half a percentage point and 25% more failures with it on. Slow requests are compared the same
way and by the same bar, when every line counts them. A regression in any one of them stops
the release at once, without waiting for the bake time: the tool turns the flag off (a change of
type automatic) when the product lets it turn flags off, and otherwise pages on-call urgently;
then the release managers decide. Each step keeps every component's comparison. A feature with no
such lines anywhere goes by time alone (the step says so).

**Failures around it.** A feature that breaks a provider shows up first as errors in its callers
(the web app among them), and a caller's new traffic can break what it calls. So an incident in a
component that calls the feature's components or that they call (the call graph from the
contracts and `runtime.calls`, across the lanes the product builds on) holds the rollout as one in
its own components does. And while it is rolling out, the feature is a suspect for those
incidents, even another lane's: triage can propose its flag off, and the tool turns it off by
itself when the policies of both the incident's product and the feature's product allow flag off
and OpenChoreo's analysis agrees.

Flipt buckets users by the OpenFeature targeting key: services pass the signed-in user, the web
template a random per-browser id in `localStorage`, the mobile template a random per-install id in
secure storage, so each person keeps the same answer as the share grows. A product can release at
once (`runtime.remediation.gradual_release: false`) or choose its own steps
(`gradual_release: {steps: [10, 100]}`).

**Launches** (release groups) go the same way: the launch gate approves the plan for the whole
group, held to its riskiest feature's bake time, and every flag widens together. A flag that goes
off, or whose users fail more (only that flag is turned off), stops the launch for the release
managers: start again, leave it as it is, or cancel it, which turns all its flags off. The
release's record says shipped once every flag is on for everyone. Store apps keep their stores'
staged rollout. Stub delivery compresses bake times, as it does deployments.

## Change management: every production change, and looking back

Every way the tool changes production writes one record to the change log (`ops/changes.py`),
when it happens:

| Change | Type | Approved by |
| --- | --- | --- |
| A feature released at G6 (its components, AWS resources, store apps, flag) | standard | G1 to G6's approvers, each recorded with the time |
| A launch turning its features' flags on | standard | the release gate's approvers |
| A hotfix released at G6 | emergency | on-call, at G5 and G6 |
| On-call turning a flag off or rolling back in an incident | emergency | on-call |
| The tool turning a flag off or rolling back by itself | automatic | `platform/remediation.yaml`; reviewed afterwards in the incident's postmortem |

A record names who asked, who approved, what changed (components and their releases, resources,
flags), the feature, release or incident behind it, the evidence (pull requests, the quality
suites each stage ran, security findings accepted with an expiry, or for an incident the cause
triage found), and whether it worked. Records are append-only and hash-chained: each carries the
hash of the one before it, so an edited or deleted record breaks the chain. `vrixel changes verify`
(with `DATABASE_URL`) and the Changes page check it. In Postgres, grant the tool's role only
INSERT and SELECT on `vrixel_changes`, so nothing it runs can rewrite history; the chain shows it if
anything else does. Keep the database's backups for as long as your strictest regime needs the
records (HIPAA documentation: six years).

When an incident's flag off or rollback undoes a release, that release is linked to the incident
as its cause. That's what change failure rate counts.

**The Changes page** (and `GET /api/changes`, `/api/changes/export.csv` for auditors) lists every
change with its approvals and evidence. **Looking back** (`GET /api/reports/lookback`) shows, per
month: incidents by severity and component, how many the tool fixed by itself and how many needed
on-call, the median and worst time to recover, changes by type and failed ones, releases and the
share that caused an incident (change failure rate), and the releases behind the most incidents.

Changes made outside the tool (kubectl, the AWS console) aren't in the log. Direct production
access should be break-glass: time-limited, approved and logged.

**ServiceNow receipts.** A product whose organization keeps its change records in ServiceNow gets
each record there too, as a receipt: a closed change_request made after the fact, which never holds
a release up (the gates already approved it):

```yaml
tools:
  change_management:
    provider: servicenow
    instance: https://acme.service-now.com
    assignment_group: Platform Engineering
    types: {standard: normal, emergency: emergency, automatic: emergency}   # the defaults
    close: {state: "3", done: successful, failed: unsuccessful}             # your state model's values
```

`VRIXEL_SERVICENOW_AUTH_<PRODUCT>` holds the Authorization header. The change_request's
`correlation_id` is the tool's change id, so a retry finds the one it made; the description carries
what changed, who asked, the approvals per gate, the pull requests and the record's hash. Where the
instance's state model won't let it be closed that way, it stays open and the Changes page says so,
next to its ServiceNow number. Gating releases on ServiceNow approval isn't supported: changes are
approved at the tool's gates.

## Resource inventory: what runs where

The Inventory page (`GET /api/inventory/<product>`, and `/export.csv`) reads each product's
environments live (`ops/inventory.py`); nothing is kept by hand:

- **Components**: each one's OpenChoreo release binding per environment, the release running and its
  state, and a `kubectl` label selector that finds its workloads on the data plane
  (`openchoreo.dev/project`, `/component` and `/environment` label every workload).
- **Resources** (AWS services, and other OpenChoreo Resources such as GlitchTip's database): the
  binding per environment, its size and release, the AWS account and region, and its outputs.
  Plain outputs (table names, ARNs, endpoints) are shown; a secret output shows only which Secret
  holds it, never the value.
- **Store apps**: the version last released.
- The feature that created each one, the last 30 days of compute cost (components), the planned
  monthly cost from the platform catalog (AWS), and production against the environments before it.

With `VRIXEL_AWS_INVENTORY_ROLE` set (a role of that name in each environment's account, allowed
`tag:GetResources`), the tool also lists everything tagged `vrixel:product=<product>` there. That
fills in ARNs, and anything tagged for the product that OpenChoreo doesn't know is shown as
unmanaged, for someone to adopt or remove. Once a day the tool keeps each product's counts per
environment (`GET /api/inventory/<product>/history`) to show the trend.

## Cost: what features and products cost

One cost ledger holds every dollar a product spends, once, in [FinOps FOCUS](https://focus.finops.org/)
columns: product, feature (when one feature caused it), stage (**build** until production, **run**
after), category, environment, and how sure the figure is (*estimate*, *metered* usage at list price,
*billed* by AWS, or *allocated* from something shared; *provisional* until the source settles). The
watcher collects hourly from each source; collecting again replaces figures rather than adding to them.

| Category | Source | Stage |
| --- | --- | --- |
| Agents (model calls) | the usage ledger, per call and model | build (feature, initiative and release threads) |
| CI | GitHub Actions: the jobs of each PR's runs and its merge, per runner OS, rounded up per job | build |
| Acceptance tests | GitHub Actions runs dispatched for the feature (their run name carries the feature id) | build |
| Builds | OpenChoreo build runs × build-plane minute rate; EAS store builds when a rate is set | build |
| Dev and staging environments | OpenChoreo FinOps, each component's daily cost, shared among the features deployed there that day by the hours each was there; the rest is the product's | build / run |
| AWS services | Cost Explorer by `vrixel:product` and `vrixel:feature` tags per environment account: a feature's spend is build until release and run after; untagged is the product's | build / run |
| Production compute | OpenChoreo FinOps, each production component | run |
| Incident triage | the usage ledger, incident threads | run |
| People at gates | active minutes at each gate × hourly rate (off unless set) | build |
| Shared platform | OpenChoreo, observability and this tool's monthly cost, split by products' production compute (off unless set) | run |

Rates are set in `VRIXEL_COST_RATES` (JSON): `github_minute_usd` per OS (set from your plan),
`build_minute_usd`, `eas_build_usd`, `people_hour_usd`, `platform_monthly_usd`. Sources:
`VRIXEL_OPENCHOREO_OBSERVER_URL` (the `finops-opencost` module), and `VRIXEL_AWS_COST_EXPLORER=1` with
the `aws` extra and `ce:GetCostAndUsage` (activate `vrixel:product` and `vrixel:feature` as cost
allocation tags in the payer account).

**Where it shows.** Every gate packet carries the feature's build cost so far, by category. The
feature page has its cost to build and its own AWS since release. The Costs page shows a product's
month: run cost by category and environment, build cost, and each feature's build cost to date
against what G4 approved, with a FOCUS CSV download for finance and BI tools
(`GET /api/costs/{product}?month=YYYY-MM`, `/api/costs/{product}/focus.csv`,
`GET /api/features/{id}/cost`).

**Held to the plan.** G4 approves a feature's AWS services with planning costs per environment;
that is its cost plan. 7 and 30 days after release (`VRIXEL_COST_REVIEW_DAYS`) the tool compares
its AWS spend with the plan as a monthly run-rate, and its production components' compute with the
week before release. Spend more than `VRIXEL_COST_TOLERANCE` (default 20%) and $5/month over
becomes a cost signal in the incident flow: SEV2 for production AWS at twice the plan or more,
SEV3 otherwise. OpenChoreo budget alert rules and AWS Budgets (named
`vrixel-<product id>-<environment>`) arrive as cost signals the same way.

Not counted yet: in-product agents' model spend outside this tool's gateway, and SaaS bills
(Sentry, store fees).
**Building a feature.** Every agent call is metered in the usage ledger (tokens, cache hits, cost by
phase and model) against the feature's budget, and each gate records the people's active minutes.

**Running it.** G4 approves a feature's AWS services with their planning cost per environment; that
becomes the feature's cost plan. After release the tool reviews the feature (by default 7 and 30 days
after, `VRIXEL_COST_REVIEW_DAYS`):

| What | Source | Compared with |
| --- | --- | --- |
| The feature's AWS resources, per environment | AWS Cost Explorer: tag `vrixel:feature` in the environment's account (`VRIXEL_AWS_COST_EXPLORER=1`, the `aws` extra, `ce:GetCostAndUsage`) | The G4 plan, as a monthly run-rate |
| Its components' compute in production | OpenChoreo FinOps (`finops-opencost` module) through the Observer cost API (`VRIXEL_OPENCHOREO_OBSERVER_URL`) | The same components the week before release |

Spend more than `VRIXEL_COST_TOLERANCE` (default 20%) and $5/month above the comparison becomes a
cost signal in the incident flow: SEV2 for production AWS at twice the plan or more, SEV3 otherwise.
Budgets are the second line: OpenChoreo budget alert rules per component, and AWS Budgets per
product account named `vrixel-<product id>-<environment>`, arrive as cost signals the same way.

`vrixel:feature` and `vrixel:product` must be activated as cost allocation tags in the payer
account; Cost Explorer data lags by about a day. Each review is stored, and served at
`GET /api/features/{id}/cost` (build cost, plan, reviews) and `GET /api/costs/{product}` (each
feature's build cost and run-rate, with totals). The feature page shows the plan against the latest
review. Not covered yet: CI minutes, build and test environment costs before release, cluster
compute and shared platform costs allocated to products.

## AWS services (VX-19)

Features that need a managed service get it from the platform's AWS catalog
(`platform/aws-resources.yaml` in sdlc-governance): DynamoDB, Kinesis, SQS, S3, SNS, and RDS
PostgreSQL and ElastiCache Valkey through OpenChoreo's Crossplane provisioner. Each type is an
OpenChoreo ClusterResourceType backed by a Crossplane Composition in `vrixel-tools/platform-resources`,
with encryption, private access, backups and tags built in. People approve; agents do the rest:

| Step | Who | What |
| --- | --- | --- |
| Design | architect agent | Picks services from the catalog (it sees what each is for, its data classes, and what the product already runs), writes a `## Resources` section and an ADR for each non-default service |
| Checks | tool | Type in the catalog and approved for the data class; parameters valid; every component's access declared (`reads`, `writes`, `deletes`) and allowed by the type; only platform components hold AWS access; no change that replaces stored data; the product has AWS accounts |
| G4 | architects | Approve the services, who may use them, and their size and planning cost per environment |
| Build | coding agents | Add `dependencies.resources` to `workload.yaml` (outputs as environment variables), reach each service behind a port with the SDK's default credentials, test against the emulator |
| dev, staging | pipeline | Creates each Resource and a workload identity per component (an IAM role through EKS Pod Identity holding only its declared actions), binds them to the environment with its size, waits until they are ready, then deploys the components |
| G6 | release managers | See the production sizes and cost; approving binds the resources in production on their behalf |

```
## Resources
- field-readings: kinesis-stream; retention_hours=168
  writes: farm-sensor-api; reads: farm-field-worker
- field-state: dynamodb-table; partition_key=field_id; size production=large
  reads, writes: farm-field-worker
```

A product runs AWS from `runtime.aws` in its `product.yaml`: a region allowed for its data class
and one account per environment. The pipeline never deletes a resource; changing a key or a
queue's ordering replaces stored data, so the design check refuses it and a person does it.

## Store apps (VX-18)

A mobile app (role `mobile`) is released, not deployed: people install a build and keep it for
months, and every build goes through the stores. The pipeline is the same up to the merge; then,
instead of OpenChoreo, each stage asks the app's repo to release the merge commit
(`vrixel-release`, `release.yml` from `template-mobile`) and waits for the status it posts:

| Stage | The app's repo | Waits for |
| --- | --- | --- |
| dev | a test build against dev on an emulator, Maestro flows | `release / dev`, `integration / report` |
| staging | the same against staging, then TestFlight and Play's internal track for testers | `release / staging`, `e2e / report` |
| production (G6) | store builds submitted; Play starts at 1% of users | `release / production` |
| rollout | each step after `min_hours`: Play vitals' crash-free users, then widen | `rollout / <n>%` |

After G6 the `rollout` node widens the Play rollout through `steps` (default 10%, 50%, 100%), each
at least `min_hours` (24) after the last, because vitals arrive daily. A failed health check halts
the Play rollout and goes to the release managers (retry the check, carry on, or stop); pause the
App Store's phased release by hand. Set the policy per product in
`repo_standards.roles.mobile.rollout`. Features ship dark in the build and turn on with their flag
as for any other component, so a store release needs no second release for G6's flag.

Old versions are the other half: the BFF answers 426 to versions below `BFF_MOBILE_MIN_VERSION`,
and an API change the app needs is expanded before the app uses it and contracted only after the
minimum passes every version that used the old shape.

## Acceptance tests across repos

Each repo tests itself (unit, contract, its own post-deploy suites). A feature that changes two or
more runtime repos is also tested **end to end, across repos**, from the product's acceptance repo
(role `acceptance`, one per product, from `vrixel-tools/template-acceptance`: Playwright Test in
TypeScript). Add it to the product's `repos:` to turn this on.

- **Backend and frontend.** The `api` project tests through the components' APIs and events (no
  browser): command, then read the result back once the event workers have applied it; events a
  service must publish; synthetic events from other teams; dead letters and duplicates. The `ui`
  project adds browser journeys when the change set has a web app. Never through databases.
- **Tags and coverage.** Each test is declared with `spec()`: the feature criteria it proves
  (tags `@CLN-FEAT-12`, `@CLN-FEAT-12/AC-3`; an extended test keeps earlier features' tags), its
  capability (`@cap-consent`), and what it covers: routes with an optional status
  (`cln-intake-api PUT /api/v1/consents/{id} 202`), events, pages. Component tags
  (`@comp-cln-intake-api`) follow from the covers. The fixtures fail a test that reaches what it
  doesn't declare, so `catalog.json` (every test, checked in CI) stays true.
- **Plans extend before adding.** The planner sees the catalog. The plan's `## Acceptance tests`
  section says per criterion `extend`, `replace` or `add`; adding a test whose coverage an existing
  test of the same kind (API or browser) already has is rejected unless the line says `because:`;
  extending or replacing a test that doesn't exist is rejected. `vrixel acceptance review --product CLN` lists tests already covered by
  another, for merging; the repo's nightly full run does the same and reports slow tests.
- **Runs after the whole change set is deployed.** In dev, then staging, the pipeline asks the
  acceptance repo (`repository_dispatch: vrixel-acceptance`) to run the merged tests with every
  component's URL, the feature's flag on for test users, and a selection: the feature's tests,
  every test that reaches a changed component, plus `@critical` journeys in staging. The full
  regression runs nightly and before release trains, not per feature.
- **Every criterion needs a passing test.** The run posts `acceptance / <feature> / <stage>` with a
  line per criterion; the `acceptance` and `acceptance-staging` suites (config/quality.yaml) fail
  the stage for any criterion that failed or has no test, and G6 shows each criterion's result.
  `budget_s` (default 600s in dev, 900s in staging) reports runs that are too slow.

## The Backstage catalog: Git is the source

Each repo a feature changes carries a Backstage `Component` in `catalog-info.yaml` at its root. The
build commits it to the feature branch next to the code, so it is reviewed in the pull request and
merges with it; Backstage in the OpenChoreo portal discovers it from the default branch. The tool
never writes to Backstage.

The pipeline fills only what it knows (the GitHub slug, the product as `system`, the OpenChoreo
project and component, a type from the repo's role, the product owners' team as `owner`) and adds
the feature's capability to the `vrixel.io/capabilities` annotation. It keeps everything else: a
description, links, `dependsOn`, an `API` entity, or a type, owner or lifecycle someone changed.
The tool's own catalog (Postgres) still holds capabilities while they are designed and in flight,
which Backstage doesn't model (`vrixel_sdlc/catalog_info.py`).

## Diagrams and work tracking

Each product chooses its tools in `<product>-governance/product.yaml`. The tools org runs the connections
and holds the credentials (`config/tools.yaml`), and limits which tools may receive content from
each data class:

```yaml
tools:
  diagrams:
    provider: lucid          # lucid | drawio
  work_tracking:
    provider: jira           # jira | openproject
    project: FARM              # Jira project key or OpenProject project identifier
    # optional: epic_type, story_type, task_type, subtask_type, statuses: {in_progress, done, cancelled}
    # block_on_drift: false   # true holds G6 while the tracker disagrees with the matrix
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

If the change touches a lane whose data class doesn't allow the tool (a Farm app change to the
PHI shared-services lane, say), the diagram stays in the HLD and isn't published. A failed
publish never fails the phase.

**Work tracking.** After every step of a feature, the engine reconciles the product's Jira or
OpenProject project with the feature.

| When | In the tracker |
| --- | --- |
| G1 approved | Epic `[FARM-FEAT-1] title`, linked to the feature page |
| G2 approved | One story per acceptance criterion and per NFR, under the epic; epic in progress |
| Plan passed | One task per planned repo change, and one sub-task per test case under its story; a deferred requirement's story is cancelled; items in a restricted lane carry no detail |
| Build and merge | A task is in progress once it has a PR and done when merged; a sub-task follows its test case's status; a story is done when all its test cases have passed |
| Every decision | A comment on the epic with gate, decision, approver and feedback; diagram links too |
| Released or stopped | Epic done, or open items cancelled |

Sync is idempotent: each item carries a marker (a Jira label, an OpenProject subject prefix), so
a retry finds what it created. A tracker outage shows on the feature page and never stops agents
or gates; the next step, or **Sync now**, catches up. Sub-tasks use `subtask_type` (Jira
"Sub-task", OpenProject "Task" with the story as parent).

Before G6 the engine reads every item back from the tracker and compares it with the matrix. Drift
is an item that is missing or deleted, closed in the tracker while its tests haven't passed, open
in the tracker though done here, or renamed. The G6 packet lists it; by default it only warns, and
`block_on_drift: true` makes it hold the release like any other gap.

Adapters resolve tool and argument names from each server's published tool schemas, so they keep
working when a server renames a parameter. Without credentials a provider runs as an offline
stand-in, and `/api/products` reports `mode: stub`. Unattended agents can't complete an OAuth
consent screen, so each server needs an API key or token issued to a service account.

## Performance tests

Budgets, and which changes need a performance test, are policy; the templates run the tests.

**Budgets** start from industry targets and products tighten them in `product.yaml`
(`performance:`): APIs answer 95% of requests within 500 ms and 99% within 1 s with under 1% errors;
web pages reach Largest Contentful Paint within 2.5 s with under 200 ms of Total Blocking Time, a
layout shift under 0.1 and at most 200 KB of compressed JavaScript; events reach their store within
5 s; mobile apps start cold within 2 s with a JavaScript bundle under 3 MB compressed; agents finish a
run within 8 model turns.

```yaml
# <product>-governance/product.yaml
performance:
  peak_rps: 40                 # expected peak per API
  staging_load_factor: 0.1     # staging is smaller than production: load it at this share of peak
  staging_regression_pct: 10   # how much worse than the last release staging may be
  smoke_regression_pct: 20     # how much worse than main a pull request may be
  budgets: { api: { p95_ms: 300 } }
  hot_paths: ["src/main/java/**/billing/**"]   # added to the templates' usual hot paths
```

**Which test** each repo of a change set needs is decided when its pull request opens, by fixed
rules (`vrixel_sdlc/performance.py`):

| Level | When | What runs |
| --- | --- | --- |
| none | only docs, tests or comments change; libraries and the acceptance repo | nothing |
| smoke | a hot path, a database migration, a runtime or base-image upgrade, timeouts, pools or runtime settings, or a large change | `perf-smoke` on the PR: the image built from main and the PR's, loaded the same way on the same runner; fails past `smoke_regression_pct` or the budget |
| full | an API contract change, a new or changed event handler, or an HLD whose **Performance** section says the feature adds traffic | the smoke test, then `performance` in staging: the expected peak times `staging_load_factor`, judged against the budget and the last release's run |

Only APIs (`provider`) and web apps (`consumer`) have a staging load test. A BFF, MCP server, agent, event worker or mobile app that would need one stops at the smoke test (a session, delegated tokens and model calls, Kafka credentials or a device farm would be needed); their production signals and the rollout's comparison watch them.

The HLD's Performance section (expected load, new traffic, data growth, journeys) feeds the rules;
data growth flags the nightly soak test. The reviewer agent lists performance risks it sees (an
N+1 query, a call inside a loop, an unbounded read): any raises the level to at least smoke. Agents
only raise a level. The tool labels the PR (`vrixel:perf-smoke`, `vrixel:perf-full`), which runs
the template's `perf-smoke` job, and asks for the staging load test with the post-deploy run
(`performance` in the dispatch). Both suites read only the repos that need them, and G5 and G6
show each repo's level and why. A failed performance suite goes back to the agents like any red
check; release managers can carry on past it with a recorded reason.

**Journeys across components.** When any repo of a change set needs the staging test, the
product's acceptance run also gets the settings (`perf_settings` in its dispatch) and loads the
critical journeys end to end (`perf/journeys.js` in template-acceptance), at the same scaled peak
and judged the same way. Its report adds `Performance: passed|failed (...)`; a failed journey test
fails the acceptance suite at G6.

**In production.** Staging is smaller than production, so the last word is the gradual release:
the templates also count slow requests per flag side (slower than the latency budget: 500 ms for
APIs and MCP servers, 60 s for an agent's run, 5 s for a worker's attempt; `FLAG_COHORTS_SLOW_MS`),
and the users with the feature on being slow more often stops the release like failing more does
(see [Gradual releases](#gradual-releases)).

## Models: more than one vendor

Every role in `config/models.yaml` names a runtime and a model, so a stage's contract (its prompt
layers, its JSON reply, its caps) is the same whichever vendor runs it.

| Runtime | What runs | Models |
| --- | --- | --- |
| `claude_code` | Claude Agent SDK: an agent session in the checkout | Anthropic |
| `codex` | OpenAI's Codex CLI (`codex exec`): an agent session in the checkout | OpenAI, or an open-weight model behind an OpenAI-compatible Responses API (vLLM) |
| `generic` | one gateway call per task (documents, critiques, triage) | Anthropic, or any OpenAI-compatible chat API (`providers` in models.yaml) |

- **Fallbacks.** A role lists `fallback` roles. When its provider is rate limited, overloaded or
  unreachable, the same task goes to the next one that may see the product's data class. The
  coder falls back to Codex on OpenAI, then to the self-hosted model; for PHI and payment products
  only the self-hosted model is allowed. The ledger records which role did the work.
- **A second opinion.** The reviewer lists `second_opinion` roles. Another model family reads the
  same PR with the same instructions and reply format: OpenAI for general data, the self-hosted
  model for PHI and payments, never the model that did the review. It is advisory at first (shown
  beside the review in the build summary, with whether the two agree);
  `second_opinion_blocking: true` makes its blocking findings block the merge.
- **Optional vendors.** Roles used only as a fallback or second opinion turn off, with a warning,
  when their provider isn't configured (`OPENAI_API_KEY`, `VRIXEL_SELFHOSTED_URL`) or their model
  has no price under `prices`. Every model that runs needs a price, so feature budgets hold
  whichever vendor does the work; self-hosted models are priced at zero (paid for as infrastructure).
- **Codex sessions** get the phase's skills, constitution, product and feature context first in
  the prompt (through stdin), then the task. Codex reads Claude-format plugins, so the same
  skills-only plugins are installed for it in the image (`/opt/vrixel/codex-home`, a plugin
  marketplace over `/opt/vrixel/plugins`); each session starts from a copy of that home, sees every
  plugin skill by name and reads one when it needs it, unless `on_demand` is off for the role or
  the config pins plugins other than the image's (plugin commands, such as
  `product-management:brainstorm`, reach it only through the pasted skills). Sessions reply with the stage's JSON schema
  (`--output-schema`), run sandboxed to the checkout with no approvals, stop at the role's
  `max_minutes`, and are costed from their token usage. Codex has no dollar cap of its own: a session
  that spent more than its cap is logged and the feature's budget check sees it. A provider that
  can't be reached is given up on after a few reconnects, so the next fallback gets the task.

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

Every agent session is an OpenTelemetry span, `vrixel.agent <phase>/<role>`, carrying the feature,
product, phase, role, task, attempt, data class and config version, plus OpenTelemetry's GenAI
attributes (operation, agent, model, provider). Each session's own telemetry nests under it, so one
trace shows the whole session (`vrixel_sdlc/telemetry.py`):

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

**Where traces go.** The tool sends OTLP/HTTP to `VRIXEL_OTEL_ENDPOINT`, a collector the platform
runs, which routes them: structure for every lane to OpenChoreo's observability plane (next to the
product's own traces, in the portal's Traces tab), and to the platform's self-hosted Langfuse for
session views, eval datasets and people's labels, with content only for the lanes allowed it (the
collector, Langfuse and the routing live in platform-foundation). `VRIXEL_TRACE_URL` is the link
template, e.g. `https://langfuse.example.com/project/vrixel/traces/{trace_id}`. Without an endpoint,
or without the OpenTelemetry SDK (`pip install '.[telemetry]'`, included in `live`), nothing is
exported and strict mode warns.

## Evals of the tool's own agents

A change to the models, prompts or settings the agents run with is checked against past work
before it merges (`vrixel_sdlc/evals.py`).

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

- **Pull requests to the config repo** (`VRIXEL_CONFIG_REPO`, e.g. `vrixel-tools/sdlc-config`): the
  GitHub App's `pull_request` webhook makes the tool replay the newest labelled cases (5 per phase,
  `VRIXEL_EVAL_CASES_PER_PHASE`; up to `VRIXEL_EVAL_BUDGET_USD`, 20 by default, per config) under
  the PR's config and its base, and post `vrixel / evals` on the head commit. Make it a required
  check. The run happens where the cases and the model keys are, never in the config repo's CI.
  `GET /api/evals/runs/{id}` has the full comparison; `POST /api/evals/runs` starts one for the
  teams in `VRIXEL_EVAL_APPROVERS`.
- **From the command line:** `vrixel eval export --product FARM --out cases.jsonl` and
  `vrixel eval run --config path/to/candidate/config [--baseline-config ...] [--pairwise]` (cases
  from the database, `DATABASE_URL`, or `--cases FILE`; exits 1 on a regression).
- **Data classes.** Only `general` cases are used unless you ask for others
  (`--data-class`, `VRIXEL_EVAL_DATA_CLASSES`); a regulated case still reaches only the providers its
  data class allows.
- **Langfuse.** With `VRIXEL_LANGFUSE_URL`, `VRIXEL_LANGFUSE_PUBLIC_KEY` and
  `VRIXEL_LANGFUSE_SECRET_KEY`, each run writes one dataset per phase (`vrixel/<phase>`), links the
  run's traces to the items and scores them; documents go to Langfuse only for `general` cases.

### Trying a config on some features first: the canary

A model, prompt or setting change that passed its evals can run on a share of new features before it
replaces the config in use. Point `VRIXEL_CANARY_CONFIG_DIR` at the candidate config directory (laid
out like `config/`) and set `VRIXEL_CANARY_PERCENT` (1 to 50). Each new feature is assigned once, by
its id, and keeps its config to the end; hotfixes, and lanes outside `VRIXEL_CANARY_DATA_CLASSES`
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
  Everything else is free prose. A test keeps each contract in step with the stub documents,
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

Vrixel's goal is agents doing 85% of the work and people 15%. A feature created with **Pilot
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
`&format=markdown`), or `vrixel pilot-report FARM` against the tool's database.

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

## Workflow profiles: feature and express

Not every change needs discovery and a UX design. Whoever starts the work picks a profile (the New
intent page asks when the product allows more than one); the gates that matter stay whatever the
profile (`vrixel_sdlc/profiles.py`):

| Profile | Runs | Gates people decide |
| --- | --- | --- |
| Feature | every phase | G1 to G6 (G5 for risky change sets) |
| Express | a small, low-risk change: spec, architecture, then plan to production; no discovery, no UX design | G4 (with the spec, since there is no G2), G5 (risky), G6 |

Express starts from a brief written from the request. The gates it decides are recorded as decided
by the profile (`approver: profile:express`), and the rail shows the skipped stations. It doesn't
skip architecture (it finds the repos, contracts and lanes the change touches), planning, tests,
security scans, the performance rules, G5 for risky changes or G6. Products list the profiles their
people may use; express needs listing and never applies to PHI or payments products.

Defects are not a profile. Before production the pipeline fixes its own failures (see [Fixing
failures before production](#fixing-failures-before-production)); in production, alerts, error
reports and reported problems open incidents, which mitigate and then hotfix or change the intent.

```yaml
# <product>-governance/product.yaml
workflow:
  profiles: [feature, express]   # default: [feature]
  default: feature
```

## Agent notes for the gates

A reviewer can't see what an agent took as given, where it departed from the brief, which options
it weighed or what it wasn't sure of. So every document ends with an `## Agent notes` section, and
the coder's reply carries the same notes for each pull request (`vrixel_sdlc/diary.py`):

```
## Agent notes
- Assumed: <something not stated that the agent took as given>
- Changed: <where it departed from the brief, the design or a rule, and why>
- Traded off: <an option it weighed, and why it chose as it did>
- Open: <a question for the people at the gate>
```

The tool keeps them per phase on the feature, shows them at the gate the phase goes to, open
questions first (G4 under express also shows the spec's; G6 shows the coders'), and adds the
coder's to the pull request description for its reviewers. A note is the agent's account, not a
check: the evaluators, reviewers and people still decide.

## Traceability

Every link from a feature's reason to what runs in production is written down: success metrics in
the brief, goals, acceptance criteria and non-functional requirements in the spec, decisions and
affected repos in the HLD, tasks and test cases in the plan, pull requests per task, test results,
deployments per repo. `GET /api/features/{id}/trace` reads them into one matrix, and the feature's
page shows it: each requirement with its goal, design, tasks and test cases and their status, pull
requests, acceptance results and where it runs, the production incidents on its components, and the
gaps in the chain. Nothing is stored twice, so the matrix is always what the pipeline has now
(`vrixel_sdlc/traceability.py`). `GET /api/features/{id}/trace.csv` exports one row per test case.

**In the spec.** Each criterion names the goal it serves, and NFRs are requirements in their own
right, with a kind and a measurable target:

```markdown
## Acceptance criteria
- AC-1 (G1): Given a farmer offline, when they log a crop, then it is saved on the device
## Non-functional requirements
- NFR-1 (performance, G1): saving answers within 200 ms on a mid-range phone
- NFR-2 (accessibility): meets WCAG 2.1 AA        # or "- None: <why>"
```

The evaluator rejects a criterion without a goal, a goal that doesn't exist, an NFR kind outside
the list (performance, availability, reliability, scalability, security, privacy, accessibility,
compliance, usability, maintainability, observability, other) and an NFR with no number, limit or named standard.

**In the plan.** Every requirement traces to a task (or is deferred, with a reason) and has at
least one test case, and each case says how it will be checked:

```markdown
## Traceability
- AC-1 -> TASK-1
- NFR-2 -> deferred: the design system's audit covers it next sprint
## Test cases
- TC-1 (AC-1, happy): saves offline -> TASK-1 tests/crop.test.ts::saves_offline
- TC-2 (AC-1, error): names the bad field -> acceptance tests/crop.spec.ts "bad date"
- TC-3 (NFR-1, performance): save under 200 ms -> suite perf-smoke
- TC-4 (AC-1, edge): storage full -> manual: needs a full device
```

**In the build.** Each coder gets its task's test cases and must write each test under exactly the
planned name. The engine looks for it on the branch; a missing test sends the task back to its
coder, and the evaluator reports it if it is still missing. A case's status is `passed` (its PR
merged, its acceptance criterion passed, or its suite passed), `failed`, `written`, `missing`,
`pending`, `manual` or `unknown`.

**At G6.** A complete matrix is a prerequisite for release: every requirement has a goal, a task
and test cases, and every case has passed. The G6 packet shows the matrix, its gaps, the
deferrals and the tracker drift. While it is incomplete, approving needs an override reason from
someone in `approvers.traceability` in `product.yaml` (G2's approvers if unset); the API answers
409 without a reason and 403 for anyone else. The override, the deferrals and a snapshot of the
matrix are written to the release's change record, in its hash chain. Hotfixes are shown their
gaps but never held. Release trains approve G6 in advance, so they aren't held either; review their
matrices before the train runs.

Back from code: `GET /api/products/{id}/trace?repo=org/name` or `?pr=org/name%2312` lists the
features that changed it, with the criteria and tasks involved.

## Taking over, and learning from people's corrections

A phase that keeps coming back is a person's to finish, and what the person corrects is learned so
the next feature doesn't need the same correction.

**When a gate goes round in circles.** Every gate shows what people already asked for in this
feature (each send-back, retry and takeover, with its reason) and how many times the phase went
back. Sent back `takeover_after_revisions` times (2 by default, in `config/registry.yaml`), the gate
suggests taking it over. At `max_gate_revisions` (3) it can't be sent back again: a person takes it
over or stops the feature. Each send-back also hands the agents the earlier rounds' feedback, so a
second round doesn't undo the first.

**Taking over.** Take over opens the agents' draft in an editor. Edit it and hand it back: your
version is saved as the next version of the document and the pipeline carries on from it (tasks
from your plan, touched lanes from your HLD; at G4 the design review is redone on your version).
An escalation (a phase that failed its checks `max_loops` times, or ran out of budget) works the
same way: Finish it myself opens the last draft to complete.

**Learning.** Every correction is recorded (product, feature, phase, gate, reason, feedback). A
lesson is proposed when:

- a person ticks **Remember this for future features** (with the lesson in their own words, or
  written from their feedback);
- a person took a phase over and changed the draft (the sections they added and changed, and the
  diff, are the evidence);
- the same phase was sent back for the same reason on 3 features (`lessons.REPEAT_FEATURES`).

The knowledge agent turns the correction into one instruction (in stub mode a fixed rule does) and
the tool opens a pull request in the product's governance repo with one file per lesson:

```yaml
# <product>-governance/lessons/L-004.yaml
id: L-004
phase: spec
text: Every spec says how long each kind of data is kept.
check: true          # also a must-have item on the critic's checklist for this phase
sources: [FARM-FEAT-12 G2 takeover by ana (missing_requirement)]
```

People edit the wording and merge, or close it. Merged lessons are governance: features started
after the merge pin them with the rest, their agents for that phase are told to follow them, and
lessons with `check: true` join the critic's checklist (a document that ignores one is sent back
automatically, before a person sees it). The **Lessons** page lists lessons in use and proposals
waiting for review, with their pull requests (`GET /api/products/{id}/lessons`). Over the API, any
decision takes `remember: true` and `lesson`, and a hand-back takes `artifact_content`.

## How the design maps to code

| Design element | Where | Notes |
| --- | --- | --- |
| Supervisor, phases, gates | `graph/pipeline.py` | Gates and escalations are `interrupt()` nodes; Design and Architecture run in parallel and join before Plan |
| Phase subgraphs | `graph/phase.py` | Each phase is a compiled subgraph: produce, deterministic checks, critic, loop up to `max_loops`, else escalate |
| Build fan-out | `graph/phase.py` | One coder agent per task through `Send`; a reviewer agent per PR; only failing tasks are retried |
| Agent roster and skills | `agents/roles.py` | Plugin skills per phase; critic skills double as evaluators |
| Performance tests | `performance.py`, `config/quality.yaml` | Budgets over industry defaults, the rules that pick none, smoke or full per repo, the PR labels and the staging request |
| Takeover and lessons | `graph/pipeline.py`, `lessons.py` | Gate history and revision limits; the person's version kept on hand-back; corrections recorded, lessons distilled and proposed as governance PRs, then pinned per feature into prompts and the critic's checklist |
| Fixing failures before production | `graph/pipeline.py` (`fix_or_ask`, `failing_repos`) | Failures in CI, the build, dev and staging go back to the coders of the repos they point at, on a fix branch after merge; people decide when the tries run out |
| Workflow profiles | `profiles.py`, `graph/pipeline.py` | Feature and express routes; the profile brief, the gates a profile decides, and the guard that keeps architecture, G5 and G6 |
| Agent notes | `diary.py`, `graph/phase.py` | The `## Agent notes` section and the coder's notes, kept per phase and shown at the gates and in PR descriptions |
| Traceability | `traceability.py` | Metrics, goals, criteria, tasks, PRs, acceptance results, deployments and incidents in one view, with gaps; reverse lookup by repo or PR |
| Pilot | `pilot.py` | People's effort, the shadow automatic-approval rule, and the report against the 15% target |
| Evaluators | `evaluators/` | Deterministic checks run first; the checklist critic runs in a fresh context (judge independence, tier 1) and its quotes are verified against the document |
| Model gateway | `gateway/gateway.py` | Role-to-model registry (`config/models.yaml`), data-class allowlist (VX-7), one ledger record per call |
| Cache-first prompt layout | `gateway/prompt.py` | Six layers, breakpoints after L2, L4 and L5, chained prefix hashes recorded on every call |
| Usage ledger | `gateway/ledger.py` | In-memory or Postgres; `summarize()` gives cache-hit rate, write share, cost by phase and model |
| Runtimes | `runtime/` | `GatewayRuntime` (generic, any model) and `ClaudeCodeRuntime` (Claude Agent SDK, live mode) |
| Product isolation | `config/registry.yaml`, each org's `product.yaml` | Registry: lane kind, org, data class, workspace. Manifest: repos, approvers, budget, thresholds, high-risk paths, runtime, `depends_on` |
| Shared services | `governance/loader.py`, `graph/pipeline.py` | Dependency lanes pinned at intake; their constitutions apply; G4 to G6 need each touched lane; shared repos publish packages (`integrations/packages.py`) |
| Constitutions | `governance/` | Read from the tools org and each product org, validated, pinned by commit SHA per feature |
| Risk-tiered merge | `graph/phase.py`, `graph/pipeline.py` | PHI and payments are `regulated`; changes to high-risk paths are `high`; both go through G5 |
| Multi-repo change set | `graph/pipeline.py` | One branch name across repos; the merge train runs libraries, then providers, then consumers |
| CI/CD | `integrations/openchoreo.py` | Agents promote to staging only; production promotion at G6 is human (VX-3). A production rollback is a person's, or the tool's under the remediation policy |
| Knowledge write-back | `graph/phase.py` | After Operate, the capability is registered in the catalog |
| Durability | `api/app.py` | `PostgresSaver` checkpointer; threads survive restarts and wait at gates for days |
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

**G3 and G4 are decided as a pair.** They wait in the same graph step, so a `revise` at one takes
effect once the other is also decided. An earlier approval of the other gate still counts.

## Modes

- **stub** (default): `StubProvider` returns each phase's deterministic output and simulates prompt
  caching (exact-prefix match at breakpoints, TTL refreshed on hits, isolated per workspace). Use
  `simulate_faults` on intake, for example `{"spec": 1, "build": 1}`, to exercise loops and
  escalations.
- **live**: `pip install -e ".[live]"`, then set `VRIXEL_MODE=live` and `ANTHROPIC_API_KEY`. Roles
  with `runtime: claude_code` run through the Claude Agent SDK; others call the Claude API through
  the gateway. Skills come from the plugins in `VRIXEL_SKILLS_DIR` (`<plugin>/skills/<skill>/SKILL.md`,
  or `<plugin>/commands/<skill>.md`). The image installs Anthropic's Product Management,
  Engineering and Design plugins ([knowledge-work-plugins](https://github.com/anthropics/knowledge-work-plugins),
  pinned by commit in `config/plugins.yaml`) and ours, `plugins/vrixel` (the critics' skills), at
  `/opt/vrixel/plugins`, skills and commands only: `plugins/prepare.sh` leaves out each plugin's
  connectors (`.mcp.json`: Slack, Linear, Atlassian and others) and hooks, so an agent working on
  product code reaches no outside tool through a plugin. Live mode refuses to start while a skill a
  phase uses is missing; CI checks the pinned commit has them all. Live mode is not covered by the
  offline tests. Run it against a sandbox product first.

  **The plugin pin is config.** Moving `knowledge_work_plugins.ref` in `config/plugins.yaml`
  changes every agent's instructions, so it goes the way a model or prompt change does: a pull
  request to the config repo, replayed against past documents (`vrixel / evals`), and if wanted
  the canary. A config that pins a commit other than the image's (an eval run's candidate, the
  canary) gets its own copy at that commit, fetched once from GitHub and laid out by
  `plugins/prepare.sh` like the image's. The Dockerfile's `KNOWLEDGE_WORK_PLUGINS_REF` names the
  same commit (a test checks; CI builds with the config's), the image records it in
  `/opt/vrixel/plugins/REF`, and strict mode warns when the two differ. The ref must be a full
  commit sha. Changes to our own plugin (`plugins/vrixel`) ship with the tool and are not replayed.

  **Skills a session loads by itself.** Besides the skills its phase pastes in, a session sees
  every installed skill by name and may load one. `on_demand` in `config/plugins.yaml` turns that
  off for the roles you list (for example the critic, so it judges against its checklist only).
  Whatever a session does load is recorded as `extra_skills`: on its ledger rows, on its trace
  (`vrixel.extra_skills`), and in the feature's agent sessions, which G5 and G6 show.

  A Claude Code session starts from Claude Code's own system prompt (the `claude_code` preset:
  tool use, safety) with the layers L2-L5 appended: the phase's skills in full, the
  constitution, the product and the feature. It reads settings from the repository only
  (`setting_sources=["project"]`: the checkout's CLAUDE.md and `.claude/skills/`, nothing from
  the container's home), and loads every installed plugin unless `on_demand` is off for the role,
  so the agent can also pull in a skill the phase didn't list (it sees only names and descriptions
  until it uses one). The plugins carry skills and commands only, so they give the agent no
  connector, hook or other outside tool. Keep the appended layers under about
  120 KB: the SDK passes them as one process argument, and the runtime warns before the limit.

- **strict**: set `VRIXEL_STRICT=1` in production. The switches above are independent, and some
  fall back quietly: a Jira, OpenProject or Lucid server without credentials runs as an offline
  stand-in, a product's flags run in memory, no `DATABASE_URL` keeps everything in memory. Strict
  mode refuses to start while any of them would, and lists each one: `VRIXEL_MODE`,
  `VRIXEL_DELIVERY`, `VRIXEL_AUTH` and `VRIXEL_GOVERNANCE_SOURCE` not live, no database, and per
  product a diagram or tracker without credentials, Flipt without its token, or ServiceNow without
  `VRIXEL_SERVICENOW_AUTH_<ID>`. What runs but can't be checked (a shared library that declares no
  package) is logged as a warning. `/api/healthz` reports `strict` (`vrixel_sdlc/strict.py`).

## Replacing the stubs

| Stub | Replace with | Interface |
| --- | --- | --- |
| `InMemoryCatalog` (no `DATABASE_URL` only) | `PostgresCatalog` with a database; a mirror to the Backstage catalog in the OpenChoreo portal is still open | `integrations/catalog.py` |
| `StubPackageRegistry` (stub delivery only) | Release tags on GitHub; packages checked in GitHub Packages | `integrations/packages.py` |
| `StubFlags` (stub delivery only) | Each product's Flipt (`tools.flags` in product.yaml) | `integrations/flags.py` |
| Phase templates | Real agent output in live mode; keep the section structure the checks read | `agents/templates.py` |
| Build tasks | Coder agents in sandboxed containers with a checkout of one repo each | `runtime/claude_code.py` |

Have compliance counsel review the regulatory constitution articles.

## Layout

```
config/            knowledge.yaml (design knowledge sources), repos.yaml (golden path for new repos), quality.yaml (test and security suites, floors per data class), tools.yaml (MCP servers, tools allowed per data class), registry.yaml (governance repo per product, platform limits), models.yaml (role registry)
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
  repo_roles.py    repo roles (library, provider, event-worker, mcp-server, agent, consumer, bff, acceptance) and merge order
  acceptance.py    product acceptance tests: plan checks, overlap, test selection, run reports
  quality/         test and security suites: policy, sources, gate
  knowledge/       design knowledge: architecture record, contracts, design check, write-back
  worksync.py      keeps the work tracker in step with each feature
  api/app.py       FastAPI service under /api; serves web/dist
  web/auth.py      GitHub OAuth, signed sessions, approver matching
  intake.py        guided intake agent
  duplicates.py    finds features and capabilities a new request repeats
  roadmap/         goals, initiatives (G0) and releases: record, workflows, views
  ops/             production signals, incidents, hotfixes and intent changes
  events.py        live-update bus
  cli.py           vrixel demo
web/               React app: decisions, features, intake
tests/             pipeline, gateway, governance, API, Postgres durability
```

## License

Vrixel SDLC is licensed under the [Apache License, Version 2.0](LICENSE). See [NOTICE](NOTICE).
Contributions are accepted under the same license. The license gives no rights to the Vrixel name
or logo.

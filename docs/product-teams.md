# Delivering products with Valxel

For product teams: governance and lanes, inception and the roadmap, how a feature is designed, built, tested and released, production issues, observability, cost, and the rest of what the tool does for a product.

Back to the [README](../README.md).

## Contents

- [Organizations and product governance](#organizations-and-product-governance)
- [New repositories](#new-repositories)
- [Data and events](#data-and-events)
- [Agentic features in products](#agentic-features-in-products)
- [Design knowledge: building on what exists](#design-knowledge-building-on-what-exists)
- [Product inception: from a vision to a roadmap](#product-inception-from-a-vision-to-a-roadmap)
- [Roadmap: goals, initiatives and releases](#roadmap-goals-initiatives-and-releases)
- [Production issues: incidents, hotfixes and intent changes](#production-issues-incidents-hotfixes-and-intent-changes)
- [The template contract: what every template brings](#the-template-contract-what-every-template-brings)
- [Keeping repos on the standards](#keeping-repos-on-the-standards)
- [Delivery: GitHub and OpenChoreo](#delivery-github-and-openchoreo)
- [Independent review of every pull request](#independent-review-of-every-pull-request)
- [Tests and security scans](#tests-and-security-scans)
- [One incident per failure](#one-incident-per-failure)
- [Error tracking: Sentry or self-hosted GlitchTip](#error-tracking-sentry-or-self-hosted-glitchtip)
- [Component alert rules: logs and exceptions](#component-alert-rules-logs-and-exceptions)
- [AWS services: alarms](#aws-services-alarms)
- [Gradual releases](#gradual-releases)
- [Change management: every production change, and looking back](#change-management-every-production-change-and-looking-back)
- [Resource inventory: what runs where](#resource-inventory-what-runs-where)
- [Cost: what features and products cost](#cost-what-features-and-products-cost)
- [AWS services (VX-19)](#aws-services-vx-19)
- [Store apps (VX-18)](#store-apps-vx-18)
- [Acceptance tests across repos](#acceptance-tests-across-repos)
- [The Backstage catalog: Git is the source](#the-backstage-catalog-git-is-the-source)
- [Diagrams and work tracking](#diagrams-and-work-tracking)
- [Performance tests](#performance-tests)
- [Workflow profiles: feature and express](#workflow-profiles-feature-and-express)
- [Agent notes for the gates](#agent-notes-for-the-gates)
- [Traceability](#traceability)
- [Taking over, and learning from people's corrections](#taking-over-and-learning-from-peoples-corrections)

## Organizations and product governance

The products in this repo (Clinic records, Farm app, Shop, Dental, Payments platform and Shared
services, in the `acme-*` orgs) are fictional. They exist so the tests and the demo exercise every
data class and lane shape. To run the tool for your own products, put your `registry.yaml` (and
the rest of `config/`) in a private repo and point `VALXEL_CONFIG_DIR` at it.

The tool lives in the tools org. It holds no product constitutions or product settings: each
product owns a governance repo in its own GitHub organization, and the tool reads it. Governance
repos are named after the product id: `cln-governance`, `farm-governance`, `ss-governance`
(`repo:` in `config/registry.yaml`; that name is the default when it's left out).

```
valxel-platform/                          tools org
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

**Access.** Set `VALXEL_GOVERNANCE_SOURCE=github` and choose one of these:
- *GitHub App (recommended).* Install one app in the tools org and every product org, with
  read access to Contents. Set `VALXEL_GITHUB_APP_ID` and `VALXEL_GITHUB_APP_PRIVATE_KEY_FILE`.
  The tool mints a short-lived `contents:read` token per org.
- *Tokens.* `VALXEL_GITHUB_TOKEN__<ORG>` per org (upper case, `-` as `_`), or one
  `VALXEL_GITHUB_TOKEN`.

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
| Source | Generated from the role's template (`valxel-platform/template-service`, `-web`, `-bff`, `-library`, `-event-worker`, `-mcp-server`, `-agent`, `-acceptance`), public. Public repos get rulesets, auto-merge, CodeQL and secret scanning on any GitHub plan; secrets and patient data never go in a repo |
| Name | The template's name replaced by the component's in the files the template lists in `.valxel/identity` (its Workload, app and component name), so its logs, traces and error reports name it |
| License | The template's own `LICENSE` (MIT, Valxel's) removed: the product's code is its owner's, under terms they choose |
| Merging | Squash only, titled after the pull request; branches deleted after merge; auto-merge and "Update branch" on |
| Features | No wiki, projects or discussions: work lives in the tracker, docs in the repo |
| Security | Dependency graph, Dependabot alerts and security updates, secret scanning with push protection, CodeQL default setup |
| Branch ruleset | Pull requests only, merged when their required checks pass: no GitHub review, because people approve in the pipeline's gates (G1, G4, G6; G5 for high-risk and regulated changes) and agents push and merge under one App identity; linear history; no force pushes or deletion; required checks per role; merges blocked on new code-scanning alerts of high or above; repository admins may merge a pull request whose checks haven't passed |
| Release tags | `v*` tags can't be moved or deleted |
| Ownership | CODEOWNERS from the gate approvers (tech leads own everything; security owns the high-risk paths) |
| Access | Teams from the approvers: tech leads maintain, architects and security push, product owners read |
| Labels | Topics, and org custom properties (`valxel_managed`, `valxel_product`, `valxel_data_class`) that org-level rulesets can target |
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
1. Publish the templates named in `config/repos.yaml` to `valxel-platform` and mark each one a
   template repository: `template-service` and `template-event-worker` (Java 21, Spring Boot),
   `template-web` (React, TypeScript), `template-library` (Java), and `template-mcp-server`,
   `template-agent`, `template-knowledge`, `template-reporting`, `template-job`,
   `template-integration`, `template-code-sandbox`, `template-document-intake` and
   `template-channel` (Python).
   Their CI jobs are named for the required checks
   (`unit`, `contract`) and post `unit / report`-style statuses the quality suites read; their
   `post-deploy.yml` runs integration, e2e and smoke suites when the pipeline dispatches
   `valxel-deployed` after a deployment is ready.
2. Create the custom properties `valxel_managed`, `valxel_product` and `valxel_data_class` in each
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
| `provider` | `valxel-platform/template-service` | `unit`, `contract`, `CodeQL` | Versioned API; commands publish CloudEvents with `Events.publish` (idempotent producer, acks=all, keyed by record id); 503 and no change when Kafka doesn't confirm |
| `event-worker` | `valxel-platform/template-event-worker` | `unit`, `CodeQL` | Applies events to Postgres (or an index) once and in order: event ids recorded in the same transaction, rows keep their Kafka position, retries then a dead-letter topic; integration tests against real Postgres and Kafka |

An HLD with event workers has an `## Events` section, one line per event type
(`- valxel.<entity>.<what happened>.v1: <who publishes it, who applies it>`). Coders are told to
publish or apply those events, reviewers check VX-16, and the build fails if a named event isn't
published or applied by any task. Services coordinate through events; a central orchestrator needs
an ADR.

### Scheduled jobs

Work due at a time goes in a `job` repo, not inside a service's timer. Reminders, nightly
reconciliations, retention purges and report builds are examples. Work that reacts to events goes
in an event worker.

| Role | Template | Required checks | What it is |
| --- | --- | --- | --- |
| `job` | `valxel-platform/template-job` | `unit`, `CodeQL` | One run, then exit, as an OpenChoreo `cronjob/scheduled-task`. A Postgres lock keeps runs from overlapping. Each run covers the time since the last success, so a run after an outage makes up for the missed ones. A time limit stops runs cleanly, and every run is recorded in `job_runs` |

The design names each job's schedule under `## Schedules`:
`- <org>/<repo>: <minute> <hour> <day of month> <month> <day of week> <time zone>; <what it does>`.
The fields are numbers only, and the time zone is an IANA name. G4 refuses a job repo with no
schedule, a schedule Kubernetes wouldn't accept, or a line that doesn't say what the job does. The
coder writes that schedule into the repo's `job.yaml`.

On every deploy, the tool reads `job.yaml` at the commit it deploys and sets the CronJob's settings
for the environment (`jobs.py`): its schedule, time zone, time limit and `suspend`. `job.yaml` can
give dev and staging their own schedules, or suspend them. A schedule change is therefore a pull
request like any other.

Two platform alert rules watch jobs: `job-failed` (a run ended in an error) and `job-missed`
(`missed_after` scheduled runs passed without a success). In production, each opens an incident.

### Conversation channels

People talk to the product (often its agent) over Slack, Teams, SMS or email through one `channel`
repo.

| Role | Template | Required checks | What it is |
| --- | --- | --- | --- |
| `channel` | `valxel-platform/template-channel` | `unit`, `CodeQL` | Each provider's own signature or token is checked before a message is read. Only people who linked their identity, by signing in through the BFF, reach the product. STOP, START and HELP are honoured, and nothing goes to opted-out people. Addresses are hashed and encrypted. Messages and replies travel as data contracts |

The design names each channel and what it carries under `## Channels`:
`- <org>/<repo>: slack full baa; sms notice; email notice`. `notice` sends a sign-in notice instead
of the text, and in a PHI product `full` needs `baa`. A channel repo is `external` in Endpoints
without an ADR. The build fails when `channel.yaml` adds a channel, sends full text where the design
said notice, or claims a BAA the design didn't record. On every deploy, its routes
(`/channels/<kind>`), its egress (the providers' hosts) and its one in-product caller (the BFF) come
from `channel.yaml`, on the connector's component type. Alert rules:
`channel-signatures-rejected`, `channel-replies-parked`, `channel-provider-unavailable`.

### Document intake

Documents people upload (a lab report, an insurance card, a referral letter) go through one
`document-intake` repo.

| Role | Template | Required checks | What it is |
| --- | --- | --- | --- |
| `document-intake` | `valxel-platform/template-document-intake` | `unit`, `CodeQL` | Presigned uploads to a quarantine bucket. Before anything reads a file, it is sized, its format is read from content, and it is scanned for malware (ClamAV). Clean files move to a KMS-encrypted bucket. Fields are read through the LLM gateway; low-confidence fields go to a review queue (never reviewed by the uploader); fields are published as a data contract. Tenant-scoped reads, one-minute downloads, retention purge, all audited by id |

The design names each document type under `## Documents`:
`- <org>/<repo>: <type>; formats pdf, png; max <N> MB; keep <N> days; dataset <name>; fields <name>*, <name>`.
G4 also wants two `s3-bucket`s in Resources that the intake reads, writes and deletes: a quarantine
with `expire_days` of at most 7, and a clean bucket. The intake is `project`-visible, reached
through the BFF. The coder writes the lines into `intake.yaml`, and the build fails when the file
takes more than the design approved: another type or format, a larger size, a different retention
or dataset, or more fields. Alert rules: `intake-malware-found` (critical),
`intake-checks-unavailable`, `intake-documents-failed`. The platform runs ClamAV
(`clamav.platform-security`) for every product with an intake.

### Connectors to third parties

A lab, a payer, a CRM or a payment processor is reached through one `connector` repo. Nothing else in
the product calls the vendor, and the vendor calls nothing else. They meet as data contracts on Kafka.

| Role | Template | Required checks | What it is |
| --- | --- | --- | --- |
| `connector` | `valxel-platform/template-integration` | `unit`, `CodeQL` | Signed webhooks in: HMAC over the timestamp and raw body, a replay window, a size cap, a rate limit, dedupe by delivery id, then an outbox. Vendor calls out: https to allowed hosts only, no redirects, OAuth client credentials, an API key or mTLS, retries, a rate limit, a circuit breaker and Idempotency-Key. Contracts are checked both ways, and refusals are parked on `<topic>.dlt` |

The design names what may cross under `## Integrations`:
`- <org>/<repo>: <vendor>; data <general|payments|phi>; baa <yes|no>; webhooks <name, ...|none>;
calls <host, ...|none>; auth <oauth2-client-credentials|api-key|mtls|none>`.

**G4 refuses:**
- PHI without `baa yes`;
- hosts given as URLs, wildcards, IP addresses or platform-internal names;
- `auth none` for PHI or payments;
- a connector with no line, or a line for a repo that isn't a connector.

In Endpoints, a connector is `external` exactly when it receives webhooks, and needs no ADR for it.

What's approved is enforced three times (`connectors.py`):
- **Coder.** The coder writes the line into `integration.yaml`.
- **Build.** The build fails when the file on the branch allows more than the line: another host,
  another webhook, a lower data class, other auth.
- **Deploy.** On every deploy, the tool reads `integration.yaml` at the deployed commit. It sets the
  binding's `webhookPaths`, the only routes the gateway exposes, and its `egressHosts`, the only
  hosts its FQDN egress policy allows (example type `deployment/connector`).

Vendor secrets are mounted files, never environment variables. Its post-deploy suite checks from
outside that:
- each webhook refuses unsigned and wrongly signed deliveries;
- health and metrics aren't reachable.

Four alert rules watch connectors:
- `connector-signatures-rejected`
- `connector-deliveries-refused`
- `connector-events-parked`
- `connector-vendor-unavailable`

## Agentic features in products

Products build their own agentic features with two more repo roles, so an HLD can propose them
like any other repo and the provision step creates them on the golden path:

| Role | Template | Required checks | What it is |
| --- | --- | --- | --- |
| `mcp-server` | `valxel-platform/template-mcp-server` | `unit`, `CodeQL` | Product capabilities as MCP tools over Streamable HTTP: OAuth scopes, annotated tools, idempotent writes, audit, PHI-minimal results |
| `agent` | `valxel-platform/template-agent` | `unit`, `evals`, `CodeQL` | A durable LangGraph workflow agent: MCP tools on an allowlist, models through the product's LLM gateway, a person approves every change |
| `knowledge` | `valxel-platform/template-knowledge` | `unit`, `retrieval`, `CodeQL` | Retrieval over the product's own records: an index kept from its events (or a scan of a document store) in its Postgres, searched as MCP tools (`search`, `get_document`, `list_sources`), filtered by who is asking with row-level security |
| `code-sandbox` | `valxel-platform/template-code-sandbox` | `unit`, `CodeQL` | Where agents run the code they write: the MCP tool `run_code`, with a fresh single-use Agent Sandbox per run (gVisor, no network, no service account token, CPU, memory, time and output limits). Only the agents the design names may call it; each run is audited by the code's SHA-256 |

An agent never runs model-written code in its own pod: the agent component type, its coder rules and
its review all say so. When the HLD has an agent run code (a code interpreter, generated Python over
a table), G4 wants a `code-sandbox` in the design or the product, called by that agent in Endpoints.
A code sandbox is `project`-visible, calls nothing, and is called by agents only; its coder sets
`SANDBOX_ALLOWED_CLIENTS` to exactly those agents. The platform's `SandboxTemplate` and warm pool
(example `agent-sandbox/code-runner.yaml`) give each run its isolation; the template's post-deploy
suite proves it from inside real sandboxes (no network or DNS, no service account token, a clean
start every run). Alert rules: `sandbox-pool-exhausted`, `sandbox-claims-left`.

Roles merge and deploy in dependency order: library, provider, event-worker, knowledge, mcp-server, agent, consumer, bff, acceptance (merged, never deployed).

**Knowledge repos** (`src/valxel_sdlc/retrieval.py`) are built by Valxel during the product build:
- **Architecture (G4).** When an HLD needs search over product text, the architecture agent lists
  a `knowledge` repo. Under `## Knowledge` it names the embedding model and the sources:
  `- <org>/<repo>: model <model> via <gateway (BAA)|gateway|self-hosted>; sources: notes (events emr.notes.v1), ...`.
  People decide the model at G4. A PHI product embeds through the gateway under the BAA or with
  its self-hosted model, and the evaluator says so when the HLD doesn't.
  The agent also decides, for each events source, how a rebuild gets every record. A rebuild
  replays the topic from its start, and ordinary retention would silently drop older records. So
  the source is either a compacted topic (`notes (events emr.notes.v1, compacted)`: whole records
  keyed by id) or a scan of the service's database. The evaluator refuses an events source with
  neither, and people approve the choice at G4. The provider that publishes a compacted source is
  told to key events by record id, carry whole records, send deletions as `.deleted` events, and
  declare the topic `cleanup.policy=compact`. The knowledge repo checks the topic at run time.
- **Build (G5).** Coder agents write the repo's `knowledge.yaml` from that line and the product's
  data model: sources, text, tenant and access fields, and excluded fields. They also draft the
  golden set (`evals/questions.yaml`), which has recall questions, a leak case for every tenant and
  team boundary, and a deletion case. People approve both with the code at G5.
- **Checks.** The `retrieval` suite runs the golden set on every PR. A leak or a deleted record
  fails it whatever the recall (`report.py --must-pass`). After the dev deploy, the same golden set
  runs with the real embedding model and reranker. G6 shows the recall beside the last release's.
- **Production.** A source more than an hour behind the product raises the `knowledge-lag` alert
  rule. It applies only to knowledge components (`roles:` in `platform/alert-rules.yaml`).
**Data contracts** (`src/valxel_sdlc/datasets.py`). Every event type is a dataset, written as an
[ODCS v3](https://bitol-io.github.io/open-data-contract-standard/) data contract with Valxel's
extensions (owner, events, compacted, retention, tenant, consumers) in its `customProperties`:
- **Architecture (G4).** The architecture agent writes one contract per topic under the HLD's
  `## Datasets` (a fenced yaml block each). Every property has a class: `none`, `pii`,
  `financial`, `phi` or `secret`. The evaluator checks each one against the standard's JSON Schema
  and Valxel's profile. It also checks that every `## Events` line and every knowledge events
  source has a contract, that the publisher is the contract's owner, and that every repo that
  applies or indexes the topic is a listed consumer. A contract already released must stay
  compatible within its event version: only optional properties, new enum values and stricter
  classes may be added, with a higher version. Anything else is a new event type, topic and
  contract.
- **Release.** Approved contracts go to the governance repo's `datasets/<id>.yaml` in the same
  pull request as the feature's decisions. The governance repo's managed `datasets` workflow runs
  the same module (`.github/scripts/datasets.py`) on every change, comparing with `main`.
- The schema components validate against is generated from the contract (`json_schema`), and
  refuses any property the contract doesn't have, so nothing unclassified is published.
- **Reconciliation.** Templates log `valxel.datasets {"<dataset>":{"<outcome>":n}}` once a minute
  (counts only). After staging passes, `reconcile.py` sums these per component. Whatever a
  contract's owner published must have reached every consumer, and nothing may have broken the
  contract (refused or parked). Otherwise the release is held like a failed suite: the agents fix
  the repos it names, and people decide when the tries run out. G6 shows the result. In production
  the `contract-broken` alert rule watches for the same breaks.
- **Topics and access.** Each contract becomes a `kafka-topic` Resource (the catalog's
  `from_contracts` type, which an architect can't list under Resources). Its owner gets write
  access, its consumers get read, and each component it names gets a `kafka-user`. On MSK the
  access is an IAM policy on the component's workload identity. On Strimzi it is a SCRAM user with
  ACLs (valxel-platform/platform-resources). A topic or group the contracts don't give a
  component is refused by Kafka itself. Consumer groups start with the component's name.


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

## Product inception: from a vision to a roadmap

A new product, or an existing one that never had a product-level design, starts with three
documents, each written by an agent, checked by machine and by the critic, and revised until both
pass (`valxel_sdlc/inception/`):

1. **Product brief** (strategist): the problem, users, goals with numeric measures, non-goals, scope,
   the volumes (`## Scale`: how many at launch and after a year, how busy the busiest hour is), the
   availability tier with its reason, the data class with its reason, compliance, risks, and the
   product's id, org, repo prefix, budget and run budget per production stage. The org must already
   exist; the person names it.
2. **Product architecture** (architect): components with their repo roles and repos, AWS resources
   from the platform catalog (checked against it and against the data class), shared services, NFRs
   with numbers, the load per user journey (`## Load`), how each component scales and what runs out
   first (`## Scaling`), what happens when each dependency fails and how each data store recovers
   (`## Failure`), and founding ADRs with their alternatives. The tool computes the capacity plan
   from the volumes and the load (see below) and adds a `## Capacity` section, prices the resources
   per environment from the catalog and adds a `## Monthly cost` table. An existing product also
   writes `## Current state` and `## Gap`.
3. **Roadmap** (strategist): initiatives in order, each with its horizon, budget, goals, what must come
   first and its candidate features, and which components each feature builds. The checks make sure
   every goal is served, every component is built, budgets fit the brief's, and (for a new product)
   the first initiative is a thin working version through at least two components.

Before the brief, a **research note** (`valxel_sdlc/research.py`): who has the problem, how they solve
it today (alternatives and their public pricing), market signals, and the evidence as numbered
claims, each with a confidence and a source (a web page it read, the request, an attachment, or
"unsourced" at low confidence), plus the assumptions the plan bets on, each with a test. The person
picks the depth: `quick` (a few searches, the default), `thorough` (more alternatives, pricing,
market size) or `own` (no searching: their own research, put in the same form). Web search runs
where the runtime has it (Claude Code, or the Claude API's web search tool at $10 per 1,000
searches, added to the ledger); elsewhere and in offline mode the note says nothing was searched.
Searches go to a search engine no BAA covers, so a PHI product's attachments stay out of a research
call that searches. The brief then has `## Evidence` (every claim cites the note: `(E1, E3)`) and
`## Assumptions` (`- A1: We believe ...; test: ...`); the checks and the critic hold it to both,
the brief gate shows the note, and setup records it as `architecture/research.md`. New initiatives
work the same way: research, then a draft citing it, the note shown at G0 and recorded as
`roadmap/research/<id>.md`, and the initiative's review lists its assumptions so the council can
say which held.

The product council decides twice: on the brief before any architecture is written (the brief
gate), then on the architecture and roadmap together (the product gate). Until the brief is
approved every call runs as PHI, so only models under a BAA see the vision.

From the command line, in offline mode by default (`--mode live` for real models):

```bash
valxel inception --vision vision.md --org acme-water --out out            # the brief, for the brief gate
valxel inception --vision vision.md --org acme-water --brief out/BRIEF.md --out out   # architecture and roadmap
valxel inception --product FARM --vision next.md --out farm               # an existing product
```

A vision is Markdown: `# Name`, the idea, then optional `## Goals` (`- <outcome> (measure: <metric>)`),
`## Users`, `## Constraints` and `## Budget`. The documents and `report.json` (status, attempts,
findings, the critic's checklist, cost) land in `--out`.

**In the web app**, **Products** starts one (for the teams in `inception.starters` in
`config/registry.yaml`): the agents write the brief, and the **brief gate** waits in Decisions for
the product council (`inception.council`). Approving it starts the architecture and roadmap; the
**product gate** shows both, with the monthly cost. Sending a gate back redrafts with the feedback;
stopping writes nothing. Approving the product gate runs setup (`valxel_sdlc/inception/setup.py`):

- creates `<org>/<prefix>governance` and opens a pull request with `product.yaml` (approver teams per
  gate, `repo_prefix`, a per-feature budget), a starter constitution, `context.md`, `CODEOWNERS`,
  `architecture/brief.md`, `architecture/product-architecture.md`, `roadmap/goals.yaml` and one file
  per initiative with its candidate features;
- opens a pull request on the config repo (`VALXEL_CONFIG_REPO`) adding the product to
  `config/registry.yaml` (without one, the line to add is shown);
- lists the approver teams an org admin needs to create;
- records the capacity plan as `runtime/capacity.yaml`, and sets up the product's environments (below).

### Capacity and environments

The agents write the assumptions; code does the arithmetic (`valxel_sdlc/capacity.py`), so no
replica count is a model's guess. From the brief's volumes and the architecture's load per journey,
with the platform's tiers, headroom and starting throughput per role (`platform/capacity.yaml` in
the platform governance repo) and the catalog's database sizes, it works out for each component the
busy-hour load at launch and after a year, the pods each stage needs, CPU and memory, the database
size its connection pools need, Kafka partitions (set for year one now, since changing them later
reorders events), and the monthly cost of each stage. The checks stop a plan that doesn't add up: a
component nothing sends load to, pools no database size takes, a stage over the run budget, or a PHI
or payments product below tier 2.

Production grows in stages, so a product with few customers pays little to run:

| Stage | Production runs | Availability |
| --- | --- | --- |
| pilot | one small pod per component, the autoscaler's ceiling at twice the launch busy hour, the small database with point-in-time recovery | tier 3 (99.5%), whatever the target; data protection never relaxes |
| growth | the target tier's floor (2 pods each at tier 2), the Multi-AZ database | the target tier |
| scale | the autoscaler follows load up to the approved maximum | the target tier |

Dev and staging are a fixed small size (1 and 2 pods), on spot nodes, scaled to zero outside
working hours, with a database that pauses when idle (Aurora Serverless v2 at 0 capacity).

All products share one OpenChoreo control plane, workflow plane and non-production observability
plane. Each product gets two data planes of its own (non-production and production) and its own
production observability plane, so production logs never leave it. At the product gate, setup
proposes those clusters to the platform's infrastructure repo (`VALXEL_INFRA_REPO`, from the
platform's `openchoreo/clusters.yaml` template); once the platform registers them, it creates the
product's project, dev and staging on the non-production data plane, production on the production
data plane (for the person who approved the gate, never an agent), and the pipeline. Until then the
step waits; `valxel environments --product CLN --approver maria` finishes it.

Every deployment then carries its environment's part of `runtime/capacity.yaml`: replicas, CPU and
memory in the release binding, and the platform's `autoscaler` trait's range where there is one
(`platform/openchoreo/` has the component types and trait to apply to the control plane). After
launch the plan keeps moving, always by a pull request people merge:

- a feature design that adds load (`- Added load: cln-intake-api +20 requests/s` under
  `## Performance`) shows G4 the maximums it raises, and the release proposes them;
- staging's load test runs at what staging's pods should take and reports what one pod handled;
  after three tests the median replaces the starting figure;
- `valxel capacity --product CLN --observed busy.json` reviews production: the busy hour over half
  the stage's capacity on 5 days in a fortnight proposes the next stage, an autoscaler at its
  maximum proposes a higher one, a month under a fifth proposes stepping down; and
  `valxel capacity --product CLN --step growth --reason "contract promises 99.9%"` proposes a step
  with its cost before and after.

When both merge and the tool reloads its config, the initiatives are on the Roadmap page ready to
start, and each feature creates the repos the architecture names at G4. An existing product gets one
pull request to its own governance repo with the architecture, brief and roadmap.

## Roadmap: goals, initiatives and releases

Above features sit a product's goals and the initiatives that serve them; beside them, releases
decide when users see features. Scope is the product today; ids carry the product, goals may name
a `parent` and the record has a `scope` field, so a portfolio level can sit above products later.

**The record is Git**, in each product's governance repo, changed by pull request like ADRs:

```
roadmap/
  goals.yaml                outcomes for a period: id, title, measure, period, metric (parent: later)
  initiatives/INI-CLN-1.md  one approved initiative: goal, horizon (now/next/later), budget envelope,
                            lanes, release model, success measure, candidate features
  releases/REL-CLN-1.md     one release: launch group or release train, features, status, notes, evidence
```

The tool runs initiatives and releases as threads alongside features, computes progress and spend
from the features themselves, and mirrors initiatives to Jira or OpenProject (an item one level
above the feature epics; set `initiative_type` in `tools.work_tracking` if your tracker calls it
something else) and releases as comments on member epics.

**Goals the tool can read.** Each goal keeps its `measure` (the sentence people read) and may add
a `metric`: how the tool reads it. This is step 1 of the outcome loop (readouts after release,
initiative status and review decisions build on it).

```yaml
goals:
  - id: GOAL-CLN-1
    measure: Median front-desk check-in time under 3 minutes
    metric:
      source: sql                 # sql | prometheus | posthog | manual
      query: |
        SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY minutes) AS value, count(*) AS n
        FROM visits WHERE arrived_at >= :start AND arrived_at < :end
      unit: minutes
      direction: down             # which way is better
      baseline: 6.5
      target: 3
      by: 2027-06-30
```

The product names its sources in product.yaml, and adds no new tool by default: most outcomes are
already business records, so `sql` reads the product's read replica; `prometheus` reads behaviour
counters from the collector the product already runs; `posthog` is optional; `manual` needs
nothing (the owner types the number in).

```yaml
tools:
  metrics:
    sql: {}                       # DSN: platform secret VALXEL_METRICS_DSN_<PRODUCT_ID>
    prometheus: { url: https://prometheus.cln.example.com }   # token: VALXEL_PROMETHEUS_TOKEN_<ID>
    min_cell: 11                  # counts 1-10 show as "under 11": fixed at 11+ for PHI, off by default otherwise
```

The tool only ever holds aggregates. A query is checked when it is defined: one read-only SELECT
(or one PromQL aggregation with no `by`), filtered on `:start` and `:end`, returning one row named
`value` (plus `n`, the count behind it, for PHI products). It runs read-only with a
statement timeout, and a result with more than one row or any column but `value` and `n` is
refused. PHI products follow the CMS cell-size convention: no count from 1 to 10 is shown (zero
is); other products can switch suppression on with `min_cell`. Credentials are platform secrets, never product.yaml; PostHog isn't approved for PHI or
payments data (config/tools.yaml). Inception writes every goal with a `manual` metric from its
brief's measure, so a new product is measurable from day one.

```
valxel outcomes check CLN                         # every goal's metric, and what's wrong with it
valxel outcomes read CLN GOAL-CLN-1 --days 30     # one reading, recorded (--dry-run: not recorded)
valxel outcomes record CLN GOAL-CLN-2 --value 3.1 --n 812 --start 2026-10-01 --end 2026-11-01
```

**Features that say what they move.** The brief names the product goal a feature moves
(`## Product goal`: `- GOAL-CLN-1: <how>`, or `- None: <why>` for work no goal covers); the check
holds it to the goals in goals.yaml and to its initiative's goal. The PRD then lists the events that
show it (`## Instrumentation`: `- checkin_completed (GOAL-CLN-1): fires when ...; properties: ...`):
every goal the brief names is fed by an event, and properties are categories and numbers, never a
person's name, contact details or ids (PHI and payments events carry no person-level ids at all).
The HLD names the repo that records each event, the coder records it with its template's API
(`ProductEvents.record` in the Java service and worker, `events.record` in the Python agent and
MCP server, `track` in the web and mobile apps, relayed by the BFF) and says so in the patch summary, and after staging passes
the tool counts the events from the templates' per-minute summary line
(`valxel.events {"checkin_completed": 12}`, read through the OpenChoreo observer). G6 shows which
fired and which never did: a goal fed only by silent events can't be measured once it ships.

**Readouts after release.** Once a feature reaches everyone (the pipeline's finalize step), the
tool reads its goal after each window the product sets (`tools.metrics.windows`, default
`[7, 30, 90]` days): the metric over the window after release against the same length of time just
before it (or the goal's baseline when there is no reading before), the change, and whether it went
toward the target. A before-and-after readout says it isn't proof the feature caused the change;
flag cohorts are the comparison. Each goal also gets a reading of the last 30 days once a week: its
trend. The watcher takes both daily (`engine.collect_outcomes`).

The Roadmap shows each goal's latest reading, baseline, target, trend and status, and each
approved initiative's status since its approval:

| Status | When |
| --- | --- |
| On track | moved toward the target by at least a quarter of the share of time gone to `by` (and at least 5% of the gap) |
| Flat | moved less than that either way |
| Off track | moved away from the target by more than 5% of the gap |
| No data | no reading yet, a suppressed one, or no baseline |

A goal entered by hand asks for its number when a readout is due: the Roadmap's "Numbers to enter"
lists each period, and the product's owners (G1 and G0 approvers) enter it
(`POST /api/products/{id}/goals/{goal}/readings`), which retakes the waiting readouts. A PHI rate
also returns `k`, the count on top, so 2% of 300 (6 people) is suppressed like any small count. The
feature page's Outcome panel shows the goals it moves, its events in staging, its readouts and the
windows still to come.

**Flag comparisons.** During a gradual release, the backend templates count the feature's goal
events per side on the same per-minute line as its requests and errors
(`"on": {"requests": 40, "errors": 0, "events": {"checkin_completed": 9}}`). Each step's history
then says how often users with it on do what the goal counts against users with it off, per 100
requests, with a z score: the one comparison that controls for everything else going on. It is
reported, never a reason to stop; errors and latency still stop a release.

**Initiative reviews.** On an initiative's review date (`review_by` in its record, else 90 days
after approval), or as soon as its goal reads off track at two readings in a row, the tool opens a
review thread (`CLN-REV-1`) for the product council (registry.yaml `inception.council`, else the
product's G0 approvers). The council sees the goal's status and recent readings, each feature's
readouts, spend against budget, and the features that would be less effort to remove than to keep
(released but still behind a flag that isn't fully on). They decide:

| Decision | What happens |
| --- | --- |
| Continue | the next review date is set (90 days on unless they pick one) |
| Change course | a feature intent opens under the initiative, with their words and the evidence |
| Stop | the initiative is dropped and its features stay; each feature they tick opens a retire intent (remove the flag and its code, migrate the data, draft a customer notice) |

Every decision is recorded on the initiative's file by pull request: its status, its next review
date and a line under `## Reviews`.

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

The platform holds one Flipt client token per product in `VALXEL_FLIPT_TOKEN_<PRODUCT_ID>`
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
With offline delivery, products without Flipt get in-memory flags so the demo works end to end.

## Production issues: incidents, hotfixes and intent changes

The tool watches production through the signals a product already emits, opens an incident, has
agents investigate, and works it to the end by itself where it safely can: it undoes the recent
change behind the failure, checks that the failure stopped, starts the fix and drafts the
postmortem. On-call is paged only when the tool can't fix it (and told, at low urgency, as a SEV1
opens). The tool's own production actions are the platform's one exception to VX-3, set in
sdlc-governance's `platform/remediation.yaml`: turn off a recent feature's flag, roll back a recent
release. Everything else is a person's decision. It acts alone only on two independent views: at least `min_sources`
(2) different senders report the incident, so one alert source, or one leaked token, can't make it
act (it waits `corroboration_minutes` for the second, then pages on-call), and OpenChoreo's
root-cause analysis agrees with triage's suspect (`require_agreement`).

**Signals in.**

| Source | Endpoint | Auth |
| --- | --- | --- |
| Prometheus (Alertmanager webhook; SLO burn-rate and error alerts) | `POST /api/signals/alertmanager` | `Authorization: Bearer <the product's token>` |
| Sentry (internal integration: issues created or regressed, alert rules) | `POST /api/signals/sentry` | `Sentry-Hook-Signature`, HMAC with `VALXEL_SENTRY_CLIENT_SECRET` |
| Support tickets (any tool that can post JSON) | `POST /api/signals/ticket` | `Authorization: Bearer <the product's token>` |
| OpenChoreo alert rules (log, metric and FinOps budget rules; webhook channel without `payloadTemplate`) | `POST /api/signals/openchoreo` | channel header `Authorization: Bearer <the product's token>` |
| CloudWatch alarms on AWS resources (each data plane's `AlarmRouting`: an EventBridge API destination) | `POST /api/signals/cloudwatch` | connection header `Authorization: Bearer <the data plane account's token>` |
| AWS Budgets (through an SNS topic) | `POST /api/signals/aws-budgets` | HTTPS subscription `https://valxel:$VALXEL_SIGNALS_TOKEN@<host>/api/signals/aws-budgets`; topic listed in `VALXEL_AWS_BUDGET_TOPICS` |
| People in the web app | "Report a problem" on the Incidents page | signed-in member of the product's org |

**Each sender has its own token** (`src/valxel_sdlc/ops/signal_tokens.py`), derived from
`VALXEL_SIGNALS_TOKEN`: `valxel signals-token product <id>` for a product's Alertmanager, OpenChoreo
channel and support tool, which then raise that product's signals only (a signal for another
product is refused with 403); `valxel signals-token account <12 digits>` for a data plane account's
CloudWatch and Inspector destinations, which then send that account's events only. A leaked token
reaches one product or one account. The platform-wide token keeps working on these endpoints
until `VALXEL_SIGNALS_TOKENS=scoped` (strict mode asks for it); AWS Budgets keep it, as their topic
is the platform's own. Rotating `VALXEL_SIGNALS_TOKEN` rotates every derived token, and the previous
value's tokens work until the rotation ends.

Each signal is tied to a product (a `valxel_product` label or tag, else its `component` matched to
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
VALXEL_PAGERDUTY_ROUTING_KEYS='{"CLN": "<integration key>", "PAY": "<integration key>"}'
VALXEL_PAGERDUTY_REGION=us          # or eu (events.eu.pagerduty.com)
VALXEL_PUBLIC_URL=https://sdlc.valxel.dev   # pages link back to the incident here
```

PagerDuty owns the schedules, notifications and escalation when nobody acknowledges; set each
service's urgency to "based on alert severity". A page carries the incident id, severity,
component, environment and why on-call is needed, and the title only for products that hold
neither PHI nor payment data: alert titles can quote exception messages, and PagerDuty is outside
those products' data boundary. Sends go out in order on a background thread; throttling, server
errors and network failures are retried for about two minutes. The incident page shows what was
sent, and says so plainly when a product has no PagerDuty service or PagerDuty refused a page.
Without `VALXEL_PAGERDUTY_ROUTING_KEYS` nothing is sent (offline).

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

## The template contract: what every template brings

Security, observability, the audit trail and business metrics come with a repo's template, not
with each feature. A template missing one makes every repo of its role ship without it.
`config/template-contract.yaml` lists the capabilities and the roles that need each:

| Area | Capabilities |
| --- | --- |
| Security | `security-scans` (Semgrep, gitleaks, SonarQube), `dependency-review`, `dependency-updates` (Dependabot with a week's cooldown), `security-policy`, `image-scan` (SBOM, Grype), `supply-chain-pins` |
| Observability | `tracing` (OpenTelemetry), `error-reports` (Sentry or GlitchTip), `json-logs`, `post-deploy` (the stage's suite after each deploy) |
| Audit trail | `audit` (`valxel.audit.v1` events, ids only) |
| Business metrics | `product-events` (`valxel.events`, for goal metrics), `flags` (OpenFeature), `flag-cohorts`, `dataset-counts` |

Each template says where it implements its capabilities in `.valxel/capabilities.yaml`
(`<capability>: <path>`). `valxel repos check` reads that file from every template in
`platform_repos`, and checks that each path exists and contains the capability's marker. A
template that falls behind fails the daily repo-standards run, and so does a new template that
never caught up.

The tool's own rules read the same list:
- Roles with `flags` are told to put changes behind the feature's flag.
- Roles with `audit` are told to publish the HLD's audit records.
- An Instrumentation event may go only to a repo whose role has `product-events`.
- Deployed roles without `post-deploy` (event worker, reporting, job) aren't asked for an
  integration, e2e or smoke report. The product's acceptance tests exercise them, and their alert
  rules show at G6.

## Keeping repos on the standards

The provision step applies the standards to repos it creates; `valxel repos` keeps every managed
repo on them afterwards, including ones created by hand:

```bash
valxel repos check                    # report drift; exits 1 if any repo needs fixing
valxel repos apply                    # change what differs (safe to repeat)
valxel repos check --repo acme-clinic/cln-governance --json
```

**Managed files.** Every repo carries the platform's `.github/workflows/security.yml` (Semgrep,
gitleaks and, optionally, SonarQube) and `.github/scripts/report.py` (how CI posts test results
and coverage of new code), and governance repos `.github/workflows/secrets.yml` (gitleaks only).
The copies live in `src/valxel_sdlc/standard_files/`; a repo whose copy differs, or a template
that lacks it, gets a pull request from `apply`.

**Coverage of new code** needs no vendor. After the tests, each template's unit job runs
[diff-cover](https://github.com/Bachmann1234/diff_cover) on the coverage report it already writes
(JaCoCo, Cobertura or LCOV) against the pull request's base, and `report.py --new-code` posts the
`code-quality / new code` status: the share of changed lines a test runs, and the lines none does.
The `code-quality` suite requires 80% (products may raise it). The step is `continue-on-error`: the
status is the gate, and the unit check stays about the tests.

**SonarQube is optional**, for a customer who already uses it: it runs once the org sets the
Actions variable `SONAR_ORGANIZATION` and secret `SONAR_TOKEN`, and the `sonarqube` suite reads it
when a product requires it. A repo that needs a build first (Java) adds `.valxel/sonar-prepare.sh`,
which writes extra scanner arguments to `.valxel/sonar.args`. (SonarQube Cloud's *automatic*
analysis gets no coverage report, so its gate fails on "0% coverage on new code": turn automatic
analysis off and let the workflow scan, or don't install it.)

Managed repos are each lane's repos (from its governance manifest), each lane's governance repo
(from `registry.yaml`) and the platform's own repos (`platform_repos` in `config/repos.yaml`:
the tool, `sdlc-governance` and the templates). Governance repos get reviews and code owners
but no required checks; templates are marked as templates and require their role's checks.

| Never changed automatically | Why |
| --- | --- |
| A repo whose visibility differs from the standard (public) | Visibility changes are a person's decision |
| Missing CODEOWNERS, `dependabot.yml`, `SECURITY.md` | Files arrive through pull requests, like any change |
| A managed workflow that differs (`managed_files` in `config/repos.yaml`) | `apply` opens a pull request with the platform's copy; it's fixed when that merges |
| Secret scanning and CodeQL on a private repo without GitHub Advanced Security | They're paid features; the report says which repos need them. Until then CodeQL is not a required check and there's no code-scanning merge rule, so pull requests aren't blocked by a check that never runs |

The `repo-standards` workflow runs the check daily and fails when a repo drifts; run it by hand
with `apply` to fix. It needs the platform's GitHub App installed in every org with administration
rights (secrets `VALXEL_GITHUB_APP_ID`, `VALXEL_GITHUB_APP_PRIVATE_KEY`); the App also needs
organization custom properties (admin) to define `valxel_managed`, `valxel_product` and
`valxel_data_class`.

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
| Build | A change note per task (`.valxel/changes/<feature>/<task>.md`) on `feature/<id>`; one PR per repo, found again by branch on retries; posts `valxel/gates` pending on each PR | |
| ci_wait | Waits until every check run and status on each PR head is green (except the tool's own `valxel/` and `openchoreo/` statuses) | |
| cs_env … cs_wait | Asks each repo to run its dev-stage suites, and the acceptance repo its tests, against the PR heads | Creates `cs-<feature>` on the dev data plane, builds each PR head, binds the builds and the feature's managed services there, waits until ready |
| merge_train | Deletes the change set's environment. Posts `valxel/gates` success (naming the gates approved), then merges exactly the commit the gates saw, in dependency order (libraries, providers, consumers); branch protection still applies. An abort posts `valxel/gates` failure | Triggers a WorkflowRun on each merge commit; shared-lane repos publish packages instead |
| build_wait | Posts `openchoreo/build` on the merge commit | Waits for the WorkflowRuns |
| deploy and wait | Posts `openchoreo/dev`, `openchoreo/staging`, `openchoreo/production` | Binds the latest release to the first environment, then promotes the same release; waits until each ReleaseBinding is ready |

Waits are interrupts with `type: "external"`. They never reach the decision inbox. The engine
checks them after every step, on a timer (`VALXEL_WATCH_INTERVAL_S`), and when GitHub sends a
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
own environment (`valxel_sdlc/changeset_env.py`):

1. **Create** `cs-<feature id>` (for example `cs-farm-feat-12`) on the data plane of the product's
   first environment, labelled `valxel.io/change-set`. It is outside the deployment pipeline and is
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
  namespace: valxel                      # optional; defaults to config/tools.yaml
  components:                            # optional; defaults to the repo name
    acme-farm/farm-mobile: field-app
```

**Going live** (`VALXEL_DELIVERY=live`):
1. Grant the GitHub App, in each product org: contents, pull requests and commit statuses
   (write), checks (read). Subscribe it to check runs, check suites, statuses and pull requests,
   with the webhook URL `<VALXEL_PUBLIC_URL>/api/webhooks/github` and `VALXEL_GITHUB_WEBHOOK_SECRET`.
2. In OpenChoreo, give the confidential service client (`service_mcp_client` by default) a role
   that can trigger workflow runs, manage release bindings and create, list and delete
   environments in each product's project. Set
   `VALXEL_OPENCHOREO_MCP_URL` (`<api host>/mcp`), `VALXEL_OPENCHOREO_TOKEN_URL` and
   `VALXEL_OPENCHOREO_CLIENT_SECRET`.
3. Create each product's OpenChoreo project, components and deployment pipeline to match
   `runtime`. Try a sandbox product first: in offline mode, the change notes alone make real PRs.

The adapters match tool and argument names against OpenChoreo's published tool schemas, and
the startup check names any missing setting.

## Independent review of every pull request

Every pull request in a product's repos gets a second review from an agent that has none of its
author's context (`valxel_sdlc.independent_review`, T-144), whoever opened it: the tool's coder, a
developer, a hotfix, Renovate. The build reviewer checks a coder's change against the feature's
design; this one sees only the diff, the base branch's copy of each changed file and the product's
constitution, so a design that missed something doesn't take it along.

It's off until the tool's operator switches it on (`VALXEL_INDEPENDENT_REVIEW=on`, or
`tool.independentReview: true` in the Helm chart). Off, pull requests get no review and no
`independent-review` status, so don't make that status required in a ruleset until it's on.

- The tool's GitHub App starts it from the pull-request webhook; nothing is installed in the repo
  and the customer gives no model key. The call goes through the LLM gateway under the product's
  data-class rule, and its cost is in the product's spend (role `independent_reviewer`, Sonnet).
- The verdict is the commit status `independent-review` on the PR's head, plus one comment. It
  fails on any blocking finding, and on any major one when the change touches high-risk paths (the
  defaults, plus the product's `high_risk_paths`). Require it in the product's ruleset to make it
  a merge gate.
- Everything from the PR goes in as untrusted text; a fork's PR isn't reviewed by the agent and
  asks for a maintainer.
- The feature page shows each PR's review, and G5 shows them beside the build reviewer's.

**Is it right?** Each PR's last review is kept as an eval case (where the change is and what the
reviewer said, never the code), and what happens to the change labels it:

| Outcome | When |
|---|---|
| catch | it asked for changes and the author fixed them before merging; or it was overridden and the change was then reverted or caused an incident |
| miss | it approved, and the change was reverted (GitHub's "Reverts owner/repo#N") or its release caused an incident |
| false alarm | it asked for changes, people merged anyway, and nothing went wrong in 14 days |
| correct approval | it approved, and nothing went wrong in 14 days |

A PR closed without merging isn't counted. The watcher settles labels daily (incidents are traced to
the release they undid through the change log). `GET /api/independent-reviews/accuracy?product_id=`
gives catch and false-alarm rates for the product, overall and by risk. A change to the reviewer's
model or prompt replays these cases like any config change (`valxel eval`, the config gate): the
catch rate mustn't drop and the false-alarm rate mustn't rise. Each replay also plants one bug in a
change that was fine, which it must catch.

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
| Pull request | Code scanning (SAST) | CodeQL, plus Semgrep from the managed `security.yml` (SARIF) | GitHub code scanning | Required, blocks high and above |
| Pull request | Dependency review (SCA) | GitHub dependency review, Dependabot | Dependency diff of the PR | Required, blocks high (general may relax to critical) |
| Pull request | Secret scanning | GitHub secret scanning with push protection | Secret-scanning alerts | Required, any exposed secret blocks |
| Pull request | Infrastructure as code | Trivy or Checkov (SARIF) | GitHub code scanning | Optional, blocks high |
| Pull request | Secrets in the history | gitleaks, from the managed `security.yml` (our own secret formats too) | Check runs `gitleaks*` | Optional until every repo carries it; any finding blocks |
| Pull request | Lint | The template's lint job | Check runs `lint*` | Optional; required where a product says so |
| Pull request | Coverage of new code | diff-cover on the unit job's coverage report, posted by the managed `report.py` | Status `code-quality / new code` | Required, 80% of the changed lines; the status names the untested ones for the coder |
| Pull request | SonarQube | SonarQube's quality gate on new code, from `security.yml` | SonarQube's API (`quality/sonar.py`) | Optional; a failed gate blocks once a product requires it, and its issues go to the coder |
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

With offline delivery every suite passes with sample numbers; tests inject results through
`services.quality.sources["stub"].results`.

### After merge: findings on the default branch (scan upkeep)

The suites above judge each change. Things also turn up on `main` later: a CVE disclosed against a
package already merged, a new CodeQL rule that flags old code, a secret that slipped past push
protection. `valxel_sdlc.upkeep` handles those, by the `upkeep:` policy in `config/quality.yaml`.

1. **Intake.** GitHub's `code_scanning_alert`, `dependabot_alert` and `secret_scanning_alert`
   webhooks, plus a nightly sweep of every managed repo's open alerts (a missed webhook is caught
   within a day), go to the **findings register** (`GET /api/findings?product_id=`).
2. **Triage, by policy:**

   | Finding | Outcome |
   | --- | --- |
   | Critical; or high with an EPSS of 10% or more | **Hotfix:** a fix starts now and goes to G6 as soon as it's ready |
   | A deadline, otherwise (high 30 days, medium 90, low 180 from first seen) | **Scheduled:** the fix is built now, then waits for the next release whose target date is inside its deadline; with none, or if that release runs late, it goes to its own G6 two days before the deadline |
   | No deadline (info) | **Backlog:** recorded; fixed when a feature touches it |
   | An exposed secret | **Incident:** on-call is paged for an active or public one; rotate first, then remove it from the code |

   PHI and payments products have shorter deadlines (high 14 days), and payments hotfix from high.
   Products may tighten in `product.yaml` (`upkeep:`), never loosen.
3. **Fixes** are features on the hotfix track: the policy, which governance owns, approved the
   start, so they begin at planning with a spec and design written from the findings. One fix per
   repo collects its findings; at most `max_open_fixes` run per product. Tests, scans and the merge
   train still apply. G5 goes to the security approvers; G6 to the release approvers, as usual.
   A dependency finding first waits a day for Dependabot's own pull request.
4. **Deadlines.** A finding past its deadline, or one two fixes didn't close, opens an incident with
   no component (nothing is rolled back) that resolves when the finding closes. A security approver
   can accept the risk until a date instead (`POST /api/findings/<id>/accept` with a reason and
   up to 90 days); it comes back when that expires. A risk accepted at a feature's security review
   holds on `main` too.
5. **Dependabot's version updates** (not security updates) merge by themselves when they're patch
   or minor and every check is green: the tool posts `valxel/gates` and merges. Majors go to people.
   They reach production with the next release.

`valxel upkeep sweep|tick|list` runs the same by hand.

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
template lists in `.valxel/identity` (its workload, app and component files): when the tool creates
a repo from a template, it replaces the template's name with the component's in each of them. Events
may also carry `valxel_product`; without it the component alone finds the product.

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

- `valxel glitchtip-token PAY` prints the path, `/api/signals/glitchtip/PAY?token=…`. The token is
  derived from `VALXEL_SIGNALS_TOKEN` and opens that product's endpoint only (a webhook can't send
  headers, so it is in the URL).
- With an API token per product (`VALXEL_GLITCHTIP_TOKENS='{"PAY": "…"}'`, scope `event:read`)
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
<the product's token>`, `valxel signals-token product <id>`) at `/api/signals/openchoreo` in every
environment:

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
  (platform catalog `alarms`, built by the Compositions in `valxel-platform/platform-resources`).
  Each data plane's `AlarmRouting` sends their state changes through EventBridge to
  `/api/signals/cloudwatch`. An alarm's description says whose it is (product, feature, resource,
  environment, the components that use it, severity).

| Where the alarm is | What happens |
| --- | --- |
| Production | ALARM opens or joins an incident for the component that uses the resource; OK resolves it, so the incident's monitoring sees recovery |
| Any other environment | Kept on the alarm board; G6 shows the approvers every alarm firing before production on the feature's resources or on resources its components use |

G4 shows the alarms each AWS service comes with. Alarms are named with the data plane's prefix, so
EventBridge sends only Valxel's; other alarms in the account are ignored.

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
off, and log one `valxel.flag_cohorts {...}` line per flag a minute:

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

The tool sums the lines from OpenChoreo's logs (`VALXEL_OPENCHOREO_OBSERVER_URL`,
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
staged rollout. Offline delivery compresses bake times, as it does deployments.

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
hash of the one before it, so an edited or deleted record breaks the chain. `valxel changes verify`
(with `DATABASE_URL`) and the Changes page check it. In Postgres, grant the tool's role only
INSERT and SELECT on `valxel_changes`, so nothing it runs can rewrite history; the chain shows it if
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

`VALXEL_SERVICENOW_AUTH_<PRODUCT>` holds the Authorization header. The change_request's
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

With `VALXEL_AWS_INVENTORY_ROLE` set (a role of that name in each environment's account, allowed
`tag:GetResources`), the tool also lists everything tagged `valxel:product=<product>` there. That
fills in ARNs, and anything tagged for the product that OpenChoreo doesn't know is shown as
unmanaged, for someone to adopt or remove. Once a day the tool keeps each product's counts per
environment (`GET /api/inventory/<product>/history`) to show the trend.

### Agent context: how full it gets

Every ledger record also carries the agent session it belongs to (Claude Code's session id, Codex's
thread) and how it started, the model's context window (`context_windows` in `config/models.yaml`),
how full the context got (the largest prompt any one call in the session sent, by the vendor's
count) and an estimate of each prompt layer's size. `valxel usage context [--product P] [--weeks N]`
and `GET /api/usage/context?product_id=P` group them by week, role and model: average, 90th
percentile and largest fill, sessions over 70%, the layers that take the most, cache hits and cost.
These numbers set the caps on prompt layers and the rule for resuming sessions (the design, "Valxel:
agent context and session management"). A model with no window in config shows as unknown, and
strict mode warns about it. Codex reports usage per turn, so its fill is an upper bound.

**Caps per prompt layer.** `context_caps` in `config/models.yaml` caps the role's skills (L2,
15k tokens), the constitutions (L3, 8k), the product context (L4, 10k) and the feature context (L5,
25k). A layer over its cap is cut at a heading, keeping what comes first, and ends with a note
naming what was left out: nothing is cut silently, and the cut is the same every time, so the
prompt still caches. The size check counts 3.5 characters a token, so it errs toward cutting early.
In live mode the tool won't start if a role's model can't hold the capped layers plus `reserve`
(64k tokens for the task and the reply).

**Each build task gets its own feature context.** The planner tags each task with the criteria
(`acs`), decisions (`adrs`), files and screens it covers. A coder or reviewer then sees:
- **from the plan:** only that task's entry (the others named in one line);
- **from the HLD:** its ADRs and its repo's lines, plus lines that name no repo (threats, NFRs),
  and the Data section only for providers and event workers;
- **from the UX spec:** only its screens, and nothing when it builds none.
Each cut document says what it left out. A coder's session also gets the full documents in
`~/feature/`, outside the repo checkout, to read when the prompt isn't enough.

**Resume a session, or hand off** (`src/valxel_sdlc/sessions.py`).
- **When it resumes.** A coder whose own tests failed, or whose review asked for changes, resumes
  its Claude Code session by id, keeping everything it already read. It gets only what to fix now.
  That needs the same task, role, model and runtime; less than 24 hours since; the session under
  70% of its window; and its transcript still kept.
- **Otherwise it starts fresh,** from the handoff document the last session left: done, open,
  decisions and why, the files that matter, and what not to try again. A fallback to another model
  always starts fresh, and so do Codex sessions (for now). If a resume fails, for example because
  Claude Code can't find the transcript, the session starts fresh from the handoff instead of
  failing.
- **Transcripts** are kept gzipped beside the feature's artifacts (`<artifact dir>/<feature>/sessions/`),
  never in the customer's repos, and the watcher deletes them after 24 hours. Handoffs (artifact kind
  `HANDOFF-<task>`) and each task's session record (in the feature's state) stay.
- **The ledger** records how each session started (fresh, resumed or handoff), so `valxel usage
  context` can compare resumed sessions with fresh ones before the rule widens beyond the coder.

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
| AWS services | Cost Explorer by `valxel:product` and `valxel:feature` tags per environment account: a feature's spend is build until release and run after; untagged is the product's | build / run |
| Production compute | OpenChoreo FinOps, each production component | run |
| Incident triage | the usage ledger, incident threads | run |
| People at gates | active minutes at each gate × hourly rate (off unless set) | build |
| Shared platform | OpenChoreo, observability and this tool's monthly cost, split by products' production compute (off unless set) | run |

Rates are set in `VALXEL_COST_RATES` (JSON): `github_minute_usd` per OS (set from your plan),
`build_minute_usd`, `eas_build_usd`, `people_hour_usd`, `platform_monthly_usd`. Sources:
`VALXEL_OPENCHOREO_OBSERVER_URL` (the `finops-opencost` module), and `VALXEL_AWS_COST_EXPLORER=1` with
the `aws` extra and `ce:GetCostAndUsage` (activate `valxel:product` and `valxel:feature` as cost
allocation tags in the payer account).

**Where it shows.** Every gate packet carries the feature's build cost so far, by category. The
feature page has its cost to build and its own AWS since release. The Costs page shows a product's
month: run cost by category and environment, build cost, and each feature's build cost to date
against what G4 approved, with a FOCUS CSV download for finance and BI tools
(`GET /api/costs/{product}?month=YYYY-MM`, `/api/costs/{product}/focus.csv`,
`GET /api/features/{id}/cost`).

**Held to the plan.** G4 approves a feature's AWS services with planning costs per environment;
that is its cost plan. 7 and 30 days after release (`VALXEL_COST_REVIEW_DAYS`) the tool compares
its AWS spend with the plan as a monthly run-rate, and its production components' compute with the
week before release. Spend more than `VALXEL_COST_TOLERANCE` (default 20%) and $5/month over
becomes a cost signal in the incident flow: SEV2 for production AWS at twice the plan or more,
SEV3 otherwise. OpenChoreo budget alert rules and AWS Budgets (named
`valxel-<product id>-<environment>`) arrive as cost signals the same way.

Not counted yet: in-product agents' model spend outside this tool's gateway, and SaaS bills
(Sentry, store fees).
**Building a feature.** Every agent call is metered in the usage ledger (tokens, cache hits, cost by
phase and model) against the feature's budget, and each gate records the people's active minutes.

**Running it.** G4 approves a feature's AWS services with their planning cost per environment; that
becomes the feature's cost plan. After release the tool reviews the feature (by default 7 and 30 days
after, `VALXEL_COST_REVIEW_DAYS`):

| What | Source | Compared with |
| --- | --- | --- |
| The feature's AWS resources, per environment | AWS Cost Explorer: tag `valxel:feature` in the environment's account (`VALXEL_AWS_COST_EXPLORER=1`, the `aws` extra, `ce:GetCostAndUsage`) | The G4 plan, as a monthly run-rate |
| Its components' compute in production | OpenChoreo FinOps (`finops-opencost` module) through the Observer cost API (`VALXEL_OPENCHOREO_OBSERVER_URL`) | The same components the week before release |

Spend more than `VALXEL_COST_TOLERANCE` (default 20%) and $5/month above the comparison becomes a
cost signal in the incident flow: SEV2 for production AWS at twice the plan or more, SEV3 otherwise.
Budgets are the second line: OpenChoreo budget alert rules per component, and AWS Budgets per
product account named `valxel-<product id>-<environment>`, arrive as cost signals the same way.

`valxel:feature` and `valxel:product` must be activated as cost allocation tags in the payer
account; Cost Explorer data lags by about a day. Each review is stored, and served at
`GET /api/features/{id}/cost` (build cost, plan, reviews) and `GET /api/costs/{product}` (each
feature's build cost and run-rate, with totals). The feature page shows the plan against the latest
review. Not covered yet: CI minutes, build and test environment costs before release, cluster
compute and shared platform costs allocated to products.


### A feature's budget, phase by phase

A feature may spend `budget_usd` from `product.yaml` (at most the platform's $500). That money is
split among groups of phases, so a single phase can't spend the whole budget
(`src/valxel_sdlc/budgets.py`). The supervisor checks a phase's share before the phase runs. When
the share is spent, the phase escalates (VX-10), and the escalation names the share. Shares add up
as the feature goes, so whatever a phase doesn't spend passes to the phases after it. When a person
adds budget at the escalation, the stuck phase gets all of it, and later phases keep their shares.

| Group | Phases | Share |
| --- | --- | --- |
| documents | discovery, spec, design, architecture | 15% |
| plan | plan | 5% |
| build | the first build, with its reviews and fixes | 55% |
| fixes | builds that run again after CI failed | 20% |
| release | release, operate | 5% |

A product can set its own shares in `product.yaml`, for example
`budget_shares: {documents: 25, build: 45}`. Any group it leaves out keeps the default. Every
value must be zero or more, and the shares must add up to 100. These are starting figures, to be
reset from the ledger once 10 pilot features have run.

### Is boilerplate worth a code generator?

Slice generators in the templates (one command writes a controller, its DTOs, repository and
tests) would save the coder's output tokens, but only where boilerplate is a real share of what
it writes. `valxel scaffolding <product>` measures that from real features. For each repo role
it reads the coder's pull requests from GitHub and its calls from the usage ledger. It reports
the lines the coder added in new files shaped like scaffolding, other new lines and changed
lines, beside the coder's output tokens. The scaffold shapes per role are in
`src/valxel_sdlc/scaffolding.py`.

A role is worth a generator once it has at least 10 coder tasks and scaffolding is at least 30%
of the lines added. Lines stand in for tokens, so "tokens a generator would save" is an
estimate. Build generators only for roles the report marks.

## Cloud services (VX-19)

Features that need a managed service get it from the platform's catalog (`platform/catalog.yaml`
in sdlc-governance), by neutral type: `key-value-table`, `event-stream`, `queue`, `object-bucket`,
`topic`, `postgres` and `cache`. Each type names the service behind it on each cloud (ADR 0010):
on AWS DynamoDB, Kinesis, SQS, S3, SNS, and RDS PostgreSQL and ElastiCache Valkey through
OpenChoreo's Crossplane provisioner; on Google Cloud Firestore, Pub/Sub, Cloud Storage and Cloud
SQL for PostgreSQL (Memorystore to follow; Cloud SQL has no `paused` size). The architect sees only the installation's cloud's services
and prices. The version 1 names (`dynamodb-table`, `sqs-queue`...) still read as aliases, and a
resource made under one keeps it. Each type is an OpenChoreo ClusterResourceType backed by a
Crossplane Composition per cloud in `valxel-platform/platform-resources`, with encryption, private
access, backups and tags (labels on Google Cloud) built in. People approve; agents do the rest:

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

A product names its cloud in `runtime.cloud` in its `product.yaml` (ADR 0010): `provider: aws`,
a region allowed for its data class and one account per environment. It must be the cloud the
installation runs on (`VALXEL_CLOUD`). The older `runtime.aws` (region and accounts) still reads as
`provider: aws`. The pipeline never deletes a resource; changing a key or a
queue's ordering replaces stored data, so the design check refuses it and a person does it.

## Store apps (VX-18)

A mobile app (role `mobile`) is released, not deployed: people install a build and keep it for
months, and every build goes through the stores. The pipeline is the same up to the merge; then,
instead of OpenChoreo, each stage asks the app's repo to release the merge commit
(`valxel-release`, `release.yml` from `template-mobile`) and waits for the status it posts:

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
(role `acceptance`, one per product, from `valxel-platform/template-acceptance`: Playwright Test in
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
  extending or replacing a test that doesn't exist is rejected. `valxel acceptance review --product CLN` lists tests already covered by
  another, for merging; the repo's nightly full run does the same and reports slow tests.
- **Runs after the whole change set is deployed.** In dev, then staging, the pipeline asks the
  acceptance repo (`repository_dispatch: valxel-acceptance`) to run the merged tests with every
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
the feature's capability to the `valxel.io/capabilities` annotation. It keeps everything else: a
description, links, `dependsOn`, an `API` entity, or a type, owner or lifecycle someone changed.
The tool's own catalog (Postgres) still holds capabilities while they are designed and in flight,
which Backstage doesn't model (`valxel_sdlc/catalog_info.py`).

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

**Product architecture diagrams.** In inception, the architect draws three Mermaid diagrams:
a context diagram (people, outside services, every component and resource), a deployment
diagram (OpenChoreo's shared control plane and the product's non-production and production data
planes, with each running component in both), and a sequence for each Load journey with its
failure path. The checks hold each one to the text. The two flowcharts are published for the
product gate with the product's diagram tool, or for a new product with
`inception.diagrams` in registry.yaml. Before setup a new product has no repo, so draw.io links
open the editor, and setup commits the `.drawio` files to `architecture/diagrams/` in the
governance repo.

**Diagrams on the page.** Gate pages and documents draw every `mermaid` block (the HLD's
diagram and flows, and the product architecture's diagrams) as a picture, with its source
underneath. Mermaid runs in strict mode, so the agent-written source can't run scripts.

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
stand-in, and `/api/products` reports it as offline (`mode: stub`). Unattended agents can't complete an OAuth
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
rules (`valxel_sdlc/performance.py`):

| Level | When | What runs |
| --- | --- | --- |
| none | only docs, tests or comments change; libraries and the acceptance repo | nothing |
| smoke | a hot path, a database migration, a runtime or base-image upgrade, timeouts, pools or runtime settings, or a large change | `perf-smoke` on the PR: the image built from main and the PR's, loaded the same way on the same runner; fails past `smoke_regression_pct` or the budget |
| full | an API contract change, a new or changed event handler, or an HLD whose **Performance** section says the feature adds traffic | the smoke test, then `performance` in staging: the expected peak times `staging_load_factor`, judged against the budget and the last release's run |

Only APIs (`provider`) and web apps (`consumer`) have a staging load test. A BFF, MCP server, agent, event worker or mobile app that would need one stops at the smoke test (a session, delegated tokens and model calls, Kafka credentials or a device farm would be needed); their production signals and the rollout's comparison watch them.

The HLD's Performance section (expected load, new traffic, data growth, journeys) feeds the rules;
data growth flags the nightly soak test. The reviewer agent lists performance risks it sees (an
N+1 query, a call inside a loop, an unbounded read): any raises the level to at least smoke. Agents
only raise a level. The tool labels the PR (`valxel:perf-smoke`, `valxel:perf-full`), which runs
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

## Workflow profiles: feature and express

Not every change needs discovery and a UX design. Whoever starts the work picks a profile (the New
intent page asks when the product allows more than one); the gates that matter stay whatever the
profile (`valxel_sdlc/profiles.py`):

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
the coder's reply carries the same notes for each pull request (`valxel_sdlc/diary.py`):

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
(`valxel_sdlc/traceability.py`). `GET /api/features/{id}/trace.csv` exports one row per test case.

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

The knowledge agent turns the correction into one instruction (in offline mode a fixed rule does) and
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

# Running Valxel

For whoever installs and runs the tool: sign-in and who can decide, workspaces and connections, tenant isolation, the audit trail, the security model and supply chain, models, the modes and replacing the stubs. Installing on a cluster or a VM: see the install guide and ADR 0007.

Back to the [README](../README.md).

## Contents

- [Web interface](#web-interface)
- [Tracing and the audit trail (VX-8, VX-15)](#tracing-and-the-audit-trail-vx-8-vx-15)
- [Security model (VX-17)](#security-model-vx-17)
- [Supply chain: what goes into a build, and what may run](#supply-chain-what-goes-into-a-build-and-what-may-run)
- [Models: more than one vendor](#models-more-than-one-vendor)
- [Modes](#modes)
- [Replacing the stubs](#replacing-the-stubs)
- [Where customer code or secrets may be kept](code-and-secrets.md)
- [Self-hosted: install and upgrade](self-hosted.md), [releases and support](releases.md), [security packet](security-packet.md)

## Web interface

**Look.** The interface wears the Valxel logo: the hexagon mark and VALXEL wordmark
(`web/src/components/Mark.tsx`, drawn as SVG; `web/public/favicon.svg`), the logo's navy for the
top bar, and its cyan-to-blue only in the mark and the page you're on. The sign-in page shows the
logo itself (`web/src/brand/valxel-lockup.png`, cut from `docs/brand/valxel-agentic-sdlc.jpg`).
Four colours carry status and nothing else: red stopped, amber waiting on a person, green passed,
blue agents working. Neutrals are cool slate, and the dark theme follows the logo's navy. Every
text colour meets WCAG AA (4.5:1) on the backgrounds it sits on, in both themes.

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
  product already has (see [Duplicate requests](product-teams.md#duplicate-requests)). You can edit the draft
  directly and submit it when it is complete. Submitting starts discovery.

  When the intent changes what people see (the product has a web or mobile app and the request
  talks about screens, alerts, forms and the like), the agent also asks about the experience, one
  question at a time: which screen or flow, the one thing a person must do or see first, where they
  use it, and the look (the product's current design unless they want something else). It offers
  choices to tap, and "you decide" leaves a part, or the rest, to the design agent, which the brief
  says. The answers become an experience brief in the intent: G1 approvers see it, the design agent
  follows it and lists each choice it made itself, and the G3 critic checks the design against it.
  People can turn the round off ("Changes what people see") or fill the brief in directly.

  Files can be attached (screenshots, PDFs, text): see `valxel_sdlc/attachments.py`. Until the intent
  is submitted they count as the strictest data class, and the intake agent sees only their names.

### Sign-in and who can decide

GitHub sign-in uses an OAuth app owned by `valxel-platform`:

1. In the org's settings, open Developer settings, OAuth Apps, New OAuth App. Set the callback
   URL to `<VALXEL_PUBLIC_URL>/api/auth/callback`.
2. Set `VALXEL_AUTH=github`, `VALXEL_GITHUB_OAUTH_CLIENT_ID`, `VALXEL_GITHUB_OAUTH_CLIENT_SECRET`,
   `VALXEL_PUBLIC_URL` and a random `VALXEL_SESSION_SECRET` of at least 32 characters.
3. Each product org approves the OAuth app (org settings, Third-party access) so team membership
   is visible.

The app requests `read:org` to read the user's orgs and teams, and `user:email` for their primary
email if GitHub has verified it. It keeps them in a signed, HttpOnly session cookie and discards
the GitHub token.

**Google, email and passkeys** come through an Amazon Cognito user pool (`src/valxel_sdlc/web/identity.py`).
GitHub stays on its own OAuth app, since GitHub isn't a standard OpenID provider.

1. Deploy `deploy/cognito.yaml`. It creates the user pool and managed login on the Essentials plan
   (email codes, passkeys, and Google when you give it a Google OAuth client), the app client, and
   its secret in Secrets Manager. Give it an SES identity: Cognito's own sender allows 50 emails a day.
2. Set its outputs as `VALXEL_COGNITO_DOMAIN`, `VALXEL_COGNITO_CLIENT_ID` and `VALXEL_COGNITO_ISSUER`,
   and the secret as `VALXEL_COGNITO_CLIENT_SECRET`. Set `VALXEL_COGNITO_GOOGLE=false` without Google.
3. Choose `VALXEL_AUTH=github` (GitHub, plus Cognito when it's set) or `VALXEL_AUTH=cognito` (Cognito
   only, plus GitHub when its OAuth app is set).

**People's assistants over MCP.** With `VALXEL_MCP_CLIENT_IDS` set to the stack's `McpClientId`
output, the tool serves MCP at `<VALXEL_PUBLIC_URL>/mcp` (`src/valxel_sdlc/api/mcp.py`). An assistant
signs its person in through the same user pool (the client id is entered in the assistant), then
reads their inbox, features, documents and traces, starts intents, and decides gates, as them:
each tool calls the API in-process with that person's rights. Only a person's token gets in: one
issued to the MCP client, with a username and the openid scope. A client-credentials token is
refused for everything. A decision records the person, with `via: mcp`. An access token carries no
proof of a passkey, so approving through a workspace group is done in the web app; batch approval
isn't offered.

**Other agents over A2A.** With `VALXEL_A2A_CLIENT_IDS` set (Cognito app clients like the MCP one,
for agents that act for a person), the intake and incident agents are published in an A2A v1.0 card
at `/.well-known/agent-card.json` and answer JSON-RPC at `/a2a` (`src/valxel_sdlc/api/a2a.py`): the
`intent` skill is the intake conversation (submit with a data part `{"submit": true}`), and
`report-incident` opens or joins an incident. The same rule applies as for MCP: only a person's
token, and the API's own checks on what they may do.

How it works:
- **Flow and checks.** Sign-in is OIDC's code flow with PKCE, a state and a nonce. The ID token's
  signature is checked against the pool's published keys, along with its issuer, audience, expiry
  and use.
- **Who someone is.** Only a verified email signs anyone in, and that email is who they are. An
  invitation, a group or an approval finds them by it, so someone who signs in with Google one day
  and GitHub (same verified email) the next is the same member.
- **Passkeys for admins and group approvers.** A passkey (`amr` swk or hwk, `acr` level 3) counts as
  two factors; an email code or Google doesn't. So a workspace's admins, and anyone approving
  through a workspace group, act only from a passkey session; GitHub sign-in counts too, since
  GitHub requires two-factor sign-in of code contributors. They add a passkey from their workspace
  page (Cognito's `/passkeys/add`), and the decision page offers "Sign in with your passkey". A
  GitHub team in `product.yaml` approves as before.
- **Session lengths.** Sessions last 12 hours. Anyone who is an approver, admin or owner in a
  workspace signs in again after 4 hours.

- **Submitting an intent** requires membership of the product's GitHub org.
- **Deciding a gate** requires matching one of the gate's approvers in that product's
  `<product>-governance/product.yaml`. An approver is a team (`acme-clinic/product-owners`) or a login.
  Cross-lane gates (G4, G5, G6) ask each touched lane in turn, using that lane's approvers.
- **Resolving an escalation** uses the approvers of the gate that follows the stuck phase.

The API records the signed-in login as the approver; a request body cannot override it.

#### Workspaces, members and approver groups

A workspace is one customer: the `workspace` each product has in `config/registry.yaml`. People
who aren't in a product's GitHub teams join through it (`src/valxel_sdlc/workspaces.py`):

| Role | Can |
|---|---|
| owner | everything an admin can, and make or change owners. A workspace always keeps one. |
| admin | invite people, set their role and products, suspend or remove them, edit groups and gate mappings, read the audit log |
| approver | decide gates their groups are mapped to, and start features |
| member | start features and follow them |
| viewer | see the workspace's products, nothing more |

- **Invitations.** An admin adds a profile (name, email or GitHub login, role, and optionally the
  products it covers) on the workspace's People page. The person gets an email with a one-time
  link when email is set up (`VALXEL_SMTP_HOST` and friends; Amazon SES works through its SMTP
  endpoint, `email-smtp.<region>.amazonaws.com`). Otherwise the page shows the link for the admin
  to share. Only the invited person can accept it: they sign in as that GitHub login, or with that
  email, which GitHub sign-in reads only when GitHub has verified it. Sign-in returns them to the
  link.
- **Pages.** Workspace (beside your name) has four tabs:
  - **People:** roles, products, suspend, remove, a new link.
  - **Approver groups:** who is in each group.
  - **Gate approvers:** a grid of gates by groups per product, saved as a new version.
  - **Audit log:** shown to admins.
  Every action is checked again by the server.
- **Approver groups.** Each workspace starts with Product, Design, Architecture, Security, Release
  and On-call; the owner is in all of them. An admin maps groups to each product's gates
  (`PUT /api/workspaces/{ws}/products/{id}/gates`). Mapped groups *add to* `product.yaml`'s
  approvers as `group:<workspace>/<group>`; the teams there still decide.
- **A feature keeps its mapping.** Each mapping change is a new version, and a feature pins the
  version it started on, like its other inputs. Changing approvers mid-flight doesn't move a gate
  to different people.
- **Checked on every request.** Roles and groups are read from the database on each request, not
  from the cookie, so suspending or removing someone takes effect at once.
- **Audited.** Every change to members, groups and mappings is in `GET /api/workspaces/{ws}/audit`.

#### Connections

A workspace's **Connections** tab lists every tool, connected or not: GitHub, GitHub repo creation,
AWS, Jira, Lucid, Sentry and PagerDuty (`src/valxel_sdlc/connections.py`). Each row shows what
Valxel can reach, who connected it and when it last worked. Only admins connect and disconnect,
and every change is in the audit log.

**GitHub** connects by installing Valxel's two GitHub Apps from that tab, one after the other:
Delivery (code, pull requests, checks, alerts) and Provisioning (creating repos, used only by the
provisioning worker). An install can be on an org or on a person's own account, and a workspace
can have several. Valxel stores no GitHub secret for it: a connection is the installation's
record. To turn installs on, in each App's settings on GitHub:

1. Set the **Callback URL** to `<VALXEL_PUBLIC_URL>/api/connections/github/callback`, and tick
   **Request user authorization (OAuth) during installation** and **Redirect on update**.
2. Give Valxel the App's slug and OAuth client: `VALXEL_GITHUB_APP_SLUG`, `VALXEL_GITHUB_APP_CLIENT_ID`
   and `VALXEL_GITHUB_APP_CLIENT_SECRET`, and the same with `VALXEL_GITHUB_PROVISIONER_APP_` for
   Provisioning.

How it behaves:
- **Each install is checked as the installer's own.** GitHub returns a code with the install, and
  Valxel exchanges it for that person's short-lived token. The installation must be one GitHub
  says they can see, and of the right App, or it isn't recorded. Without this, anyone could attach
  another customer's installation id to their own workspace. The token is used for those two reads
  and dropped.
- **Health.** Every hour, the Delivery App reads its installation. An uninstalled or suspended App
  turns the row to *Needs attention* and emails the workspace's admins once, with a Reconnect
  button on the page.
- **AWS** connects at go-live, with a CloudFormation stack the customer creates in their own account
  (`deploy/customer-stack.yaml`). In the AWS row the admin picks the mode and region, and **Open AWS
  to create the stack** opens the console's quick-create page with the stack's parameters filled in:
  Valxel's account, the workspace, the mode and the workspace's external ID. After creating it,
  they enter their account ID and Valxel assumes both roles to check them.

  The stack makes one role per job, each trusting only Valxel's account (`VALXEL_AWS_ACCOUNT_ID`),
  only with that external ID, and acting only on resources tagged `valxel:workspace`:
  - **Deploy, lite mode:** the products' VMs, reached over SSH with one-minute EC2 Instance Connect
    keys.
  - **Deploy, full mode:** EKS clusters, passing only the roles the stack makes. Crossplane's own
    Pod Identity role builds the catalog services.
  - **Agents:** AWS's `ViewOnlyAccess` (metadata, never data), tags and costs.

  Valxel stores no AWS secret; sessions last 15 minutes. The hourly health check assumes the
  roles again, so a deleted stack shows within the hour. Host the template where the console can
  read it and set `VALXEL_AWS_STACK_TEMPLATE_URL`.
- **Jira, Lucid, Sentry and PagerDuty** connect through the vendor's own consent screen
  (`src/valxel_sdlc/oauth_connections.py`). Whoever runs Valxel registers one OAuth client with
  each vendor, with the redirect URL `<VALXEL_PUBLIC_URL>/api/connections/oauth/callback`. For
  Sentry, register a public integration whose redirect URL is
  `<VALXEL_PUBLIC_URL>/api/connections/sentry/setup` instead. Then give Valxel each client's
  settings:
  - `VALXEL_JIRA_OAUTH_CLIENT_ID` and `VALXEL_JIRA_OAUTH_CLIENT_SECRET` (an Atlassian OAuth 2.0
    integration with Jira's `read:jira-work`, `write:jira-work` and `read:jira-user` scopes);
  - the same with `LUCID`, and with `PAGERDUTY` (a scoped OAuth app: incidents read and write,
    services read, users read; it uses PKCE);
  - `SENTRY`'s, plus `VALXEL_SENTRY_OAUTH_APP_SLUG`.

  A tool whose client isn't set shows as not set up. When an admin connects one, Valxel asks the
  vendor what the token reaches and records it (the Jira site, the Lucid user, the Sentry org, the
  PagerDuty account). The tokens go to the secret store, never the database, at
  `valxel/workspaces/<workspace>/<tool>/<account>`: AWS Secrets Manager, or files under
  `VALXEL_SECRETS_DIR`. With `VALXEL_SECRET_STORE=env`, as on a laptop, they stay in memory until
  Valxel restarts.

  Valxel renews a token in the five minutes before it expires. Atlassian's refresh tokens work only
  once, so a renewal holds a lock and reads the store again first, and if the vendor refuses a
  refresh token another process has already spent, Valxel goes on with the newest one. The hourly
  health check renews tokens when due and makes the same read as at connect time. A revoked
  connection turns to *Needs attention*. Disconnecting deletes the tokens; the vendor's side is the
  customer's to revoke.
- **Features wait.** A self-serve workspace's feature (any made from the Workspaces page, except
  self-hosted ones) waits before architecture and build until GitHub reaches its product's org and
  repos ("Waiting for: connect GitHub (farm-field-api isn't in the GitHub App's repos)"). It carries
  on by itself once connected. Workspaces set up by hand, with the Apps installed in their orgs,
  never wait.

An existing deployment's workspaces have no owner yet. Give each one from the command line:

```bash
DATABASE_URL=... valxel workspaces owner wrkspc-shared --github asha
DATABASE_URL=... valxel workspaces list
```

#### Sign-up and plans

Someone signed in with no workspace and no products lands on **Set up Valxel** (`/start`,
`web/src/pages/Start.tsx`). It takes about 10 minutes, in four steps:

1. **Plan.** The plans table comes from `src/valxel_sdlc/billing.py`:
   - *Self-hosted:* $0 for organizations under 250 people and $10M revenue. One checkbox
     confirms they qualify, and the wizard links to the install guide (`VALXEL_INSTALL_GUIDE_URL`).
     No hosted workspace is made.
   - *Lite:* hosted dev and staging free for 90 days.
   - *Full:* a monthly price per product.
2. **Workspace.** The founder names it and becomes its owner, in every approver group.
3. **GitHub.** The founder installs the Delivery App, then the repo-creation App, or skips the
   second and creates repos by hand. Each install comes back to the wizard. Someone without a
   GitHub organization gets a guided step first: make a free personal account, then a free
   organization, then come back.
4. **First product.** The founder describes the idea in the intake conversation. Its inception
   runs as any other, but its gates go to the workspace's own Product group (`group:<workspace>/product`),
   which includes the founder, rather than the platform's product council. The registry line it
   proposes puts the product in that workspace. Only admins of a self-serve workspace start
   products this way. Elsewhere it stays the council's starters.

Owners move a workspace between Lite and Full (`PUT /api/workspaces/<id>/plan`). Its billing
(`GET /api/workspaces/<id>/billing`) shows the plan, the trial's days left, and when a card will be
asked for: at go-live for both plans, or when extending Lite's hosting.

**The payment provider and the Lite and Full prices are still to be chosen.** Until then nothing
is charged and nothing waits for a card, and the billing view says so. To show prices, set
`VALXEL_PRICE_LITE_USD` and `VALXEL_PRICE_FULL_USD`. A provider will implement `BillingProvider`
in `billing.py` and be chosen with `VALXEL_BILLING`. Once one is set, connecting AWS for go-live
waits until the workspace has a card on file.

Anyone signed in can create a new workspace (`POST /api/workspaces`) and becomes its owner.
Self-hosted deployments that use only GitHub teams work exactly as before: with no members,
nothing changes.

Run as many replicas as you like behind one load balancer, all on the same Postgres
(`src/valxel_sdlc/cluster.py`). Nothing routes a person back to the same replica:

- **One run per feature.** The replica that moves a feature, incident, release or initiative on
  holds its Postgres advisory lock while it runs. Another replica asked to move it on at the same
  time answers that it is still running, and every replica shows it as running. A replica that
  dies lets go with its connection; one that loses its lock connection stops before the next step.
- **Picked up after a crash.** A replica that dies mid-step (a crash, an out-of-memory kill, a
  deploy) leaves the thread at its last checkpoint, and work it had queued but not started is
  visible to the others. The watcher resumes such threads from their checkpoint within a minute
  (`engine_parts/recovery.py`), the step that was cut off running again from its start. A thread
  restarted 3 times from the same step (`VALXEL_RUN_RECOVERY_LIMIT`) is stopped with an error
  instead. A thread that stopped on an error stays stopped until someone presses Resume on its
  page (`POST /api/features/{id}/resume`); resuming can't skip a gate. Queued work counts as
  dropped after `VALXEL_RUN_QUEUE_STALE_S` (900 s).
- **One watcher.** Every replica starts the watcher; only the one holding its lock polls CI and
  OpenChoreo and runs the daily inventory and hourly cost review. If it goes, another takes over on
  its next tick.
- **The watcher's jobs tell you when they stop working** (`src/valxel_sdlc/watcher_jobs.py`). Each
  housekeeping job (inventories, outcomes, upkeep, costs, receipts, connections, transcripts, change-set
  environments, crash recovery) logs its failures with their traceback, counts them per job in the
  shared notes (`watcher-job:<name>`: last success and failure, failures in a row and in all, the
  error's type), and a failed daily job is retried hourly. A job without a success for 26 hours logs
  `watcher job <name> stale` at error level and posts to `VALXEL_NOTIFY_WEBHOOK`, then again daily
  while it stays stale, and once more when it recovers. `GET /api/healthz/watcher` lists stale jobs
  for monitoring (not for probes: it reads the shared notes).
- **Asked once.** Post-deploy, acceptance and rollout runs the tool asks GitHub for are claimed in
  a table first, so two replicas never both ask.
- **Shared.** Live updates go through Postgres NOTIFY to every replica's browsers; intake chats,
  a thread's last error and its tracker sync error are in tables.

Without a database (development) everything is in memory: run one process.

### Tenant isolation in the database

Every workspace (customer) shares the tool's database, so a query that forgot to filter by product
would show one customer another's work. Postgres row-level security makes the database refuse it
(`src/valxel_sdlc/tenancy.py`). Set `VALXEL_TENANCY=rls`; strict mode requires it.

- **Each connection states whose rows it may see,** set from the work it does when it leaves the
  pool: a request, the workspaces of every product the signed-in person may see; a feature's run,
  its own workspace, whoever started it; the watcher, daily jobs, the CLI and startup, the platform
  (all). A connection that states nothing sees nothing.
- **Each table has a policy:** by product (usage, costs, changes, signals, findings, eval cases and
  the rest), by thread (LangGraph's checkpoints, notes, work items: the feature index records each
  thread's workspace), by workspace column, by owner (intake chats, which have no product yet) or
  platform only (signals that matched no product). The claims and provisioning queue hold keys and
  plumbing, not customer content, and stay open.
- **The tool's login must not bypass it.** Postgres never applies row-level security to a superuser
  or a role with `BYPASSRLS`, so connect as an ordinary role that owns the tables. The policies are
  forced, so the owner is held to them too. With `VALXEL_TENANCY=rls`, startup checks the login and
  strict mode refuses one that bypasses it.
- **Files are held to the same scope.** A thread's documents, session transcripts and attachments
  are kept in its workspace's folder, `VALXEL_ARTIFACT_DIR/<workspace>/<thread>/` (the platform's own
  threads in `_platform/`). With `VALXEL_TENANCY=rls`, every read and write checks the current scope
  as the policies do: a request or run touches only its own workspaces' folders, the platform all of
  them, and no scope none; a stored reference that points outside its thread's folder is refused.
  Shared storage (the tools cluster's EFS, which every API and worker pod mounts) holds every
  workspace's files, so this check is what keeps them apart.
- **What it doesn't cover:** separate deployments per regulated customer ("cells") are a separate
  decision.

## Tracing and the audit trail (VX-8, VX-15)

The templates give every product the same instrumentation:

| | How | Where |
| --- | --- | --- |
| Traces | OpenTelemetry, exported over OTLP to the data plane's collector; incoming `traceparent` is honored and passed on, so one trace runs from the browser through services, agents, MCP servers and the LLM gateway. Agent spans follow the GenAI conventions and never carry prompt or result text. | every template |
| Logs | JSON with `trace.id` and `span.id` | every template |
| Audit | Who did what to which records, as CloudEvents (`valxel.audit.v1`) on the product's Kafka audit topic. A request whose record Kafka doesn't acknowledge fails. | service, agent, MCP server, LLM gateway |
| Audit store | `ss-audit-sink` copies the topic into an append-only, hash-chained Postgres table (one consumer group) and archives it to object storage under Object Lock (another), so neither destination holds up the other; `audit-sink verify` checks the chain and `restore` rebuilds Postgres from the archive. | each product's runtime |

The pipeline holds designs and builds to it. In PHI and payments products an HLD that changes a
repo users reach must have an `## Audit` section naming its audited actions
(`- consent.read: consent`); coders are told to publish those records, reviewers to check them,
and the build fails its check if an action the HLD names appears in no task.

## Security model (VX-17)

Every request carries two checked identities: the calling workload, proven by the mesh (Istio
ambient mTLS), and the person or service it acts for, as a token from the product's Keycloak realm
issued for the product. `valxel-platform/platform-security` generates each product's realm and mesh
policies from one spec.

| Component | Template | What it enforces |
| --- | --- | --- |
| BFF (`bff`) | `template-bff` (Spring Cloud Gateway) | The only internet-facing component for a web app: OIDC sign-in with PKCE, tokens kept in the Redis session, `__Host-SESSION` cookie, `/api/**` relayed with the person's token, CSRF and Origin checks |
| Web app (`consumer`) | `template-web` | No tokens in the browser; same origin through the BFF; project-visible only; security headers on every page |
| Service (`provider`) | `template-service` | Issuer, keys, expiry and the product audience; `@PreAuthorize` on every endpoint (the build fails otherwise); record-level `AccessPolicy`; forward inside the product, token exchange across trust boundaries |
| Agent (`agent`) | `template-agent` | Token exchange per MCP server for the person behind each step (the approver for approved changes); no fixed tokens; people's tokens never stored. Deployed as the platform's `agent` component type: no Kubernetes token in the pod, read-only file system, non-root with no capabilities, network only to the LLM gateway, MCP servers, database and event brokers, and gVisor for PHI and payments products (`isolation` in config/repos.yaml) |
| MCP server (`mcp-server`) | `template-mcp-server` | Audience and a scope per tool; audit on behalf of the person |
| Mobile app (`mobile`) | `template-mobile` (React Native, Expo) | A public client (PKCE in the system browser), tokens in the Keychain or Keystore, calls only through the BFF's `/mobile/api` route (bearer token, audience and minimum version checked there); PHI builds encrypt local data and block screenshots (VX-18) |

Designs declare exposure in an `## Endpoints` section, one line per component:
`- <org>/<repo>: <project|namespace|internal|external|none>[; calls <component>, ...]`. Only the BFF
may be `external` (anything else needs an ADR that says so), a web app never is, and every call
must name a component of the design or the product. A mobile app is `none` and calls only the
product's BFF. G4 approves it; the components' `workload.yaml`
and the product's mesh policies follow it. Coders and reviewers get their role's rules. A web app's
post-deploy tests run against the BFF's public URL.

## Supply chain: what goes into a build, and what may run

**Agent sessions never hold the tool's credentials** (`valxel_sdlc/sandbox.py`). Every session runs
with an allowlisted environment (its model's key, locale, telemetry; every other variable the tool
has is blanked, so the GitHub App key, database URL and webhook secrets aren't there), with every
hook off (`disableAllHooks`, so a repository's `.claude/settings.json` runs nothing), and with Read
denied on the tool's own files and secrets. Shell commands run in Claude Code's sandbox, reaching
only the package registries and GitHub (`config/egress.yaml`); Codex runs in its workspace-write
sandbox. npm, yarn and pnpm skip packages' install scripts. A coder works in its own checkout of its
one repository, cloned with a token the App mints for that repository alone with `contents: write`:
it can push its branch, and can't post the `valxel/gates` status, change settings or reach another
repository. Its folder is deleted when the session ends. `VALXEL_AGENT_ENV_PASS` names any other
variable a deployment's sessions need (e.g. `AWS_REGION` for Bedrock); `VALXEL_AGENT_SANDBOX_WEAKER=1`
uses Claude Code's weaker sandbox in containers that can't create user namespaces.

**In production, the API queues work and workers run it** (`valxel_sdlc/run_queue.py`,
`VALXEL_RUN_QUEUE=postgres`; strict mode requires it in live mode). Starting a feature, a gate
decision, a resume or a wait that ended is a row in `valxel_run_queue`; the API answers at once and
the page shows the thread running. `valxel worker` processes (`--concurrency`, default 4; `/healthz` on
`VALXEL_WORKER_PORT`, 8082) claim rows with `for update skip locked`, never a thread's later row
before its earlier one, and run each under the thread's advisory lock as before; the watcher runs in
them too, and API processes set `VALXEL_PROCESS=api` to run none. A worker renews its claims while it
runs; one that dies stops, and its rows are claimed again when the claim runs out
(`VALXEL_RUN_QUEUE_LEASE_S`, 120), at most `VALXEL_RUN_QUEUE_MAX_ATTEMPTS` (3) times. On SIGTERM a
worker stops claiming, gives runs under way `VALXEL_WORKER_DRAIN_S` (25) to finish and hands the rest
back, so a deploy doesn't count against them. A new feature's first state is its first checkpoint, so
its page shows it while it waits. Rows carry their workspace, under row-level security.

**Every step is safe to run twice.** A graph step is checkpointed when it ends, so a worker that
dies mid-step leaves the thread at the step's start, and the next run repeats the whole step. What a
step does elsewhere must therefore converge, or be asked once (`tests/test_repeat_safety.py`):

| What a step does | Run twice |
| --- | --- |
| Files, PRs, merges, repos, rulesets, branch protection (GitHub) | Converge: an unchanged file isn't committed, an open PR for the branch is reused, a merged PR isn't merged again |
| Deploy and promote (OpenChoreo) | Converge on "this environment runs this release" |
| Flags (Flipt) | Set to a value (on, off, a percentage), never toggled; flags are created with `ensure` |
| Workflows the tool starts (post-deploy, acceptance, screens, rollout checks, store releases) | Asked once per wait or attempt: a claim in `valxel_claims`, given back if the request fails |
| Production change log | One record per key, keyed by what happened (`<feature>:g6:<n>`, a rollout step, the reading that found a regression) |
| ServiceNow receipts | Found by correlation id before one is created; receipts lost with a stopped process are sent by the watcher's hourly catch-up |
| Pages (PagerDuty) | Deduplicated by key, and remembered as sent |
| Work tracker items (Jira, OpenProject) | Found by the feature's label or marker before one is created. A comment posted just before a crash may appear twice |
| Email notices | Remembered as sent; one sent just before a crash may go twice |

**In production, each session runs in a sandbox of its own** (`valxel_sdlc/session_runner.py`,
`VALXEL_SESSION_EXECUTOR=kubernetes`; strict mode requires it in live mode). The tool claims a fresh
sandbox from the platform's warm pool (Agent Sandbox 1.0: a `SandboxClaim` on the `agent-session`
warm pool in `VALXEL_SESSION_NAMESPACE`, `VALXEL_SESSION_POOL`; it deletes itself at its shutdown time
should the tool not get to it), posts the session to the runner there (`valxel
session-runner`, the tool's own image) and deletes the claim when it answers. Only the session
crosses: its prompt and options, the feature's documents it reads, the repository and branch with a
token for that one repository (a coder) or read-only tokens for the repositories it reads (the
architect), its model credentials and telemetry settings, and an earlier transcript to resume. The
runner clones there, runs Claude Code, and returns the reply, usage, session id, skills loaded and
the transcript to keep; the coder pushes its branch from the sandbox. The tool's App key, database,
webhook secrets and other workspaces' files never reach it, and the tool no longer clones customer
code into its own container. The template, owned by the platform
(`platform-foundation/gitops/apps/agent-sessions`), runs it under gVisor with no service account
token, a read-only root, resource limits and a deadline, and network only to the package
registries, GitHub, the model provider and the telemetry collector. Codex runs there too, with a gateway session token as its only key (no provider key goes into a
sandbox), so with sandboxes on it needs the gateway. `local` (the default) runs sessions in the
tool's process, for development.

**A session's model credential is a short-lived gateway token, not the tool's key**
(`valxel_sdlc/gateway_sessions.py`; strict mode requires it in live mode). With
`VALXEL_LLM_GATEWAY_URL` and `VALXEL_LLM_GATEWAY_API_KEY` (a secret, through the secret store) set,
the tool mints a token from the LLM gateway (ss-llm-gateway, `POST /v1/sessions`) for each Claude Code
session and gives the session the gateway's address and the token instead of `ANTHROPIC_API_KEY`.
The token expires shortly after the session's own deadline, may spend no more than the session's cap,
may use only the session's model and `VALXEL_LLM_GATEWAY_SESSION_MODELS` (default `claude-haiku-*`,
for Claude Code's housekeeping), carries the feature's labels, and is minted for the feature's data
class, so a gateway set up for less refuses a PHI session. The tool revokes it when the session ends
and logs what it spent. If the gateway can't mint one, the session doesn't start (the provider counts
as unavailable, so the role's fallbacks apply). The tool's own caller entry in the gateway's policy
needs a `sessions` grant, and `data_classes` listing every class its products have.

**The tool's own model calls go through the gateway too.** With the gateway set, the Anthropic
provider calls it with the tool's gateway key: each call carries its labels and declares its data
class (`x-valxel-data-class`), and the gateway applies that class's rule (providers, redaction) and
audits it. The tool keeps its roles, fallbacks, budgets and ledger. The tool then needs no provider
key, and strict mode flags one left in its environment. Codex goes through the gateway too: its session gets a token capped at the role's budget, and calls
the gateway's Responses API as its provider. The gateway refuses its next call once the cap is
spent, which is the live dollar cap Codex has none of itself; the task then fails rather than
handing a fresh budget to a fallback. Codex roles count as available behind the gateway once
`VALXEL_LLM_GATEWAY_CODEX=on`, set when the gateway's policy has a provider and prices for Codex's
models (the gateway refuses sessions on unpriced models).

**Only images the platform's build signed run** (`platform/openchoreo/` in the platform governance
repo). The pipeline builds only merge commits that passed the gates. The build scans the image
(high and critical CVEs fail it), writes a CycloneDX SBOM, signs the image with a KMS key only the
workflow plane can use, and attests SLSA provenance naming the repository and commit; the suites
`container` and `provenance` are required for every product, so a build missing any step doesn't
reach an environment. Kyverno on every data plane admits only images signed by that key, with
provenance from one of the product's own repositories and an SBOM, and pins pods to the verified
digest.

**What a change adds is checked** (`valxel_sdlc/dependencies.py`, suite `new-dependencies`): each
package must exist in its registry (a name nobody publishes is a model's invention), be at least 30
days old (new names are where squatting happens) and carry an allowed license
(`config/dependencies.yaml`); an OpenSSF Scorecard under 3 is reported. Dependency manifests are
high-risk paths, so a change to them waits for a person at G5 in every product.

**Agent instructions are high-risk too.** Coder sessions load the checkout's `CLAUDE.md` and
`.claude/` as instructions, and other coding agents read their own files (`AGENTS.md`, `.mcp.json`,
Cursor and Copilot rules). An earlier agent pull request, or anyone with write access, could change
what the next session does. So a change to any of them, whether the plan named it or the pull
request just contains it, makes the change set `high`: a person approves it at G5, and the
feature's events say which files.

**Builds are pinned.** Template and tool workflows pin actions by commit; `supply-chain-pins` pins
Dockerfile base images to their digests and, in Gradle repos, writes `gradle.lockfile` and
`gradle/verification-metadata.xml`, refreshing both on Dependabot's pull requests. The tool's image
installs exactly `requirements.lock` (uv.lock's export, with hashes; CI fails when they differ),
checks the Codex download's checksum, and is scanned with an SBOM in CI.

**What already runs is rescanned.** ECR's enhanced scanning (Amazon Inspector, continuous) rescans
images whenever a CVE is published; an EventBridge rule on `Inspector2 Finding` sends findings to
`POST /api/signals/inspector` (Bearer the account's token, `valxel signals-token account <id>`). High and critical vulnerabilities in
running images open incidents for the component (critical with a fix: sev2), and a closed finding
resolves its signal.

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
  when their provider isn't configured (`OPENAI_API_KEY`, `VALXEL_SELFHOSTED_URL`) or their model
  has no price under `prices`. Every model that runs needs a price, so feature budgets hold
  whichever vendor does the work; self-hosted models are priced at zero (paid for as infrastructure).
- **Codex sessions** get the phase's skills, constitution, product and feature context first in
  the prompt (through stdin), then the task. Codex reads Claude-format plugins, so the same
  skills-only plugins are installed for it in the image (`/opt/valxel/codex-home`, a plugin
  marketplace over `/opt/valxel/plugins`); each session starts from a copy of that home, sees every
  plugin skill by name and reads one when it needs it, unless `on_demand` is off for the role or
  the config pins plugins other than the image's (plugin commands, such as
  `product-management:brainstorm`, reach it only through the pasted skills). Sessions reply with the stage's JSON schema
  (`--output-schema`), run sandboxed to the checkout with no approvals, stop at the role's
  `max_minutes`, and are costed from their token usage. Codex has no dollar cap of its own: a session
  that spent more than its cap is logged and the feature's budget check sees it. A provider that
  can't be reached is given up on after a few reconnects, so the next fallback gets the task.

## Modes

- **stub** (default): `StubProvider` returns each phase's deterministic output and simulates prompt
  caching (exact-prefix match at breakpoints, TTL refreshed on hits, isolated per workspace). Use
  `simulate_faults` on intake, for example `{"spec": 1, "build": 1}`, to exercise loops and
  escalations.
- **live**: `pip install -e ".[live]"`, then set `VALXEL_MODE=live` and `ANTHROPIC_API_KEY`. Roles
  with `runtime: claude_code` run through the Claude Agent SDK; others call the Claude API through
  the gateway. Skills come from the plugins in `VALXEL_SKILLS_DIR` (`<plugin>/skills/<skill>/SKILL.md`,
  or `<plugin>/commands/<skill>.md`). The image installs Anthropic's Product Management,
  Engineering and Design plugins ([knowledge-work-plugins](https://github.com/anthropics/knowledge-work-plugins),
  pinned by commit in `config/plugins.yaml`) and ours, `plugins/valxel` (the critics' skills), at
  `/opt/valxel/plugins`, skills and commands only: `plugins/prepare.sh` leaves out each plugin's
  connectors (`.mcp.json`: Slack, Linear, Atlassian and others) and hooks, so an agent working on
  product code reaches no outside tool through a plugin. Live mode refuses to start while a skill a
  phase uses is missing; CI checks the pinned commit has them all. Live mode is not covered by the
  offline tests. Run it against a sandbox product first.

  **The plugin pin is config.** Moving `knowledge_work_plugins.ref` in `config/plugins.yaml`
  changes every agent's instructions, so it goes the way a model or prompt change does: a pull
  request to the config repo, replayed against past documents (`valxel / evals`), and if wanted
  the canary. A config that pins a commit other than the image's (an eval run's candidate, the
  canary) gets its own copy at that commit, fetched once from GitHub and laid out by
  `plugins/prepare.sh` like the image's. The Dockerfile's `KNOWLEDGE_WORK_PLUGINS_REF` names the
  same commit (a test checks; CI builds with the config's), the image records it in
  `/opt/valxel/plugins/REF`, and strict mode warns when the two differ. The ref must be a full
  commit sha. Changes to our own plugin (`plugins/valxel`) ship with the tool and are not replayed.

  **Skills a session loads by itself.** Besides the skills its phase pastes in, a session sees
  every installed skill by name and may load one. `on_demand` in `config/plugins.yaml` turns that
  off for the roles you list (for example the critic, so it judges against its checklist only).
  Whatever a session does load is recorded as `extra_skills`: on its ledger rows, on its trace
  (`valxel.extra_skills`), and in the feature's agent sessions, which G5 and G6 show.

  A Claude Code session starts from Claude Code's own system prompt (the `claude_code` preset:
  tool use, safety) with the layers L2-L5 appended: the phase's skills in full, the
  constitution, the product and the feature. It reads settings from the repository only
  (`setting_sources=["project"]`: the checkout's CLAUDE.md and `.claude/skills/`, nothing from
  the container's home), and loads every installed plugin unless `on_demand` is off for the role,
  so the agent can also pull in a skill the phase didn't list (it sees only names and descriptions
  until it uses one). The plugins carry skills and commands only, so they give the agent no
  connector, hook or other outside tool. Keep the appended layers under about
  120 KB: the SDK passes them as one process argument, and the runtime warns before the limit.

- **strict**: set `VALXEL_STRICT=1` in production. The switches above are independent, and some
  fall back quietly: a Jira, OpenProject or Lucid server without credentials runs as an offline
  stand-in, a product's flags run in memory, no `DATABASE_URL` keeps everything in memory. Strict
  mode refuses to start while any of them would, and lists each one: `VALXEL_MODE`,
  `VALXEL_DELIVERY`, `VALXEL_AUTH` and `VALXEL_GOVERNANCE_SOURCE` not live, no database, and per
  product a diagram or tracker without credentials, Flipt without its token, or ServiceNow without
  `VALXEL_SERVICENOW_AUTH_<ID>`. In live mode it also refuses secrets from the environment:
  `VALXEL_SECRET_STORE` must be `aws` or `files` (a secret manager's secrets mounted as one file
  each in `VALXEL_SECRETS_DIR`), and no secret may be left in the environment as well. Settings that
  aren't secrets but were kept in the same secret are read from the folder into the environment,
  with a warning naming them. What runs but can't be checked (a shared library that declares no
  package) is logged as a warning. `/api/healthz` reports `strict` (`valxel_sdlc/strict.py`).

## Replacing the stubs

| Stub | Replace with | Interface |
| --- | --- | --- |
| `InMemoryCatalog` (no `DATABASE_URL` only) | `PostgresCatalog` with a database; a mirror to the Backstage catalog in the OpenChoreo portal is still open | `integrations/catalog.py` |
| `StubPackageRegistry` (stub delivery only) | Release tags on GitHub; packages checked in GitHub Packages | `integrations/packages.py` |
| `StubFlags` (stub delivery only) | Each product's Flipt (`tools.flags` in product.yaml) | `integrations/flags.py` |
| Phase templates | Real agent output in live mode; keep the section structure the checks read | `agents/templates.py` |
| Build tasks | Coder agents in sandboxed containers with a checkout of one repo each | `runtime/claude_code.py` |

Have compliance counsel review the regulatory constitution articles.

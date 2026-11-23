# Security packet

For the security and compliance reviewers of a team that runs Valxel. It describes how Valxel
handles code, credentials and decisions. Each section links to the source of truth. Answers to a questionnaire should quote this page, not paraphrase it.

## What Valxel is

Valxel runs a product's software delivery with AI agents:

- intake;
- specification and design;
- planning;
- code and review;
- build;
- release to production.

People approve every step that matters at gates. Agents work in sandboxes and open pull requests
in the team's GitHub. They never hold production credentials, and they never promote to
production.

Everything runs in the team's own cloud account ([install guide](self-hosted.md)):

| | Where |
|---|---|
| The tool | Their Kubernetes, beside their OpenChoreo |
| Agents working on code | gVisor sandboxes in their cluster, while working only |
| Model access | Their own model key, through a gateway in their cluster |
| Audit records | Their audit sink and Object Lock buckets, exportable to their SIEM |
| Products' running environments | Their OpenChoreo data planes |

Code, secrets and audit records never leave their account, except the prompts sent to their
chosen model provider.

## Data: what is kept, where, and for how long

The full inventory, with a deletion rule and evidence for each place, is
[code and secrets](code-and-secrets.md). In short:

- **Code** is handled while an agent works on it. It is deleted with the session's sandbox.
  Folders a crashed worker leaves behind are swept after 6 hours. Session transcripts are
  deleted after 24 hours.
- **Agents' notes** (findings, test failures, review comments) can quote short excerpts of code:
  at most 20 lines per block and 4,000 characters per note. Secrets in them are masked. They stay
  with the feature as its record, and for regulated products in a six-year archive under Object
  Lock.
- **Approved documents** go to the team's own repositories. Agent transcripts, prompts and
  evaluator notes never do.
- **Sample data** is synthetic. Agents never see real patient or cardholder data.
- **Model providers** receive prompts and responses through the gateway, under each provider's
  retention terms. PHI work is refused unless a BAA covers the model account.

## Isolation

- **Agent sessions:**
  - each session is a pod of its own under gVisor (a user-space kernel), in a namespace with
    restricted pod security;
  - no service account token is mounted, and the root file system is read-only;
  - Claude Code's own sandbox applies again inside the pod.
- **Network:**
  - a session may reach the package registries and GitHub on one allowlist (`config/egress.yaml`),
    the LLM gateway, and DNS for those names only;
  - deny rules block other sandboxes, the Kubernetes API, the nodes and the cloud metadata
    endpoint, whatever an allow rule says (Cilium).
- **Workspaces:** one install can hold several workspaces (teams or products). Each workspace's
  rows are separated by Postgres row-level security, enforced for the tool's own login (strict
  mode refuses a login that can bypass it), and checkpoint archives are keyed per workspace.

## Identity and access

- **Sign-in:**
  - GitHub OAuth, Cognito, or the team's own OpenID Connect provider (Okta, Entra ID, Google
    Workspace, Keycloak), with signed, server-revocable session cookies;
  - every decision needs a sign-in from the last 4 hours, and passkeys are available for approver
    groups;
  - "Sign out on every device" and admins can revoke sessions.
- **Roles:** workspace roles are re-read on every request. A gate's approver team is checked with
  GitHub at the moment of the decision. An identity provider's groups are read at sign-in, approve
  only from a two-factor sign-in, and every decision needs a sign-in from the last 4 hours.
- **Separation of duties:** whoever started a feature, or rewrote a phase, cannot approve it at
  the code and release gates (G5, G6), nor at any gate for PHI and payments products. A team with
  one approver can waive this only with a written reason. The waiver is recorded and listed for
  review afterwards.
- **Assistants and other agents** (MCP, A2A) act only with a person's token. Their calls are
  audited with the client named.

## Credentials

- **Secrets are read from a secret store, never from the environment.** Strict mode refuses to
  start in production while a secret sits in the environment.
- **GitHub, two Apps:**
  - The delivery App acts on code, pull requests and checks.
  - The Provisioning App creates repos, and only a separate worker can sign for it.
  - Both private keys live in KMS as sign-only keys: no process holds key material.
  - Each session gets an installation token for one repository, without settings or status rights.
- **Model access:** only the gateway holds a model key. The tool has a gateway key, of which only
  the hash is stored. Each session gets a short-lived gateway token with its own budget; the
  gateway reserves the worst-case cost before every call and refuses once the budget is spent.
- **Redaction:** the gateway redacts every credential type Valxel issues, and secrets in general,
  before a provider sees the text.

## Audit

- **What's recorded:**
  - every gate decision, with the person, time and evidence;
  - workspace changes, model calls and the tool's own requests;
  - every production change.
- **How it's kept:** each record goes into an append-only table with a hash chain. A record that
  can't be written stops the decision.
- **Tamper-evidence:**
  - chain heads are anchored hourly to S3 under Object Lock (compliance mode, six years);
  - records also go through Kafka to a separate audit sink, which keeps its own hash-chained copy
    and a locked archive;
  - `valxel audit` and `audit-sink verify` recompute the chains against the anchors.
- **SIEM:** the sink exports to the team's security monitoring (Splunk, Sentinel and
  similar).

## Supply chain

- **The tool's own images:**
  - built in GitHub Actions, scanned, and signed by digest with keyless cosign;
  - each has an SPDX SBOM and build provenance in the registry;
  - installs pin digests, and the chart refuses a tag. Verification commands are in the
    [install guide](self-hosted.md#2-install).
- **The tool's own code:**
  - pull requests only, with required review;
  - CodeQL, Semgrep, gitleaks and dependency review;
  - a lint and type gate that only ratchets down;
  - an independent AI review on every pull request.
- **Products' code:**
  - the templates and pipelines carry the same scanning, plus SBOMs, signing and image policy at
    deploy;
  - security findings are triaged with deadlines.

## AI-specific risks

The full threat model is [ADR 0008](adr/0008-threat-model-agent-plane.md). It treats an agent as
possibly hostile once it has read anything written outside the tool.

- **Prompt injection:**
  - content from outside the tool is wrapped and marked as untrusted;
  - agents can't reach production or settings;
  - a person reviews code at G5;
  - an injection eval runs on every model or prompt change and must stay at zero.
- **Exfiltration:** egress is limited to the allowlist and DNS to listed names, and the gateway
  redacts credentials.
- **Runaway cost:** each session has its own budget, there's a hard cap at the gateway, and
  unpriced models are refused.
- **Quality drift:** decisions at the gates label evaluation cases. A model or prompt change must
  match or beat the current one on past cases before it ships.

## Shared responsibility

| The Valxel project | The team running it |
|---|---|
| The tool's code, images, chart and their signatures; security patch releases as set out in [support](releases.md#support) | Kubernetes, the gVisor node pool, Cilium, Agent Sandbox and OpenChoreo, patched and upgraded |
| Defaults that are safe: strict mode, digest pins, hardened pods, network policy | Postgres, Kafka, buckets and KMS: encryption at rest, backups, Object Lock retention |
| Release notes, migration notes, the install and upgrade guide | Upgrading to releases that get fixes; secrets and their rotation; the GitHub Apps; who may sign in and approve |
| Handling vulnerabilities reported to the project | The model account and its terms (BAA for PHI) |
| No access to anyone's install | Monitoring, SIEM, incident response for their environment |

## Known gaps

We list these rather than wait to be asked. Each has a tracker item.

- **Package registry proxy (T-136):** agents' package installs go straight to the public
  registries on the allowlist. A proxy with quarantine and age rules, which flags new dependencies
  for a person, isn't in place yet.
- **Install rehearsal (T-123):** the chart is tested in CI but has not yet been installed end to
  end on a real cluster.

## Reporting a vulnerability

Use GitHub's private vulnerability reporting on the repository ([SECURITY.md](../SECURITY.md)).
Reports are acknowledged within two business days.

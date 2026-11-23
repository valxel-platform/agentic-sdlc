# Security packet

For a customer's security and compliance reviewers. It describes how Valxel handles code,
credentials and decisions, for both the hosted and self-hosted models. Each section links to the
source of truth. Answers to a questionnaire should quote this page, not paraphrase it.

## What Valxel is

Valxel runs a product's software delivery with AI agents:

- intake;
- specification and design;
- planning;
- code and review;
- build;
- release to production.

People approve every step that matters at gates. Agents work in sandboxes and open pull requests
in the customer's GitHub. They never hold production credentials, and they never promote to
production.

| | Hosted | Self-hosted |
|---|---|---|
| Where the tool runs | Valxel's AWS account, shared by customers | The customer's Kubernetes, one tenant ([install guide](self-hosted.md)) |
| Where agents work on code | gVisor sandboxes in Valxel's account, while working only | gVisor sandboxes in the customer's account |
| Model access | Valxel's model accounts, through Valxel's gateway | The customer's own model key, through a gateway in their account |
| Audit records | Valxel's audit sink, exportable to the customer's SIEM | The customer's audit sink and buckets |
| Products' running environments | The customer's account (OpenChoreo data planes) | The customer's account |

## Customer data: what is kept, where, and for how long

The full inventory, with a deletion rule and evidence for each place, is
[code and secrets](code-and-secrets.md). In short:

- **Code** is handled while an agent works on it. It is deleted with the session's sandbox.
  Folders a crashed worker leaves behind are swept after 6 hours. Session transcripts are
  deleted after 24 hours.
- **Agents' notes** (findings, test failures, review comments) can quote short excerpts of code:
  at most 20 lines per block and 4,000 characters per note. Secrets in them are masked. They stay
  with the feature as its record, and for regulated products in a six-year archive under Object
  Lock.
- **Approved documents** go to the customer's own repositories. Agent transcripts, prompts and
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
- **Tenancy (hosted):** each workspace's rows are separated by Postgres row-level security,
  enforced for the tool's own login (strict mode refuses a login that can bypass it). Sessions
  have per-workspace network policy, and checkpoint archives are keyed and encrypted per workspace.

## Identity and access

- **Sign-in:**
  - GitHub OAuth or Cognito, with signed, server-revocable session cookies;
  - every decision needs a sign-in from the last 4 hours, and passkeys are available for approver
    groups;
  - "Sign out on every device" and admins can revoke sessions.
- **Roles:** workspace roles are re-read on every request. A gate's approver team is checked with
  GitHub at the moment of the decision.
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
- **SIEM:** the sink exports to the customer's security monitoring (Splunk, Sentinel and
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

## Shared responsibility (self-hosted)

| Valxel | The customer |
|---|---|
| The tool's code, images, chart and their signatures; security patches within the [support window](releases.md#support-window) | Kubernetes, the gVisor node pool, Cilium, Agent Sandbox and OpenChoreo, patched and upgraded |
| Defaults that are safe: strict mode, digest pins, hardened pods, network policy | Postgres, Kafka, buckets and KMS: encryption at rest, backups, Object Lock retention |
| Release notes, migration notes, the install and upgrade guide | Secrets and their rotation; the GitHub Apps; who may sign in and approve |
| Vulnerability handling for reports made to Valxel | The model account and its terms (BAA for PHI) |
| Access to an install only when the customer grants it | Monitoring, SIEM, incident response for their environment |

## Known gaps

We list these rather than wait to be asked. Each has a tracker item.

- **Package registry proxy (T-136):** agents' package installs go straight to the public
  registries on the allowlist. A proxy with quarantine and age rules, which flags new dependencies
  for a person, isn't in place yet.
- **Sign-in:** GitHub or Cognito only. Generic OIDC (Okta, Entra ID) is not yet supported.
- **Self-hosted rehearsal (T-123):** the chart is tested in CI but has not yet been installed end
  to end on a customer-like cluster.
- **Hosted isolation (T-02, T-123, T-125):** per-customer isolation on the hosted shared non-prod
  cluster is to be proven in the hosted rehearsal. Dedicated cells for regulated customers are an
  open decision.
- **Support window:** the [support window](releases.md#support-window) is proposed, not yet
  committed.

## Reporting a vulnerability

Use GitHub's private vulnerability reporting on the repository ([SECURITY.md](../SECURITY.md)).
Reports are acknowledged within two business days.

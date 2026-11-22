# 0008. Threat model for the agent plane

Date: 2026-11-22
Status: Accepted
Deciders: Sunil Vallabhaneni

## Context

The engineering review of 21 November 2026 found four gaps in controls that carry weight: DNS open
to any name from a sandbox, packages installed from any public registry, audit chains that an
insider could rewrite, and gates a person could approve for their own work. They share a root
cause: the controls were designed against an outside attacker, and assumed an honest agent and a
trusted operator. This ADR writes the threat model down, so a new control is judged against it and
a gap shows here before a review finds it.

## What we protect

| Asset | Where it lives |
| --- | --- |
| Customer code | Agent sandboxes while a session runs; the customer's GitHub. Valxel handles it only while working (hosted agreement) |
| Repo credentials | GitHub App installation tokens (`ghs_`), one repository each, minted per session |
| Model access | The LLM gateway's provider keys; sessions hold short-lived gateway tokens (`vgs_`) with their own budget |
| PHI and payment data | Products' environments only; sample data is synthetic. Agents never see real PHI |
| The record of who decided what | Gate decisions, the workspace audit log, the production change log, the audit sink |
| Production | Deploys, flags and rollbacks the tool drives through OpenChoreo |

## Who attacks, and how they get in

| Attacker | Entry points |
| --- | --- |
| A prompt-injected agent | Anything an agent reads: intents and attachments, repo content (README, CLAUDE.md, code comments), issues, logs and alerts it triages, web pages it fetches, package contents |
| An outside attacker | The web app and API, MCP and A2A endpoints, webhooks, sign-in |
| A compromised person's account | A stolen session or GitHub account of an approver or admin |
| An insider | A Valxel operator or customer admin with database or cluster access |
| A customer of a shared deployment | Another workspace's data, sessions or cluster tenancy (the shared non-prod cluster) |

## Controls

Status: **in place**, **new** (with the review fixes), or **gap** (tracker item).

| Threat | Control | Status |
| --- | --- | --- |
| Injected agent leaks code or tokens over the network | Sandbox egress limited to one list of registries and GitHub (`config/egress.yaml`, generated into the cluster policy) | new (T-129) |
| ...through DNS lookups of its own domain | DNS limited to the listed names and two cluster services | new (T-128) |
| ...through the model call itself | Gateway redacts every credential type we issue before a provider sees text | new (T-126) |
| Injected agent installs a malicious package and commits it | Registry proxy with quarantine; new dependencies flagged for a person | gap (T-136) |
| Injected agent pushes harmful code | One-repo token without settings or status rights; G5 review by a person; untrusted-content wrapping; injection eval at zero | in place |
| Agent reaches another sandbox, the node, the API server or instance metadata | gVisor per session, Cilium deny rules, no service-account token, read-only root | in place |
| Agent overspends | Session budget in the gateway; unpriced models refused for every caller | new (T-127); hard cap gap (T-137) |
| Stolen approver session | 4-hour re-sign-in for workspace approvers; passkeys for workspace groups | in place; GitHub-team approvers and revocation are a gap (T-134) |
| One person approves their own work | Separation of duties: the feature's starter and a phase's rewriter can't approve it at G5, G6, and at every gate for PHI and payments (`separation.py`) | new (T-133) |
| Insider rewrites the decision record | Append-only tables with hash chains; chain heads anchored hourly under S3 Object Lock and sent to the audit sink (`audit_anchors.py`); the tool's records in the sink's locked archive | new (T-114, T-113, T-135) |
| Audit records lost or quietly dropped | Outbox in the request, relayed to Kafka at least once; a decision that can't be recorded doesn't happen | in place (T-113) |
| Outside attacker through the web app | Sign-in on every route, workspace roles re-read per request, JSON-only writes, frame-ancestors none, HSTS | in place (headers new, T-142) |
| Assistants and agents acting for a person | MCP and A2A accept only a person's token; calls carry the client and are audited | in place (T-117) |
| One customer sees another's data | Row-level security per workspace; per-workspace network policy for sessions; checkpoint archive keys by workspace | in place; the shared non-prod cluster's per-customer isolation is to be proven in the hosted rehearsal (T-123, T-125) |
| Code kept longer than the work needs | Sandboxes deleted with the session; transcripts after 24 hours; trace content off by default | partly; inventory and deletion rules (T-121) |

## Decision

- Every control that touches agents, credentials, approvals or the audit record is judged against
  this model: which attacker, which entry point, what it stops.
- An agent is treated as possibly hostile once it has read anything a person outside the tool
  wrote. A person with database or cluster access is treated as able to change what the tool
  stores; what must hold against them lives outside the tool's reach (Object Lock, the audit sink).
- The table above is kept current: a fixed gap changes to in place in the PR that fixes it.

## Consequences

- Reviews compare against this list instead of rediscovering it.
- Some controls cost convenience: a one-person pilot must turn separation of duties off for G5 and
  G6 (`separation_of_duties_gates: []`), and that choice is visible in its registry.
- Open gaps stay listed until closed: T-134, T-136, T-137, the hosted cluster's tenant isolation,
  and T-121.

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
| Stolen approver session, or an approver removed from their team | Every decision needs a sign-in from the last 4 hours; a GitHub team it goes through is checked with GitHub at that moment (a "yes" holds 5 minutes; GitHub not answering means wait); sessions are revoked on the server at sign-out, "sign out on every device", an admin's "Sign out" and `valxel sessions revoke`; passkeys for workspace groups | in place (T-134). Needs the GitHub App's organization permission Members: read |
| One person approves their own work | Separation of duties: the feature's starter and a phase's rewriter can't approve it at G5, G6, and at every gate for PHI and payments (`separation.py`). A team with one approver turns on the waiver: the author approves with a written reason, recorded as `waived` and listed at `/api/waivers` for review afterwards | new (T-133, T-143) |
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
- Some controls cost convenience: a team with one approver (a solo founder) can't have real
  separation of duties. It turns on `separation_of_duties_waiver` rather than emptying the gate
  lists: each self-approval then carries a reason, says `waived` in the decision and the audit
  event, and is listed for someone (an advisor, a later hire) to review. That is a compensating
  control, not the control; it ends when a second approver joins.
- Open gaps stay listed until closed: T-136, T-137, the hosted cluster's tenant isolation,
  and T-121.

# 0003. Model calls through a gateway: in the process, and the platform's in front of it

Date: 2026-11-19 (the in-process gateway since the first release; the platform's from T-06/T-08)
Status: Accepted
Deciders: Sunil Vallabhaneni

## Context

Every model call carries a product's data, which has a data class (general, PHI, payments) that
limits which providers may see it (VX-7). Every call costs money that has to land on a feature's
and a product's budget. Agent sessions (Claude Code, Codex) run code and must not hold provider keys.

## Decision

- Inside the tool, every call goes through `gateway.ModelGateway`: it resolves a role to a provider
  and model, enforces the data class's allowlist, applies prompt layering and caching, and writes
  one ledger record per call (`gateway/`). Roles, fallbacks and budgets stay in the tool.
- In deployed environments, the tool's calls and its sessions' calls go on to ss-llm-gateway in the
  same cluster: redaction, metering and audit happen there, provider keys live only there, and
  sessions get short-lived tokens with their own budget (`gateway_sessions.py`, T-06, T-08).

## Alternatives

- Call providers directly from each agent: no single place for the data-class rule, costs or audit.
- Only the platform's gateway: it doesn't know the tool's roles, fallbacks or feature budgets.

## Consequences

- Two layers to configure; strict mode refuses live mode without the platform's gateway.
- A provider can be added once (both gateways translate to it) and the data-class rule is enforced
  twice, by the tool and by the platform.
- Revisit if the platform's gateway takes over roles and fallbacks.

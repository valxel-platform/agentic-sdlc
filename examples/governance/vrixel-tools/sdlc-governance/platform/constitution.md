---
name: platform
version: 1.4.0
---
# Vrixel platform constitution

Applies to every product. Product constitutions may tighten these articles, never loosen them.
Precedence: platform constitution, then product constitution, then ADRs, then PRD and task instructions.

- **VX-1** Humans own intent, architecture and production: G1, G4 and G6 are never automated away. (Human)
- **VX-2** Every artifact has an ID, its parents and the constitution version it was made under. (Policy)
- **VX-3** No agent holds production credentials or promotes to production. (Policy)
- **VX-4** Contracts change before code and stay backward compatible: expand, then contract. (Test)
- **VX-5** Every acceptance criterion maps to at least one automated test. (Policy)
- **VX-6** Reuse before build: check the capability catalog first; a new capability states why. (Rubric)
- **VX-7** PHI and payment data reach only approved model providers, stores and regions. (Policy)
- **VX-8** Every service ships with SLOs, structured logs that carry the trace id, and OpenTelemetry traces that continue the caller's W3C trace context. (Policy)
- **VX-9** User-facing UI meets WCAG 2.1 AA. (Test, Rubric)
- **VX-10** Stay within the per-feature budget; escalate rather than overspend. (Policy)
- **VX-11** Every repository keeps unit-test line coverage at or above 90%, measured on every pull request; a change that drops it below 90% does not merge. (Test)

Agentic features in products (repos with the `agent` and `mcp-server` roles):

- **VX-12** Product agents reach models only through the product's LLM gateway and tools only through MCP servers on an allowlist; they hold no provider keys. (Policy)
- **VX-13** A product agent's changes to records are approved by a person before they happen, unless an ADR names the action as safe to automate; every tool that changes data is annotated, scoped and idempotent. (Policy, Test)
- **VX-14** Product agents ship with evals that gate every pull request and run against the real model after each deployment to dev. (Test)

Audit:

- **VX-15** Every change to product records, every read of PHI or payment records, and every person's decision on an agent's proposal is published as an audit record to the product's audit topic: who (and on whose behalf), the action, the record ids and the outcome, never field values. In PHI and payments products a request whose audit record can't be published fails, and the design names its audited actions. Audit records are kept append-only and tamper-evident, with a write-once backup, for at least six years. (Policy, Test)

Data and events:

- **VX-16** A request that changes product records publishes one event to the product's Kafka and writes no database; no request writes to both. Event workers apply events to the data stores, once and in order, and every store can be rebuilt by replaying its topics. Services coordinate through events; a central orchestrator needs an ADR. (Policy, Test)

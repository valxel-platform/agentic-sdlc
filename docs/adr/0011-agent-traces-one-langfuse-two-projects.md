# 0011. Agent traces: one Langfuse for the tool, with a project for real work and one for tests

Date: 2026-11-24
Status: Accepted
Deciders: Sunil Vallabhaneni

## Context

Every agent session the tool runs is an OpenTelemetry trace. A collector next to the tool sends
the structure to the cloud's tracing (X-Ray on AWS) and, where the data class allows, the content to
a self-hosted Langfuse, where people look into sessions, label them and build eval datasets
(platform-foundation, `docs/agent-observability.md`).

Products' environments each have an OpenChoreo observability plane: one for non-production, one
for production, and separate ones for PHI and payments production. The question was whether
Langfuse should follow the same shape, an instance per environment, for consistency.

The tool runs once, in the tools cluster, and every session it runs there is real work for a real
product. What isn't real work is a handful of trial runs: eval replays of a config change, the
platform's short-lived staging cluster before an upgrade, and dev installs. Today those land in
the same project as real sessions, beside people's labels and the datasets evals come from.

## Decision

- **The tool's agents: one Langfuse, in the tools cluster, with two projects.** `valxel` holds
  real work (every feature's sessions, canary features included, tagged with their config
  version) and people's labels. `valxel-test` holds eval replays, the platform staging cluster and
  dev installs. Each project has its own keys and members, and the test project may keep traces
  for less time.
- **Traces say which they are.** Every span carries `deployment.environment.name`: `production`
  by default, `test` where `VALXEL_TRACE_ENVIRONMENT=test`. Both ways of replaying past work
  (`valxel eval` and the config-PR eval runner) set `test` themselves. Agent sessions inherit the
  value (Claude Code through `OTEL_RESOURCE_ATTRIBUTES`, Codex through its `[otel] environment`).
- **The collector routes on it, failing towards real work.** Only `test` goes to the test project;
  anything else, including a span with no value, goes to `valxel`. A trial can be misfiled into
  real work and cleaned up; real work can't be lost into the test project.
- **Eval datasets live in the test project.** An eval run writes its dataset copy, run items and
  scores there (`VALXEL_LANGFUSE_*` holds the test project's keys). The cases themselves, labelled
  at gates, stay in the tool's database.
- **Products' own agents follow the planes.** When a product built from the Agent or MCP server
  template ships an agent, its traces go to a Langfuse beside that environment's observability
  plane: non-production, production, and separately PHI and payments production, whose data can't
  share a store with anything else. Not built until the first such product.
- **One way of working across all of them.** OpenTelemetry to a collector next to the workload;
  structure to the cloud's tracing, which never holds content; content to Langfuse only as the data
  class allows (`config/telemetry.yaml`); the same retention rules and trace links.

## Alternatives

- **A second Langfuse instance for the tool's non-production.** Mirrors the planes, but the tool
  has no non-production to mirror: the instance would see only trials, and it doubles ClickHouse,
  Postgres, upgrades and keys to operate.
- **One project, filtered by the environment tag.** Cheapest, but every key can write to the real
  datasets and every member sees trials and real work together.
- **A Langfuse per environment for everything, the tool included.** Consistent on paper; it puts
  the tool's traces in a cluster the tool doesn't run in.

## Consequences

- The platform's Langfuse needs a second project, `valxel-test`, with its own keys, and the
  collector a second exporter (platform-foundation).
- The platform staging cluster sets `VALXEL_TRACE_ENVIRONMENT=test` when it is built (T-123).
- Trace links from the tool point at the `valxel` project; trials have none.
- Revisit when the first product's agent ships, or if the test project grows enough to need its
  own instance.

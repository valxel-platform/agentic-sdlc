# 0002. Durable work as LangGraph graphs checkpointed in Postgres

Date: 2026-11-19 (in effect since the first release)
Status: Accepted
Deciders: Sunil Vallabhaneni

## Context

A feature's life runs for days or weeks: agents work for minutes, then the feature waits hours or
days at a gate for a person, or for CI, a deploy, a rollout's bake time. Incidents, initiatives,
releases and new products have the same shape. The work must survive restarts and deploys, resume
on any replica, and keep a record of every step for audits and evals.

## Decision

Each kind of work (feature, initiative, release, incident, inception, review) is a LangGraph state
graph (`graph/`, `ops/incident.py`, `roadmap/graphs.py`, `inception/`). A human gate or an external
wait is an `interrupt`; resuming it is a `Command`. State is checkpointed in Postgres
(`PostgresSaver`); a thread runs on one replica at a time under an advisory lock (`cluster.py`), and
after each run only its latest checkpoint is kept, regulated lanes' history archived first
(`checkpoint_archive.py`).

## Alternatives

- A workflow engine (Temporal, Camunda): durable and mature, but another cluster service to run,
  and workflows in a separate programming model from the agents that do the work.
- Hand-written state machines over database rows: full control, but every wait, retry and resume
  written by hand.
- A queue of tasks without a graph: simple, but the flow between phases and gates becomes implicit.

## Consequences

- Gates and waits are ordinary code (`interrupt`), testable offline with an in-memory saver.
- State must stay serializable, and changing a graph's shape needs care with threads already
  checkpointed under the old one.
- The tool depends on LangGraph's checkpoint format and its upgrades (pinned below 2).
- Revisit if threads need cross-region failover or the checkpoint store becomes the bottleneck.

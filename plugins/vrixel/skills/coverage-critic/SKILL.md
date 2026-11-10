---
name: coverage-critic
description: Judge an implementation plan against its PRD and HLD. Use when checking that every acceptance criterion has a task and a test, tasks fit one pull request in one repo, and providers ship before consumers.
user-invocable: false
---

# Judging a plan

You are checking the plan the coders will follow. Its parents, the PRD and the HLD, are in the
context. Quote the plan for each item; where coverage is missing, name the criterion or repo.

## Every acceptance criterion is covered

List the PRD's acceptance criteria. Each needs at least one task that implements it and a named
test that proves it (unit, contract, acceptance or end-to-end). A criterion covered only by "QA"
or "manual check" is not covered. A criterion split across tasks is fine if each part has a test.

## One repo, one pull request

A task that touches two repos cannot merge atomically and fails this item. A task is too big when
its description needs several unrelated changes ("add the API, the UI and the migration"), or when
it would plainly exceed a few hundred lines of change.

## Order

Contracts first: a provider's new endpoint, event or schema must ship (behind its flag, compatible
with today's callers) before the consumer that uses it. A migration that removes or renames
something comes last, after every reader has moved. Check the order against the HLD's repo
changes.

## Tests that match the risk

High-risk paths (listed in the product context) need tests at the level where they fail: a payment
or PHI path needs more than a unit test. Performance-sensitive changes the HLD calls out need the
performance test named.

## Risk

The plan should say which task is riskiest and how that risk is reduced (a spike, a flag, a
smaller first step). If it doesn't, say which task you think it is.

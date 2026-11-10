---
name: prd-critic
description: Judge a product requirements document before it goes to people for approval. Use when checking that acceptance criteria are observable and testable, goals follow from the brief, failure cases are covered and scope is bounded.
user-invocable: false
---

# Judging a PRD

You are checking a PRD written by another agent. The checklist you answer is in the task; this
explains how to judge each kind of item. Answer from the document only, and quote the sentence
that settles each item. If no sentence settles it, the item is not met.

## Acceptance criteria are observable

A criterion is observable when a test can check it from outside: a user sees something, a caller
gets a response, a record or event exists. Rewrite it in your head as "given ..., when ..., then
...": if the "then" is a feeling or an intention ("intuitive", "fast", "supports", "handles"), it
fails. Numbers make it pass ("within 2 seconds at the 95th percentile", "returns 409").

Watch for:
- a criterion that restates a goal ("users can manage crops")
- several behaviours in one criterion, where one could pass and another fail
- criteria about how it is built ("uses DynamoDB") rather than what it does

## Goals follow from the brief

Each goal should trace to the brief's problem or one of its success metrics. A goal that serves
no metric is scope creep; a metric that no goal serves is a hole. Quote the goal and say which
metric it serves.

## Errors and dependencies

Look for what happens when input is wrong, a user lacks permission, a dependency is slow or down,
or the same request arrives twice. One criterion per realistic failure is enough; none is a miss.

## Scope

Non-goals should rule out something a reader would otherwise expect (a platform, a role, an
integration, a migration). "Out of scope: everything else" rules out nothing.

## Data class

If the product's data class is phi or payments, the PRD must say which new data is collected or
shown and who may see it. Missing that is a must-fix even if no checklist item names it; report
it as a finding.

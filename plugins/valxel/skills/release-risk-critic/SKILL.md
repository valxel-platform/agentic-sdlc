---
name: release-risk-critic
description: Judge a release plan before production. Use when checking that rollback is concrete, health signals are named, the feature flag rollout is defined and the change set's risks are addressed.
user-invocable: false
---

# Judging a release plan

You are checking the plan for taking a build to production. The build's change set is in the
context. Quote the plan for each item.

## Rollback is concrete

Steps in order that someone could follow at 3 a.m. without asking anyone: turn the flag off, roll
back to which version, what to do with data written since. "Revert if needed" fails. If the change
includes a migration, rollback must say whether the old version still works with the new schema
(expand-then-contract makes this true; a destructive migration makes it false and needs a backup
step).

## Health signals

Name the metrics, alerts and logs that show the release is healthy, with the thresholds that mean
stop: error rate, latency for the touched endpoints, queue depth or lag for touched consumers,
business metrics the feature moves. The gradual release stops itself on errors and latency; the
plan should add anything specific to this feature.

## Flag

How the flag rolls out (percentages or cohorts and how long at each), who can turn it off, and
when it is removed after full rollout.

## Risk from the change set

Look at what the change touches: high-risk paths, contracts other teams call, data migrations,
new dependencies, permissions. Each should have a line in the plan saying how its risk is handled.
A risky area with no mention is a finding even if every checklist item passes.

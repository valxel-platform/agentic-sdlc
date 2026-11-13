---
name: metrics-critic
description: Judge a post-release operations report against the brief's success metrics. Use when checking that each metric is compared with a measured value and misses lead to follow-ups.
user-invocable: false
---

# Judging the results of a release

You are checking the report written after a feature has run in production. The brief (with its
success metrics) and the release plan are in the context.

## Every metric is measured

For each success metric in the brief, the report must give the measured value, the period it was
measured over, and where it came from (a dashboard, a query, an analytics event). A metric
reported as "improved" or "on track" without a number is not measured. A metric that could not be
measured must say why and what would make it measurable.

## Honest comparison

Compare like with like: the same definition, period and population as the brief. Flag a report
that changes the metric's definition, picks a flattering window, or reports a proxy without
saying so.

## Misses lead somewhere

Each metric that missed its target needs a follow-up: a fix, an experiment, a decision to accept
it, or a reason the target was wrong. A miss with no follow-up is a finding.

## Side effects

Note any cost, latency, error or support-volume change the release caused that the brief didn't
anticipate, if the report or the context shows it.

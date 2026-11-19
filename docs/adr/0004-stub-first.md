# 0004. Every integration has an offline stand-in; strict mode refuses them in production

Date: 2026-11-19 (in effect since the first release)
Status: Accepted
Deciders: Sunil Vallabhaneni

## Context

The tool talks to GitHub, OpenChoreo, Flipt, PagerDuty, ServiceNow, trackers, AWS and several model
providers. A pipeline that needs all of them to run can't be developed, demoed or tested offline,
and every test would be slow and flaky.

## Decision

Each integration sits behind an interface with an offline stand-in: stub agents (`VALXEL_MODE=stub`),
in-memory delivery (`VALXEL_DELIVERY=stub`), recording pagers, in-memory flags and ledgers. The
whole pipeline runs end to end, deterministically, with no network. Production can't run on a
stand-in by accident: strict mode (`VALXEL_STRICT=1`, `strict.py`) refuses to start while any
stand-in or insecure default is in use. Live integrations are checked against recorded cassettes
(`cassettes.py`, CI's smoke job).

## Alternatives

- Mocks per test: no runnable demo, and each test re-invents the integration.
- Always live, against sandboxes: slow, flaky and costly, and impossible offline.

## Consequences

- Fast, deterministic tests and a demo that runs anywhere.
- Each stand-in is code to keep in step with the real integration; the cassettes and strict mode
  are what catch drift.
- A stand-in's behaviour can be mistaken for the real one's: production readiness is strict mode's
  list, not the demo.

# 0001. Record architecture decisions

Date: 2026-11-19
Status: Accepted
Deciders: Sunil Vallabhaneni

## Context

The tool grew to about 50,000 lines across delivery, operations, the roadmap and intake in a few
months. Its decisions live in commit messages, pull request descriptions and the README, which
says what the tool does but not why it was built this way or what was turned down. The
architecture review (items P2-15, P2-16) asked for the reasons to be written where the next person
will look.

## Decision

Keep architecture decision records in `docs/adr`, one per decision, numbered, in the template's
shape. A pull request that makes an architectural choice adds or supersedes one. The decisions
already taken are recorded first (0002 to 0006).

## Alternatives

- The README alone: it describes behaviour and grows with every feature; reasons get lost in it.
- A wiki or doc outside the repo: drifts from the code and isn't reviewed with it.

## Consequences

Reviewers can ask "which ADR?" of an architectural change. Records are short and never rewritten,
so they stay cheap to keep.

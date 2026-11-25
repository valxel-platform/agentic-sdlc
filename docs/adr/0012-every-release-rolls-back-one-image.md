# 0012. Every release can be rolled back to the image before it: the schema expands, then contracts

Date: 2026-11-24
Status: Accepted
Deciders: Sunil Vallabhaneni

## Context

A release of the tool is an image. In the cluster it runs pinned by digest
(platform-foundation, `gitops/clusters/tools/releases.yaml`), and rolling back is reverting that
pin; the single VM and the laptop do the same with an image tag. A rollback is only real if the
previous image still works against the database the new one left behind.

The tool brings its schema up to date as it starts: each store runs its own
`CREATE TABLE IF NOT EXISTS` and `ADD COLUMN IF NOT EXISTS` statements, and LangGraph's checkpointer
runs its own numbered migrations. So by the time anyone decides to roll back, the new release's
DDL has run, and nothing undoes it. Today every statement is additive, by habit. Nothing keeps it
that way, and one `DROP COLUMN` or `SET NOT NULL` would make the revert fail in the middle of an
incident.

## Decision

Every release keeps the database usable by the image before it.

- **Expand only.** A release adds tables, columns that allow NULL or have a default, and indexes.
  It doesn't drop, rename, retype or tighten anything the previous image reads or writes, and it
  doesn't add constraints that image's writes could break. DDL is idempotent (`IF NOT EXISTS`), so
  either image can start against the database and run its own.
- **Contract later.** Removing or tightening something is a separate, later release, shipped once
  the release before it no longer uses the thing. It's listed in `config/schema-contractions.yaml`
  with that release, so the exception is reviewed and visible.
- **Stored data follows the same rule.** A new field in stored JSON (feature state, checkpoints,
  documents) is optional, and code reads past fields it doesn't know. A change to what an existing
  field means gets a new field instead.
- **Dependencies count.** A major version of `langgraph-checkpoint-postgres` (or anything else that
  migrates the database) is upgraded like a contraction: in a release of its own, after checking
  the previous image runs against its migrated tables.

`tests/test_schema_rollback.py` reads every SQL statement in `src/` and fails on one that breaks the
rule and isn't listed. It reads statements, not intent: the stored-data part is for review.

## Alternatives

- **A migration tool with down-migrations (Alembic).** Down-migrations run at the worst moment,
  are rarely tested, and lose data a newer release wrote. Expand-then-contract needs no reverse
  step at all.
- **Snapshot the database before each release and restore it to roll back.** It loses the work done
  since the release, and it's minutes of downtime; it stays the last resort for a release that
  went wrong in ways a revert can't fix (docs/operations runbooks).
- **Migrate in a separate job before the rollout.** It doesn't change what the previous image has to
  tolerate, and it adds an ordering the three deployment tiers would each have to keep.

## Consequences

- Rolling back is reverting one pin, with no database step, at any tier.
- Dropping something takes two releases, and unused columns linger for a release. That's the price.
- The check covers DDL in the code, not data shapes or a dependency's own migrations; those rely on
  review. A rehearsal that starts the previous image against a database the new one migrated would
  cover them; it comes with the platform staging cluster (platform-foundation, upgrade path).

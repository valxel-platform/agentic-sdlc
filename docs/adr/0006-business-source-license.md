# 0006. The Business Source License 1.1

Date: 2026-11-13
Status: Accepted
Deciders: Sunil Vallabhaneni

## Context

Valxel is a commercial product. Its source should be readable and usable by individuals, small
companies, schools and nonprofits, and by anyone evaluating it, without letting a larger company,
or a cloud provider, run it as a hosted service in competition.

## Decision

License the tool under the Business Source License 1.1 (`LICENSE`), from version 0.2.0:

- Production use is granted to individuals, organizations under 250 people and US$10M revenue,
  schools and nonprofits, as long as they don't offer the tool, or a feature of it, to others as a
  hosted, managed or embedded service. Building and running their own software with it, including
  for clients, is allowed.
- Anything else needs a commercial license.
- Each version becomes Apache 2.0 three years after its first public release.
- Repositories the tool creates for products don't inherit it (they're the customer's).

## Alternatives

- An open source license (Apache 2.0, AGPL): the widest use, but no protection against a hosted
  competitor (Apache) or a license many companies won't touch (AGPL).
- Closed source: no outside evaluation or contribution, and less trust from regulated customers.

## Consequences

- It is not open source; contributors and users must read the grant.
- Dependencies' licenses must allow it; templates and generated product repos are licensed separately.
- Revisit the thresholds and the change date as the business does.

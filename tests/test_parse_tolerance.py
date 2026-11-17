"""The section parsers read the ways Markdown and models write the same list (agents/parse.py tidy)."""

import pytest

from valxel_sdlc.agents.parse import (parse_acs, parse_affected_repos, parse_cases, parse_events, parse_new_repos,
                                      parse_nfrs, parse_trace, section, tidy)

PLAIN = """## Acceptance criteria
- AC-1 (G1): Given a farmer, when they log a crop, then it is saved
- AC-2 (G1): Given no network, when they log, then it is queued

## Non-functional requirements
- NFR-1 (performance, G1): p95 under 300 ms

## Test cases
- TC-1 (AC-1, happy): saves a log -> TASK-1 tests/test_log.py::test_saves
- TC-2 (AC-2, error): queues offline -> acceptance offline.spec.ts "queues a log"

## Traceability
- AC-1 -> TASK-1 / tests/test_log.py

## Affected repos
- acme-farm/farm-api (provider)

## New repos
- acme-farm/farm-sync (event-worker): applies queued logs [ADR-1]

## ADRs
- ADR-1: acme-farm/farm-sync applies the queue

## Events
- valxel.crop.logged.v1: published by farm-api
"""

MESSY = """## 1. Acceptance Criteria:
* **AC-1** (G1): Given a farmer, when they log a crop, then it is saved
1. **AC‑2 (G1):** Given no network, when they log, then it is queued

## **Non-functional requirements**
+ NFR-1 (performance, G1): p95 under 300 ms

## Test Cases
• TC-1 (AC-1, happy): saves a log → TASK-1 `tests/test_log.py::test_saves`
* TC-2 (AC-2, error): queues offline => acceptance `offline.spec.ts` “queues a log”

## Traceability
- `AC-1` → `TASK-1` / `tests/test_log.py`

## Affected repos
* `acme-farm/farm-api` (provider)

## New repos
  - `acme-farm/farm-sync` (event-worker): applies queued logs [ADR-1]

## ADRs
* ADR-1: acme-farm/farm-sync applies the queue

## Events
- `valxel.crop.logged.v1`: published by farm-api
"""

PARSERS = [parse_acs, parse_nfrs, parse_cases, parse_trace, parse_affected_repos, parse_new_repos, parse_events]


@pytest.mark.parametrize("parse", PARSERS, ids=lambda p: p.__name__)
def test_a_messy_document_reads_the_same_as_a_plain_one(parse):
    assert parse(PLAIN) and parse(MESSY) == parse(PLAIN)


def test_nested_items_and_other_lines_are_left_alone():
    body = "Intro line with * a star\n- top\n    * nested deeper\n```\n* code\n```"
    assert tidy(body).splitlines() == ["Intro line with * a star", "- top", "    * nested deeper", "```", "* code", "```"]


def test_emphasis_inside_an_item_and_inline_code_with_spaces_stay():
    assert tidy("* the **only** way: `a b`") == "- the **only** way: `a b`"


def test_a_section_heading_matches_whatever_its_case_number_or_colon_and_not_a_longer_heading():
    doc = "## 2) acceptance CRITERIA\n- AC-1: x\n## Acceptance criteria details\n- AC-9: y"
    assert section(doc, "Acceptance criteria") == "- AC-1: x"
    assert section("### Acceptance criteria\n- AC-1: x", "Acceptance criteria") == ""  # a level-3 heading isn't one

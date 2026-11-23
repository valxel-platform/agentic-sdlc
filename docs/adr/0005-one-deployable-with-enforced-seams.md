# 0005. One deployable with enforced seams, not separate services yet

Date: 2026-11-19
Status: Accepted
Deciders: Sunil Vallabhaneni

## Context

The tool is about 50,000 lines of Python in one package. The architecture review (P2-15) asked to
split it along its seams: intake, the roadmap, delivery and operations. It already runs as two
kinds of process from one image, the API and the workers (`run_queue.py`), so a crash or deploy of
one doesn't stop the other.

Measured when this was written, 85 imports cross between the seams or from the shared core into
them: for example, gates read operations' change log and cohorts, incidents read the roadmap's
releases, and governance validates every seam's settings. The seams are real but not clean.

## Decision

Keep one deployable for now, and make the seams explicit and enforced in the code:

- `config/seams.yaml` names each seam's modules, the core, and the root (engine, API, CLI, evals)
  that wires them together.
- A seam may use the core and itself; the core uses no seam; seams don't use each other.
- `tests/test_seams.py` fails on any new import that breaks this. Today's 85 are listed as known
  debt and the list may only shrink: fix one by moving the shared piece into the core or passing
  it in from the root (`services.py`), then delete its line.

Split a seam into its own service when one of these is true, with a new ADR:

1. it needs to scale or be deployed on its own schedule (operations' signal intake under alert storms);
2. it must run somewhere the rest can't, for data residency or a regulated cell (T-02);
3. a separate team owns it;
4. its failures must not reach the rest (the workers already give some of this).

When that happens, the seam's known breaks are the list of calls to turn into an API or events.

## Alternatives

- Split now into four services: each would need APIs for the 85 crossings, shared tenancy and
  auth, and its own deploys, for no current scaling, team or residency need.
- Leave it as it is: the coupling grows unseen until a split becomes a rewrite.

## Consequences

- No new network calls, deploys or failure modes now; the seams stop getting worse.
- Fixing a known break is ordinary refactoring, visible in the debt list's length.
- The rules are about imports; a seam can still reach another through the database or the engine.
  Revisit if a regulated product needs its operations or delivery to run separately.

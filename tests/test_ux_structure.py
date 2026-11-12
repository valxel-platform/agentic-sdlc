"""Stricter UX checks (each screen's app, states, flow and copy) and sequence diagrams in the architecture."""

from __future__ import annotations

from vrixel_sdlc.agents.parse import parse_copy, parse_flow, parse_screen_apps, parse_screens, parse_sequences
from vrixel_sdlc.evaluators.checks import _flows, _ux_structure

REPOS = [{"name": "acme/web", "role": "consumer"}, {"name": "acme/api", "role": "provider"}]
UX = """## Screens
- S1: Check-in form (web: acme/web)
- S2: Ready list (web: acme/web)
- S3: Help
## States
- S1: loading, empty (blank form), error (fields keep input), success
- S2: loading, success
## Flow
- start -> S1: from the menu
- S1 -> S2: Check in
## Copy
- S1 button: "Check in"
"""


def test_the_parts_parse():
    assert parse_screens(UX) == {"S1": "Check-in form", "S2": "Ready list", "S3": "Help"}
    assert parse_screen_apps(UX) == {"S1": "acme/web", "S2": "acme/web", "S3": ""}
    assert parse_flow(UX) == [("start", "S1", "from the menu"), ("S1", "S2", "Check in")]
    assert parse_copy(UX) == {"S1": ["Check in"]}


def test_a_builder_gets_what_it_needs_or_the_spec_goes_back():
    found = _ux_structure(UX, {"repos": REPOS})
    assert "S3: say which app it's in, as '- S3: <name> (web: <org>/<repo>)' or '(mobile: ...)'" in found
    assert "S2: no error state: what does the person see when it fails, and what can they do?" in found
    assert "S2: no empty state (or 'no empty: <why>')" in found
    assert "S3 (Help) can't be reached from start in the Flow" in found
    assert "Flow: say how the person leaves ('- S2 -> done' or '-> cancel')" in found
    assert "S2 (Ready list) has no copy under Copy" in found
    wrong = UX.replace("(web: acme/web)", "(web: acme/other)", 1)
    assert any("isn't one of the product's apps" in f for f in _ux_structure(wrong, {"repos": REPOS}))


HLD = """## Flows
```mermaid
sequenceDiagram
  title Check in
  participant web
  web->>api: POST /visits
  api-->>web: 201
```
"""


def test_flows_are_drawn_when_components_talk():
    repos = [{"name": "acme/web", "role": "consumer"}, {"name": "acme/api", "role": "provider"}]
    assert parse_sequences(HLD)[0]["participants"] == ["api", "web"]
    assert _flows(HLD, repos) == ["Flows: show at least one failure path (an 'alt' block for a dependency that is slow or down)"]
    ok = HLD.replace("  api-->>web: 201", "  alt api is down\n    api-->>web: 503\n  end")
    assert _flows(ok, repos) == []
    stranger = ok.replace("web->>api", "web->>billing")
    assert any("billing isn't an affected repo" in f for f in _flows(stranger, repos))
    assert "no Flows section" in _flows("", repos)[0]
    assert _flows("", repos[:1]) == []  # one component: nothing to draw

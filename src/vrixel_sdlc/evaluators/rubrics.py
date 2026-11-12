"""What the critic checks in each phase's document: yes/no items, each answered with a quote from
the document. A document passes when every must-have item is met with a quote that is really in
it; should-have items are reported but don't block. The deterministic checks (checks.py) run
first; these are the judgments only a reader can make. The readiness questions (could QA test it,
could a developer build it without asking) come from AWS's AI-DLC reviewers."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Item:
    id: str
    text: str
    must: bool = True


RUBRICS: dict[str, tuple[Item, ...]] = {
    "discovery": (
        Item("problem", "States the problem and who has it, in the users' terms"),
        Item("metrics", "Each success metric has a number or threshold that can be measured after release"),
        Item("evidence", "Names the evidence or assumptions behind the problem"),
        Item("options", "Considers at least one alternative or existing capability", must=False),
        Item("assumptions", "Every assumption the brief relies on is listed under Agent notes as 'Assumed:' "
                            "(none presented as fact)"),
    ),
    "spec": (
        Item("observable", "Every acceptance criterion describes behavior a user or caller can observe"),
        Item("goals", "The goals follow from the brief's problem and success metrics"),
        Item("errors", "Acceptance criteria cover what happens when input is wrong or a dependency fails"),
        Item("testable", "QA could write a test from every acceptance criterion without asking the author"),
        Item("scope", "Non-goals rule out at least one thing a reader might expect", must=False),
        Item("assumptions", "Every assumption the PRD relies on is listed under Agent notes as 'Assumed:' "
                            "(none presented as fact)"),
    ),
    "design": (
        Item("coverage", "Every acceptance criterion in the PRD has a screen or state that shows it"),
        Item("states", "Screens cover empty, loading and error states"),
        Item("access", "Accessibility notes are specific (labels, focus order, contrast or keyboard use)"),
        Item("copy", "Interface text is plain and names actions by what they do", must=False),
    ),
    "architecture": (
        Item("changes", "Says what changes in each affected repo"),
        Item("contracts", "Contract changes stay compatible for existing callers (expand before contract)"),
        Item("data", "Says how the product's data class is protected in the new flow"),
        Item("adrs", "Each ADR states the decision, the alternatives considered and the consequences"),
        Item("buildable", "A developer could build each repo's change from this document without asking the architect"),
        Item("failure", "Describes what happens when a new dependency is slow or down"),
        Item("threats", "Each threat says how it is prevented and how it is detected, and each Authorization line "
                        "says which records a caller may touch, not only who may call"),
        Item("data", "The Data section says what happens to data written by the new version if the release is "
                     "rolled back, and a destructive change is split into expand and contract", must=False),
        Item("blast", "Says what else breaks if a new or changed component fails, and how that is contained",
             must=False),
        Item("performance", "States the expected load and whether the feature adds traffic or data volume",
             must=False),
    ),
    "plan": (
        Item("small", "Each task changes one repo and is small enough for one pull request"),
        Item("tests", "Each task names the tests that prove its acceptance criteria"),
        Item("order", "Task order lets providers ship before the consumers that need them"),
        Item("risks", "Names the riskiest task and how to reduce the risk", must=False),
    ),
    "release": (
        Item("rollback", "The rollback plan lists concrete steps in order"),
        Item("signals", "Names the signals (metrics, alerts, logs) that show the release is healthy"),
        Item("flag", "Says how the feature flag is used during the rollout", must=False),
    ),
    "operate": (
        Item("measured", "Compares each success metric from the brief with a measured value"),
        Item("followups", "Lists follow-ups for metrics that missed their target", must=False),
    ),
}


def for_phase(phase: str) -> tuple[Item, ...]:
    return RUBRICS.get(phase, ())


BRIEF = Item("brief", "Follows the request's experience brief: the screen or flow it names, the main task shown "
                     "first, and the look it asks for; any part it leaves to the design agent is listed under Agent "
                     "notes as a choice made")


def for_request(phase: str, description: str) -> tuple[Item, ...]:
    """The phase's items, plus the experience brief's when the person gave one (intake)."""
    items = for_phase(phase)
    if phase == "design" and "Experience brief:" in (description or ""):
        items = items + (BRIEF,)
    return items


def as_text(items: tuple[Item, ...]) -> str:
    return "\n".join(f"- {i.id} ({'must' if i.must else 'should'}): {i.text}" for i in items)

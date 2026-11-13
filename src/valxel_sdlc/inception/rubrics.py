"""What the critic judges in each inception document: the questions only a reader can answer.
Each must-have item is met only with a quote that is really in the document."""

from __future__ import annotations

from ..evaluators.rubrics import Item

RUBRICS: dict[str, tuple[Item, ...]] = {
    "brief": (
        Item("problem", "States the problem and who has it in the users' own terms, not as a missing feature"),
        Item("outcomes", "Each goal is an outcome for users or the business, not a feature to build"),
        Item("nongoals", "The non-goals rule out something a reader could reasonably expect the product to do"),
        Item("dataclass", "The data class follows from what the product will store and who it serves"),
        Item("evidence", "Each claim about the users, the problem, the alternatives or the market is under Evidence "
                         "citing the research note, or is listed under Assumptions"),
        Item("assumptions", "Every assumption the brief relies on is under Assumptions with a test someone could "
                            "actually run"),
        Item("risks", "Each risk names how it will be handled", must=False),
    ),
    "architecture": (
        Item("traces", "Each component serves a goal or a user job in the brief"),
        Item("boundaries", "Each component has one responsibility, and no two components own the same records"),
        Item("protects", "Says how the product's data class is protected end to end: where the data lives, who "
                         "reads it, and where its audit records go when it isn't general"),
        Item("defaults", "Uses the platform defaults (Kafka for events, Postgres for records) unless an ADR says "
                         "why not"),
        Item("buildable", "A developer could start the first repo from this document without asking the architect"),
        Item("proportionate", "The design is no bigger than the brief's goals and budget need", must=False),
    ),
    "roadmap": (
        Item("first", "The first initiative delivers something people can use end to end (for a new product, a "
                      "thin working version deployed to dev; for an existing one, the most valuable gap)"),
        Item("order", "Each initiative's dependencies come before it, and the reason for the order is clear"),
        Item("outcomes", "Each initiative says which goal it moves"),
        Item("features", "Each candidate feature is small enough to go through the pipeline on its own"),
        Item("sizing", "Budgets are proportionate to each initiative's scope", must=False),
    ),
}

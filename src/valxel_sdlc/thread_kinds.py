"""What kind of thread an id names: CLN-FEAT-3 is a feature, CLN-INI-1 an initiative. Every part of
the tool reads ids, so this is the core's, not the engine's."""

from __future__ import annotations

KINDS = {"FEAT": "feature", "INI": "initiative", "REL": "release", "INC": "incident", "PRD": "inception",
         "REV": "review"}


def kind_of(thread_id: str) -> str:
    """CLN-FEAT-3 -> feature, CLN-INI-1 -> initiative, CLN-REL-2 -> release."""
    parts = thread_id.split("-")
    return KINDS.get(parts[1], "feature") if len(parts) >= 3 else "feature"

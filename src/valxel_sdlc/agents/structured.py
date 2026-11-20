"""Replies the pipeline reads by machine: their shapes, and checking a reply against them.

The critic, the reviewer and the coder each answer in JSON with a fixed shape. The shape goes to
the model with the request (Claude Code also enforces it as structured output), and every reply
is checked against it here, whatever runtime produced it: a reply that doesn't fit is asked for
once more with what was wrong, and then counts as a failure the phase can see. Nothing downstream
guesses at free text.
"""

from __future__ import annotations

import json
import re
from typing import Literal, TypeVar

from pydantic import BaseModel, Field, ValidationError

_FENCED = re.compile(r"```(?:json|valxel-result)?\s*\n(.*?)\n```", re.S)
_OBJECT = re.compile(r"\{.*\}", re.S)


from ..sessions import Handoff  # noqa: E402 - the coder's reply carries one

class ChecklistItem(BaseModel):
    id: str
    met: bool
    # A short quote copied from the document that shows the item is met (empty when it isn't).
    evidence: str = ""
    note: str = ""


class CriticReply(BaseModel):
    items: list[ChecklistItem]
    # The critic's overall 0-1 score. Kept for comparison during the pilot; the checklist decides.
    score: float = Field(ge=0.0, le=1.0)
    notes: list[str] = Field(default_factory=list)


class DesignIssue(BaseModel):
    """The approved design can't be built as written: build stops and people decide whether to
    reopen it (graph/gates.py). Not for preferences or for what the code can work around."""

    document: Literal["spec", "design", "architecture"]  # the approved document that's wrong
    section: str = ""  # its heading, e.g. '## Endpoints'
    problem: str  # what in it can't be built as written, and why
    evidence: str  # what shows it: a failing test, a constraint in the code, a contract, a criterion


class ReviewReply(BaseModel):
    approve: bool
    # What must change before the PR can merge; empty when approved.
    blocking: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    score: float = Field(ge=0.0, le=1.0)
    # Performance risks the change brings (an N+1 query, a call in a loop, an unbounded read, a
    # blocking call on a hot path): each one makes the pull request run its performance smoke test.
    performance: list[str] = Field(default_factory=list)
    # Set only when the approved design itself is wrong (not the code): see DesignIssue.
    design_issue: DesignIssue | None = None


class TestRun(BaseModel):
    command: str = ""
    passed: bool
    failed: list[str] = Field(default_factory=list)


class CoderReply(BaseModel):
    # What changed and why, for the PR description; also names the flag, audit records and events used.
    summary: str
    files: list[str] = Field(default_factory=list)
    tests: TestRun
    # For the reviewer and the people at G5: each 'Assumed: ...', 'Changed: ...', 'Traded off: ...' or
    # 'Open: ...' (valxel_sdlc.diary); empty when there is nothing the diff doesn't show.
    notes: list[str] = Field(default_factory=list)
    # For whoever works on this task next, when it isn't this session resumed (valxel_sdlc.sessions).
    handoff: Handoff = Field(default_factory=Handoff)
    # Set only when the task can't be built as the approved documents say (DesignIssue): build stops.
    design_issue: DesignIssue | None = None


M = TypeVar("M", bound=BaseModel)


def schema(model: type[BaseModel]) -> dict:
    return model.model_json_schema()


def instruction(model: type[BaseModel]) -> str:
    """What to tell the model about the reply's shape."""
    return ("Reply with one JSON object only, matching this JSON schema exactly (no other text):\n"
            + json.dumps(schema(model), separators=(",", ":")))


def parse(text: str, model: type[M]) -> tuple[M | None, str]:
    """The reply as `model`, or None and why it doesn't fit."""
    raw = (text or "").strip()
    candidates = [raw] + [m.group(1) for m in _FENCED.finditer(raw)] + [m.group(0) for m in _OBJECT.finditer(raw)]
    candidates = [c.strip() for c in candidates if c.strip().startswith("{")]
    error = "no JSON object in the reply"
    for c in candidates:
        try:
            return model.model_validate_json(c), ""
        except ValidationError as e:
            error = "; ".join(f"{'.'.join(str(p) for p in x['loc']) or 'reply'}: {x['msg']}" for x in e.errors()[:5])
        except ValueError:
            continue
    return None, error

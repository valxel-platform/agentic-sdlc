"""Graph state for a feature thread and for a single phase subgraph.

Parent state holds IDs, versions and pointers; artifact bodies live in the ArtifactStore.
Every key that more than one node can write in the same step has a reducer, because
Design and Architecture (and their gates G3 and G4) run in parallel.
"""

from __future__ import annotations

import operator
from typing import Annotated, Any, TypedDict


def merge_dict(left: dict | None, right: dict | None) -> dict:
    return {**(left or {}), **(right or {})}


def last(_left: Any, right: Any) -> Any:
    return right


def add_usage(left: dict | None, right: dict | None) -> dict:
    left, right = left or {}, right or {}
    return {
        "tokens": left.get("tokens", 0) + right.get("tokens", 0),
        "cost_usd": round(left.get("cost_usd", 0.0) + right.get("cost_usd", 0.0), 6),
    }


PHASES = ("discovery", "spec", "design", "architecture", "plan", "build", "release", "operate")
GATES = ("g1", "g2", "g3", "g4", "g5", "g6")


class ArtifactRef(TypedDict):
    id: str
    kind: str
    phase: str
    uri: str
    version: int
    parents: list[str]
    constitution_version: str


class GateDecision(TypedDict, total=False):
    gate: str
    phase: str
    lane: str
    decision: str  # approve | revise | takeover | resolved | retry | abort
    approver: str
    feedback: str
    active_minutes: float
    at: str


class FeatureState(TypedDict, total=False):
    # Identity (set once at intake)
    feature_id: str
    product_id: str
    title: str
    description: str
    created_by: str  # GitHub login of the intent creator
    data_class: str
    governance: dict  # commit pins of the platform and product governance repos, set at intake
    constitution_version: str
    simulate_faults: dict[str, int]  # stub mode: phase -> number of defective attempts

    # Written by single nodes or by parallel branches through reducers
    token_budget_usd: Annotated[float, last]  # an escalation can raise it
    status: Annotated[str, last]  # running | done | aborted
    risk_tier: Annotated[str, last]  # low | high | regulated
    tasks: Annotated[list[dict], last]
    release: Annotated[dict, last]
    change_set: Annotated[dict, last]
    lanes_touched: Annotated[list[str], last]  # dependency lanes whose repos the HLD changes

    artifacts: Annotated[dict[str, ArtifactRef], merge_dict]  # kind -> latest ref
    trace_links: Annotated[list[list[str]], operator.add]  # [parent_id, child_id]
    phase_status: Annotated[dict[str, str], merge_dict]  # passed | escalated | resolved
    scores: Annotated[dict[str, float], merge_dict]
    findings: Annotated[dict[str, list[str]], merge_dict]
    loop_count: Annotated[dict[str, int], merge_dict]
    feedback: Annotated[dict[str, str], merge_dict]  # phase -> latest human feedback
    escalations: Annotated[dict[str, dict], merge_dict]
    approvals: Annotated[dict[str, bool], merge_dict]
    pull_requests: Annotated[dict[str, dict], merge_dict]
    gate_decisions: Annotated[list[GateDecision], operator.add]
    events: Annotated[list[dict], operator.add]
    tokens_used: Annotated[int, operator.add]
    cost_usd: Annotated[float, operator.add]


class PhaseState(TypedDict, total=False):
    """Private state of one phase subgraph: produce -> evaluate -> loop or finish."""

    phase: str
    ctx: dict  # read-only snapshot of the feature the phase needs
    attempt: int
    max_loops: int
    threshold: float
    budget_remaining_usd: float
    draft: dict  # {"kind", "content", "extra"}
    score: float
    findings: list[str]
    status: str  # passed | retry | escalated
    reason: str
    usage: Annotated[dict, add_usage]
    task_results: Annotated[dict[str, dict], merge_dict]
    reviews: Annotated[dict[str, dict], merge_dict]

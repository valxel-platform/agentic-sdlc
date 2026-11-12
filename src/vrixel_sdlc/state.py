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
    reason: str  # why it was sent back (pilot.REVISE_REASONS), for revise, retry and abort
    waited_minutes: float  # how long the question waited for this decision
    shadow_auto: dict  # pilot: whether the trial rule would have approved it without a person
    changed: bool  # an escalation resolved with the person's own version of the document


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
    related_features: list[dict]  # similar features and capabilities found when it was created
    not_duplicate_reason: str  # why the creator says it isn't a duplicate, when it looked like one
    initiative_id: str  # the approved initiative this feature delivers ('' = unplanned work)
    track: str  # standard | hotfix (an incident's fix: starts at planning, on-call approves G5 and G6)
    profile: str  # feature | express: which phases and gates the work goes through (vrixel_sdlc.profiles)
    hotfix: dict  # {incident_id, repo, change, severity, approved_by} for the hotfix track
    pilot: bool  # measured in the pilot report, with the shadow modes on
    attachments: list  # files attached at intake (vrixel_sdlc.attachments), under the feature's artifacts
    config_variant: str  # "canary": the feature's agents run with the candidate config (services.default_canary)

    # Written by single nodes or by parallel branches through reducers
    token_budget_usd: Annotated[float, last]  # an escalation can raise it
    status: Annotated[str, last]  # running | done | aborted
    risk_tier: Annotated[str, last]  # low | high | regulated
    tasks: Annotated[list[dict], last]
    cases: Annotated[list[dict], last]  # the plan's test cases (agents/parse.py parse_cases)
    release: Annotated[dict, last]
    change_set: Annotated[dict, last]
    lanes_touched: Annotated[list[str], last]  # dependency lanes whose repos the HLD changes
    design: Annotated[dict, last]  # the design review G4 approved: decisions, supersessions, contract changes
    flag: Annotated[str, last]  # the feature's flag key, created when G4 approves ('' = no flag service)
    ci_retries: Annotated[int, last]  # automatic build retries after red CI, since CI was last green
    fix_retries: Annotated[int, last]  # agents' fixes for failures after merge, before production (since staging last passed)
    fix_branch: Annotated[str, last]  # the branch the current fix goes on ('' before merge)
    performance: Annotated[dict, last]  # repo -> the performance test it needs (vrixel_sdlc.performance)
    instrumentation_check: Annotated[dict, last]  # the feature's product events counted in staging (outcomes)
    rerun_repos: Annotated[list[str], last]  # repos whose CI failed: the build retry redoes only their tasks

    artifacts: Annotated[dict[str, ArtifactRef], merge_dict]  # kind -> latest ref
    diaries: Annotated[dict[str, list[dict]], merge_dict]  # phase -> the agents' notes for the gate (vrixel_sdlc.diary)
    trace_links: Annotated[list[list[str]], operator.add]  # [parent_id, child_id]
    phase_status: Annotated[dict[str, str], merge_dict]  # passed | escalated | resolved
    scores: Annotated[dict[str, float], merge_dict]
    findings: Annotated[dict[str, list[str]], merge_dict]
    loop_count: Annotated[dict[str, int], merge_dict]
    feedback: Annotated[dict[str, str], merge_dict]  # phase -> latest human feedback
    escalations: Annotated[dict[str, dict], merge_dict]
    approvals: Annotated[dict[str, bool], merge_dict]
    # Whether the feature changes what people see: when it doesn't, UX design and G3 are skipped.
    # Decided at creation (the intake's judgement), and G2's approver can change it.
    ui: Annotated[bool, last]
    ui_why: Annotated[str, last]
    code_read: Annotated[dict, last]  # the repos the architect read, at which commit (vrixel_sdlc.workspace)
    fit: Annotated[dict, last]  # whether the approved design and architecture fit together (vrixel_sdlc.fit)
    pull_requests: Annotated[dict[str, dict], merge_dict]
    # Each planned test case's test on its task's branch: {case id: {req, task, file, test, found}}.
    case_evidence: Annotated[dict[str, dict], merge_dict]
    traceability: Annotated[dict, last]  # the matrix G6 approved (traceability.snapshot), with any override
    gate_decisions: Annotated[list[GateDecision], operator.add]
    new_repos: Annotated[list[dict], last]  # repos created for this feature, with what was applied
    deployments: Annotated[dict[str, dict], merge_dict]  # component -> {"env", "release", "repo", ...}
    resources: Annotated[list[dict], last]  # AWS resources G4 approved, as OpenChoreo Resources (VX-19)
    cost_plan: Annotated[dict, last]  # what G4 approved it to cost: AWS per environment (vrixel_sdlc.ops.cost)
    rollout: Annotated[dict, last]  # store apps' staged rollout: {"step", "at", "attempt", "history"}
    delivery: Annotated[dict[str, dict], merge_dict]  # wait (ci, build, dev, ...) -> latest result
    quality: Annotated[dict[str, dict], merge_dict]  # stage (pr, build, dev, ...) -> suites evaluated
    waivers: Annotated[list[dict], operator.add]  # accepted security findings, each with an expiry
    diagrams: Annotated[list[dict], operator.add]  # published architecture diagrams, one per HLD version
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
    critic: dict  # the last critic verdict: checklist items, and the 0-1 score with what it would have decided

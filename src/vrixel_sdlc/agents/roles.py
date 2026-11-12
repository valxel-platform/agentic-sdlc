"""Agent roster: which plugin skills staff each phase, what it produces, what checks it."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PhaseSpec:
    phase: str
    role: str
    kind: str  # artifact kind produced
    skills: tuple[str, ...]  # plugin skills loaded as the producer's instructions
    critic_skills: tuple[str, ...]  # skills the critic scores against
    parents: tuple[str, ...]  # artifact kinds this phase reads
    gate: str | None  # human gate after the phase
    required_capabilities: frozenset[str] = frozenset({"text"})


PHASE_SPECS: dict[str, PhaseSpec] = {
    "discovery": PhaseSpec(
        "discovery", "discovery", "OPP",
        ("product-management:product-brainstorming", "product-management:synthesize-research",
         "product-management:competitive-brief", "design:user-research", "design:research-synthesis"),
        ("product-management:brainstorm",), (), "g1",
    ),
    "spec": PhaseSpec(
        "spec", "spec", "PRD",
        ("product-management:write-spec", "product-management:roadmap-update"),
        ("vrixel:prd-critic",), ("OPP",), "g2",
    ),
    "design": PhaseSpec(
        "design", "designer", "UX",
        ("design:design-system", "design:ux-copy", "design:design-handoff"),
        ("design:design-critique", "design:accessibility-review"), ("PRD",), "g3",
    ),
    "architecture": PhaseSpec(
        "architecture", "architect", "HLD",
        ("engineering:system-design", "engineering:architecture"),
        ("engineering:tech-debt",), ("PRD",), "g4",
    ),
    "plan": PhaseSpec(
        "plan", "planner", "PLAN",
        ("product-management:sprint-planning", "engineering:testing-strategy"),
        ("vrixel:coverage-critic",), ("PRD", "HLD", "UX"), None,
    ),
    "build": PhaseSpec(
        "build", "coder", "BUILD",
        ("engineering:debug", "engineering:documentation"),
        ("engineering:code-review",), ("PLAN", "HLD", "UX"), "g5",
        required_capabilities=frozenset({"text"}),  # code_execution in live deployments
    ),
    "release": PhaseSpec(
        "release", "release", "REL",
        ("engineering:deploy-checklist",),
        ("vrixel:release-risk-critic",), ("BUILD",), "g6",
    ),
    "operate": PhaseSpec(
        "operate", "ops", "OPS",
        ("engineering:incident-response", "engineering:standup",
         "product-management:metrics-review", "product-management:stakeholder-update"),
        ("vrixel:metrics-critic",), ("OPP", "REL"), None,
    ),
}

# Where each gate sends the thread on approval.
GATE_PHASE = {"g1": "discovery", "g2": "spec", "g3": "design", "g4": "architecture", "g5": "build", "g6": "release"}
GATE_APPROVE_TARGET: dict[str, str | list[str]] = {
    "g1": "spec",
    "g2": ["design", "architecture"],
    "g3": "join",
    "g4": "join",
    "g5": "merge_train",
    "g6": "operate",
}
NEXT_AFTER_PHASE = {"discovery": "g1", "spec": "g2", "design": "g3", "architecture": "g4",
                    "plan": "build", "release": "g6", "operate": "finalize"}

# Skills agents are given outside the phases above (incidents, roadmaps, release notes).
EXTRA_SKILLS: tuple[str, ...] = (
    "engineering:incident-response", "engineering:debug", "engineering:documentation",
    "engineering:deploy-checklist", "product-management:roadmap-update", "product-management:write-spec",
)


def used_skills() -> set[str]:
    """Every plugin skill an agent may be given; live mode refuses to start if one is missing."""
    names = set(EXTRA_SKILLS)
    for spec in PHASE_SPECS.values():
        names |= set(spec.skills) | set(spec.critic_skills)
    return names

"""Workflow profiles: how much of the pipeline a piece of work goes through.

Not every change needs discovery and a UX design. The person who starts the work picks a profile
(or the product's default), and the tool keeps the gates that matter whatever the profile:

  feature   everything: discovery G1, spec G2, design G3 and architecture G4 in parallel, plan,
            build, CI (G5 when risky), dev, staging, release G6, production
  express   a small, low-risk change (copy, a label, a setting, a minor tweak): no discovery and no
            UX design, and the spec isn't a gate of its own. G4 approves the spec and the design
            together

Defects are not a profile. Before production the pipeline fixes its own failures (red CI, a failed
build, deployment or test in dev or staging, a security finding): the agents get what failed and
try again, up to the product's max_loops, and people decide only when they can't. In production,
alerts, error reports and reported problems open incidents, which mitigate and then hotfix or
change the intent (ops/incident.py).

No profile skips architecture (it finds the repos, contracts and lanes a change touches), planning,
tests, security scans, the performance rules, G5 for risky changes, or G6 before production. An
incident's hotfix is its own track (graph/pipeline.py hotfix_brief), decided by on-call.

Products choose which profiles their people may use (product.yaml `workflow.profiles`; feature and
nothing else by default). Express needs listing explicitly, and never applies to PHI or payments
products: their specs are always reviewed on their own.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Profile:
    name: str
    title: str
    skips: tuple[str, ...]  # phases that don't run
    skips_gates: tuple[str, ...]  # gates decided by the profile, not a person
    starts_at: str  # the first phase


PROFILES: dict[str, Profile] = {
    "feature": Profile("feature", "Feature", (), (), "discovery"),
    "express": Profile("express", "Express", ("discovery", "design"), ("g1", "g2", "g3"), "spec"),
}
DEFAULT_ALLOWED = ("feature",)
REGULATED = ("phi", "payments")


def get(name: str | None) -> Profile:
    return PROFILES.get(name or "feature", PROFILES["feature"])


def allowed(workflow: dict | None, data_class: str) -> list[str]:
    """The profiles a product's people may use."""
    names = list((workflow or {}).get("profiles") or DEFAULT_ALLOWED)
    return [n for n in names if n in PROFILES and not (n == "express" and data_class in REGULATED)]


def default(workflow: dict | None) -> str:
    return str((workflow or {}).get("default") or "feature")


def check(raw: dict | None, data_class: str) -> list[str]:
    """Problems with a product's `workflow:` block."""
    if raw is None:
        return []
    if not isinstance(raw, dict):
        return ["workflow must be a mapping"]
    problems = [f"workflow.{k} is not a setting (use profiles, default)" for k in sorted(set(raw) - {"profiles", "default"})]
    names = raw.get("profiles")
    if names is not None:
        if not isinstance(names, list) or not names:
            problems.append("workflow.profiles must be a non-empty list")
            names = []
        for n in names:
            if n not in PROFILES:
                problems.append(f"workflow.profiles: {n} is not a profile ({', '.join(PROFILES)})")
            elif n == "express" and data_class in REGULATED:
                problems.append(f"workflow.profiles: express isn't available to {data_class} products")
        if "feature" not in names:
            problems.append("workflow.profiles must include feature")
    d = raw.get("default")
    if d is not None and d not in (names or DEFAULT_ALLOWED):
        problems.append(f"workflow.default: {d} is not one of the product's profiles")
    return problems


def brief(state: dict, profile: Profile) -> str:
    """The opportunity brief a profile without discovery starts from: what the person wrote."""
    return "\n".join([
        f"# Opportunity brief: {state['title']}", "",
        "## Problem", (state.get("description") or state["title"]).strip(), "",
        "## Capability", "Extends: the existing capability this change belongs to", "",
        "## Success metrics", "- M1: the change described above is live, with no new errors on the affected journeys", "",
        "## Profile", f"- {profile.title}: discovery and UX design skipped; the spec is approved with the "
        "architecture at G4", "",
    ])

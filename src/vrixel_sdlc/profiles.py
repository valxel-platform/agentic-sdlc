"""Workflow profiles: how much of the pipeline a piece of work goes through.

Not every change needs discovery and a UX design. The person who starts the work picks a profile
(or the product's default), and the tool keeps the gates that matter whatever the profile:

  feature   everything: discovery G1, spec G2, design G3 and architecture G4 in parallel, plan,
            build, CI (G5 when risky), dev, staging, release G6, production
  bugfix    a defect in something that exists: no discovery and no UX design. Spec G2 states the
            defect and how to tell it is fixed; architecture G4 finds where; then the rest
  express   a small, low-risk change: as bugfix, and the spec isn't a gate of its own. G4 approves
            the spec and the design together

No profile skips architecture (it finds the repos, contracts and lanes a change touches), planning,
tests, security scans, the performance rules, G5 for risky changes, or G6 before production. An
incident's hotfix is its own track (graph/pipeline.py hotfix_brief), decided by on-call.

Products choose which profiles their people may use (product.yaml `workflow.profiles`; feature and
bugfix by default). Express needs listing explicitly, and never applies to PHI or payments
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
    "bugfix": Profile("bugfix", "Bug fix", ("discovery", "design"), ("g1", "g3"), "spec"),
    "express": Profile("express", "Express", ("discovery", "design"), ("g1", "g2", "g3"), "spec"),
}
DEFAULT_ALLOWED = ("feature", "bugfix")
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
    what = "Defect" if profile.name == "bugfix" else "Change"
    return "\n".join([
        f"# Opportunity brief: {state['title']}", "",
        "## Problem", (state.get("description") or state["title"]).strip(), "",
        "## Capability", f"Extends: the existing capability this {what.lower()} belongs to", "",
        "## Success metrics", f"- M1: the {what.lower()} described above is resolved, with no new errors on the "
        "affected journeys", "",
        "## Profile", f"- {profile.title}: discovery and UX design skipped "
        f"({'the spec is approved with the architecture at G4' if 'g2' in profile.skips_gates else 'the spec is reviewed at G2'})",
        "",
    ])

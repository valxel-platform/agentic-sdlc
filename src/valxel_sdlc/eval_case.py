"""One eval case: what an agent was asked, what it produced, and what people (or what happened
afterwards) said about it. The core's, since agents in several parts of the tool keep cases
(evals.py replays them; independent_review.py keeps the independent reviewer's).

The cases a document phase, a coder and its reviewer, intake and triage leave are built here too, so
the seams that keep them don't import the evals (the root's, which replay every seam's agents)."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime


@dataclass
class EvalCase:
    id: str
    product_id: str
    feature_id: str
    phase: str
    data_class: str
    at: str
    config_version: str
    ctx: dict
    output: str
    status: str  # passed | escalated
    critic: dict = field(default_factory=dict)
    # What people decided: {"decision", "reason", "feedback", "first_time"}; None until a gate decides.
    label: dict | None = None
    reference: str = ""  # the version people approved (artifact text), once there is one

    def as_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> EvalCase:
        return EvalCase(**{k: d[k] for k in EvalCase.__dataclass_fields__ if k in d})


# --------------------------------------------------------------------------- document phases

DOC_PHASES = ("discovery", "spec", "design", "architecture", "plan")


# What a replay doesn't need from the phase's context, or must not inherit from the run it came from.
_DROP = ("feedback", "prior_findings", "simulate_faults", "budget_remaining_usd", "pilot", "deployments", "quality")


def now() -> str:
    return datetime.now(UTC).isoformat()


def case_from(state: dict, phase: str, ctx: dict, out: dict, *, config_version: str = "") -> EvalCase:
    """The eval case a finished document phase leaves behind."""
    n = sum(1 for e in state.get("events") or [] if e.get("node") == phase and e.get("kind") in ("passed", "escalated"))
    keep = json.loads(json.dumps({k: v for k, v in ctx.items() if k not in _DROP}, default=str))
    from .agents import PHASE_SPECS

    content = (out.get("draft") or {}).get("content", "")
    # A phase without a gate (the plan) has no person's decision: the next phase takes what passed.
    no_gate = not getattr(PHASE_SPECS[phase], "gate", None) and out.get("status") == "passed"
    return EvalCase(id=f"{state['feature_id']}:{phase}:{n + 1}", product_id=state["product_id"],
                    feature_id=state["feature_id"], phase=phase, data_class=ctx.get("data_class", ""),
                    at=now(), config_version=config_version, ctx=keep,
                    output=content, status=out.get("status", ""), critic=dict(out.get("critic") or {}),
                    label={"decision": "no_gate", "first_time": None} if no_gate else None,
                    reference=content if no_gate else "")



# --------------------------------------------------------------------------- coder and reviewer

CODE_PHASES = ("coder", "reviewer")  # the cases a coder session and its review leave


# What a replay keeps of the build context: the rest is per-run state (feedback, sessions, faults).
_CODE_DROP = ("feedback", "prior_findings", "simulate_faults", "budget_remaining_usd", "pilot", "sessions",
         "deployments", "quality")


def _keep(ctx: dict) -> dict:
    return json.loads(json.dumps({k: v for k, v in ctx.items() if k not in _CODE_DROP}, default=str))


def coder_case(state_or_ctx: dict, task: dict, output: str, *, config_version: str = ""):
    ctx = state_or_ctx
    return EvalCase(id=f"{ctx['feature_id']}:{task['id']}:coder", product_id=ctx["product_id"],
                    feature_id=f"{ctx['feature_id']}:{task['id']}", phase="coder", data_class=ctx.get("data_class", ""),
                    at=now(), config_version=config_version, ctx={"build": _keep(ctx), "task": dict(task)},
                    output=output, status="passed")


def reviewer_case(ctx: dict, task: dict, tail: str, pr_url: str, output: str, *, config_version: str = ""):
    return EvalCase(id=f"{ctx['feature_id']}:{task['id']}:reviewer", product_id=ctx["product_id"],
                    feature_id=f"{ctx['feature_id']}:{task['id']}", phase="reviewer",
                    data_class=ctx.get("data_class", ""), at=now(), config_version=config_version,
                    ctx={"build": _keep(ctx), "task": dict(task),
                         "tail": tail.replace(f"Review {pr_url}", "Review the change below", 1)},
                    output=output, status="passed")


def label_merged(store, feature_id: str, prs: dict, pr_files) -> int:
    """Label a merged change set's coder and reviewer cases: the merge commit and the hidden tests.
    `pr_files(repo, number)` lists a pull request's files. Returns how many tasks were labeled."""
    from .evaluators.weakening import TEST_FILE

    n = 0
    for pr in prs.values():
        if pr.get("state") != "merged" or not pr.get("merge_sha") or not pr.get("task_id"):
            continue
        files = pr_files(pr["repo"], pr["number"]) or []
        tests = sorted(f["path"] for f in files if TEST_FILE.search(f["path"]) and f.get("status") != "removed")
        label = {"decision": "merged", "repo": pr["repo"], "pr": pr["number"], "merge_sha": pr["merge_sha"],
                 "tests": tests}
        for phase in CODE_PHASES:
            store.label(f"{feature_id}:{pr['task_id']}", phase, label)
        n += 1
    return n


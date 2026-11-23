"""One eval case: what an agent was asked, what it produced, and what people (or what happened
afterwards) said about it. The core's, since agents in several parts of the tool keep cases
(evals.py replays them; independent_review.py keeps the independent reviewer's)."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field


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

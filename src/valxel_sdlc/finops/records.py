"""What one cost is: a record in FinOps FOCUS columns, and the categories, stages and bases it uses
(valxel_sdlc.finops.ledger explains them). The core's: delivery's gate packets read costs as well as
ops' collectors and the ledger, which stays with ops (ADR 0005).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

STAGES = ("build", "run")


CATEGORIES = ("llm", "ci", "test", "build", "environment", "aws", "gcp","azure","compute", "incident", "people", "platform")


BASES = ("estimate", "metered", "billed", "allocated")


_FOCUS = {"llm": ("AI and Machine Learning", "Anthropic"), "ci": ("Developer Tools", "GitHub"),
          "test": ("Developer Tools", "GitHub"), "build": ("Developer Tools", "OpenChoreo"),
          "environment": ("Compute", "OpenChoreo"), "aws": ("", "AWS"), "gcp": ("", "Google Cloud"),
          "compute": ("Compute", "OpenChoreo"),
           "azure": ("", "Microsoft Azure"),
          "incident": ("AI and Machine Learning", "Anthropic"), "people": ("Other", "People"),
          "platform": ("Other", "Valxel platform")}


def now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass
class CostRecord:
    key: str
    product_id: str
    stage: str  # build | run
    category: str
    usd: float
    period_start: str
    period_end: str
    feature_id: str = ""
    env: str = ""
    source: str = ""  # usage-ledger, github-actions, openchoreo-finops, cost-explorer, rates, allocation
    service: str = ""  # e.g. Amazon DynamoDB, claude-opus-5-5, GitHub Actions (Linux)
    resource: str = ""  # repo, component, run, PR, gate
    quantity: float = 0.0
    unit: str = ""  # tokens, minutes, hours
    basis: str = "metered"
    status: str = "final"
    detail: dict = field(default_factory=dict)
    updated_at: str = ""

    def __post_init__(self) -> None:
        if self.stage not in STAGES or self.category not in CATEGORIES or self.basis not in BASES:
            raise ValueError(f"cost record {self.key}: bad stage, category or basis")
        self.usd = round(float(self.usd), 6)


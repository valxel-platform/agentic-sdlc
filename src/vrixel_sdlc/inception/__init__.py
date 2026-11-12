"""Product inception: from a vision to a product brief, a product architecture and a roadmap.

A person describes a product (or names an existing one) and its goals. Agents write three
documents in turn, each checked by machine (inception/checks.py) and by a critic against a
checklist (inception/rubrics.py), and revised until both pass or the loops run out:

    brief         the problem, users, goals with measures, non-goals, the data class, the product's
                  id, org and budget                                  -> the brief gate
    architecture  components and their repo roles, AWS resources from the platform catalog with a
                  monthly cost per environment, NFRs, founding ADRs
    roadmap       initiatives in order, each with its goals, budget and candidate features, and
                  which components each feature builds                 -> the product gate

The product council approves the brief before any architecture is written, then the architecture
and roadmap together. This module writes and checks the documents; the gates and the setup that
turns approved documents into a governance repo come later (docs: Product inception design).

Until the brief gate approves a data class, every call runs as PHI: models under a BAA only.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

DOCS = ("brief", "architecture", "roadmap")
KIND = {"brief": "BRIEF", "architecture": "PARCH", "roadmap": "ROADMAP", "research": "RESEARCH"}
ROLE = {"brief": "strategist", "architecture": "architect", "roadmap": "strategist"}
SKILLS = {
    "brief": ("product-management:write-spec", "product-management:competitive-brief"),
    "architecture": ("engineering:system-design", "engineering:architecture"),
    "roadmap": ("product-management:roadmap-update",),
}
DATA_CLASSES = ("general", "phi", "payments")
STRICTEST = "phi"
ENVIRONMENTS = ("dev", "staging", "production")
_PID = re.compile(r"^[A-Z]{2,6}$")


@dataclass
class Vision:
    """What the person said: a new product's vision and goals, or an existing product to describe."""

    name: str
    org: str
    vision: str
    goals: list[str] = field(default_factory=list)     # "Fewer dry fields (measure: under 2 a month per farm)"
    users: list[str] = field(default_factory=list)
    constraints: list[str] = field(default_factory=list)  # compliance, platform, timeline
    budget_usd: float = 0.0                             # the first year's features
    product_id: str = ""                                # proposed (new) or existing
    existing: bool = False                              # describe an existing product
    attachments_text: str = ""
    research_depth: str = "quick"                       # quick | thorough | own (vrixel_sdlc.research)

    @staticmethod
    def from_markdown(text: str, *, org: str = "", product_id: str = "", existing: bool = False) -> "Vision":
        """A vision written as Markdown: '# Name', free text, then '## Goals', '## Users',
        '## Constraints' and '## Budget' sections (each optional)."""
        from ..agents.parse import section

        title = re.search(r"^#\s+(.+)$", text, re.M)
        head = re.split(r"(?m)^## ", text, maxsplit=1)[0]
        body = re.sub(r"^#\s+.+$", "", head, count=1, flags=re.M).strip()

        def bullets(name: str) -> list[str]:
            return [ln.strip()[2:].strip() for ln in section(text, name).splitlines() if ln.strip().startswith("- ")]

        budget = re.search(r"[\d][\d,.]*", section(text, "Budget"))
        return Vision(name=(title.group(1).strip() if title else "New product"), org=org, vision=body,
                      goals=bullets("Goals"), users=bullets("Users"), constraints=bullets("Constraints"),
                      budget_usd=float(budget.group(0).replace(",", "")) if budget else 0.0,
                      product_id=product_id, existing=existing)

    def as_request(self) -> str:
        """The vision as the agents read it (inside an untrusted block)."""
        parts = [f"Product: {self.name}", f"GitHub org: {self.org}", "", self.vision]
        for label, items in (("Goals", self.goals), ("Users", self.users), ("Constraints", self.constraints)):
            if items:
                parts += ["", f"{label}:", *[f"- {i}" for i in items]]
        if self.budget_usd:
            parts += ["", f"Budget for the first year of features: ${self.budget_usd:,.0f}"]
        if self.product_id:
            parts += ["", f"{'Existing' if self.existing else 'Proposed'} product id: {self.product_id}"]
        return "\n".join(parts)


def valid_product_id(pid: str) -> bool:
    return bool(_PID.match(pid or ""))

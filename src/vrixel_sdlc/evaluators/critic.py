"""Rubric critic. Runs in a fresh context: it sees only the artifact, its parents, the rubric
skills and the constitutions, never the producer's reasoning (judge independence, tier 1)."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from ..agents.context import build_layers
from ..gateway import CallTags
from ..runtime import AgentTask

_JSON = re.compile(r"\{.*\}", re.S)


@dataclass
class CriticVerdict:
    score: float
    notes: list[str]
    tokens: int
    cost_usd: float


def stub_verdict(content: str) -> str:
    """Deterministic stub score: lower when the artifact is visibly incomplete."""
    score = 0.9 if len(content) > 120 else 0.5
    return json.dumps({"score": score, "notes": []})


def parse_verdict(text: str) -> tuple[float, list[str]]:
    m = _JSON.search(text or "")
    if not m:
        return 0.0, ["Critic returned no JSON verdict"]
    try:
        data = json.loads(m.group(0))
        return float(data.get("score", 0.0)), [str(n) for n in data.get("notes", [])]
    except (ValueError, TypeError):
        return 0.0, ["Critic verdict was not valid JSON"]


def critique(services, *, phase: str, rubric_skills: tuple[str, ...], content: str, ctx: dict) -> CriticVerdict:
    product = services.settings.product(ctx["product_id"])
    layers = build_layers(
        role="critic",
        skills=rubric_skills,
        constitution=services.constitution(ctx["product_id"]),
        product=product,
        parent_texts=ctx.get("parent_texts", {}),
        tail=(
            f"Score this {phase} artifact from 0 to 1 against the rubric skills and the constitution. "
            'Reply with JSON only: {"score": <float>, "notes": [<string>, ...]}.\n\n' + content
        ),
    )
    tags = CallTags(
        product_id=ctx["product_id"], feature_id=ctx["feature_id"], phase=phase, role="critic",
        data_class=product.data_class, workspace=product.workspace,
    )
    res = services.runtime_for("critic").run(
        AgentTask(role="critic", phase=phase, tags=tags, layers=layers, stub_text=stub_verdict(content), max_tokens=512)
    )
    score, notes = parse_verdict(res.text)
    return CriticVerdict(score=score, notes=notes, tokens=res.tokens, cost_usd=res.cost_usd)

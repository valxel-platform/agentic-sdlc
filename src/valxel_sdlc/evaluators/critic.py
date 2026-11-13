"""Checklist critic. Runs in a fresh context: it sees only the artifact, its parents, the rubric
skills and the constitutions, never the producer's reasoning (judge independence, tier 1).

It answers the phase's checklist (rubrics.py) yes or no, each yes with a quote from the document.
The tool checks that every quote is really in the document, so a yes can't be invented, and the
document passes when every must-have item is met. The critic's overall 0-1 score is still asked
for and recorded beside the checklist's result, so the pilot can compare the two ways of deciding.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from ..agents import structured, untrusted
from ..agents.context import build_layers, skills_dir_of
from ..gateway import CallTags
from ..runtime import AgentTask
from .. import lessons
from . import rubrics

_JSON = re.compile(r"\{.*\}", re.S)


@dataclass
class CriticVerdict:
    score: float  # share of must-have items met (1.0 when there's no checklist)
    notes: list[str]
    tokens: int
    cost_usd: float
    passed: bool = True
    items: list[dict] = field(default_factory=list)
    legacy_score: float = 0.0  # the critic's own 0-1 score, for comparison


def _plain(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[*_`>#]", "", text or "")).strip().lower()


def quoted(evidence: str, content: str) -> bool:
    """Whether the quote is really in the document (ignoring case, spacing and Markdown marks)."""
    q = _plain(evidence)
    return len(q) >= 3 and q in _plain(content)


def stub_verdict(content: str, phase: str = "", items: tuple[rubrics.Item, ...] | None = None) -> str:
    """Deterministic stub: every item met with a quote from the document, unless the document is
    visibly incomplete, when the first must-have item isn't."""
    lines = [ln.strip("# ").strip() for ln in content.splitlines() if ln.strip("# ").strip()]
    quote = lines[0][:80] if lines else ""
    short = len(content) <= 120
    items = [{"id": i.id, "met": not (short and n == 0), "evidence": "" if (short and n == 0) else quote}
             for n, i in enumerate(rubrics.for_phase(phase) if items is None else items)]
    return json.dumps({"items": items, "score": 0.5 if short else 0.9, "notes": []})


def parse_verdict(text: str) -> tuple[float, list[str]]:
    """A score and notes from a JSON reply (the reviewer's older shape; kept for callers that score)."""
    m = _JSON.search(text or "")
    if not m:
        return 0.0, ["Critic returned no JSON verdict"]
    try:
        data = json.loads(m.group(0))
        return float(data.get("score", 0.0)), [str(n) for n in data.get("notes", [])]
    except (ValueError, TypeError):
        return 0.0, ["Critic verdict was not valid JSON"]


def judge(reply: structured.CriticReply, content: str, items: tuple[rubrics.Item, ...]) -> tuple[bool, list[dict], list[str]]:
    """Pass or fail from the checklist; quotes not found in the document don't count."""
    got = {i.id: i for i in reply.items}
    out, findings = [], []
    for item in items:
        a = got.get(item.id)
        met = bool(a and a.met and quoted(a.evidence, content))
        why = ("" if met else "not answered" if a is None else
               (a.note or "not met") if not a.met else "its quote isn't in the document")
        out.append({"id": item.id, "must": item.must, "met": met, "evidence": a.evidence if a else "", "why": why})
        if item.must and not met:
            findings.append(f"Checklist: {item.text} ({why})")
    return not findings, out, findings


def critique(services, *, phase: str, rubric_skills: tuple[str, ...], content: str, ctx: dict,
             role: str = "critic") -> CriticVerdict:
    snap = services.snapshot(ctx["product_id"], ctx.get("governance"))
    product = snap.product
    # The phase's checklist, plus the product's approved lessons marked to be checked.
    items = rubrics.for_request(phase, ctx.get("description", "")) + lessons.checklist(snap.lessons, phase)
    ask = (f"Check this {phase} document against the checklist below and the constitution. Try to refute it: "
           "assume an item isn't met until the document's own words show it is, and look for what is missing as "
           "hard as for what is wrong. For each item, say whether it is met; when it is, copy a short exact quote "
           "from the document that shows it (evidence); when it isn't, say specifically why, naming the section "
           "or id (AC-2, TASK-3, M1). Also give your overall score from 0 to 1, and notes on what to improve (a "
           "concern you can't tie to the document goes in notes, not as an unmet item)."
           "\n\nChecklist:\n" + rubrics.as_text(items) + "\n\n" + structured.instruction(structured.CriticReply)
           + "\n\nThe document:\n" + untrusted.wrap(f"{phase}-document", content))
    tags = CallTags(
        product_id=ctx["product_id"], feature_id=ctx["feature_id"], phase=phase, role=role,
        data_class=product.data_class, workspace=product.workspace,
    )
    tokens, cost, reply, error = 0, 0.0, None, ""
    for attempt in range(2):  # one more try when the reply doesn't fit the shape
        tail = ask if not attempt else ask + f"\n\nYour last reply didn't fit the schema ({error}). Reply again."
        variant = ctx.get("config_variant", "")
        layers = build_layers(role="critic", skills=rubric_skills, constitution=snap.constitution, product=product,
                              product_context=snap.product_context, parent_texts=ctx.get("parent_texts", {}), tail=tail,
                              skills_dir=skills_dir_of(services, variant))
        runtime = services.runtime_for(role, variant) if variant else services.runtime_for(role)
        res = runtime.run(AgentTask(
            role=role, phase=phase, tags=tags, layers=layers, skills=tuple(rubric_skills), stub_text=stub_verdict(content, phase, items), max_tokens=1024,
            schema=structured.schema(structured.CriticReply)))
        tokens, cost = tokens + res.tokens, cost + res.cost_usd
        reply, error = structured.parse(res.text, structured.CriticReply)
        if reply is not None:
            break
    if reply is None:
        return CriticVerdict(score=0.0, notes=[f"Critic reply didn't fit the checklist schema: {error}"],
                             tokens=tokens, cost_usd=cost, passed=False)
    if not items:  # a phase without a checklist: the score alone
        return CriticVerdict(score=reply.score, notes=reply.notes, tokens=tokens, cost_usd=cost,
                             passed=True, legacy_score=reply.score)
    passed, judged, findings = judge(reply, content, items)
    must = [j for j in judged if j["must"]]
    share = sum(j["met"] for j in must) / len(must) if must else 1.0
    return CriticVerdict(score=round(share, 4), notes=findings + reply.notes, tokens=tokens, cost_usd=cost,
                         passed=passed, items=judged, legacy_score=reply.score)

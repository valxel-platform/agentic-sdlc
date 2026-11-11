"""Writing the inception documents: each by its agent, checked by machine and by the critic, and
revised until both pass or the loops run out.

    brief = draft(services, vision, "brief")                    # -> the brief gate
    arch = draft(services, vision, "architecture", brief=approved_brief)
    road = draft(services, vision, "roadmap", brief=approved_brief, architecture=arch.text)  # -> the product gate

The documents are drafts held by the caller until the gates; nothing here writes to Git or GitHub.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field

from ..agents import structured, untrusted
from ..agents.context import load_skill, persona
from ..evaluators.critic import judge, stub_verdict
from ..gateway import CallTags, PromptLayers
from ..resources import load_catalog, summary
from ..runtime import AgentTask
from . import DOCS, ENVIRONMENTS, KIND, ROLE, SKILLS, STRICTEST, Vision, checks, formats, rubrics, templates

WORKSPACE = "wrkspc-inception"


@dataclass
class Draft:
    doc: str
    text: str
    status: str  # passed | escalated
    attempts: int
    findings: list[str] = field(default_factory=list)  # what was still wrong when it stopped
    checklist: list[dict] = field(default_factory=list)
    cost_usd: float = 0.0
    monthly_usd: dict = field(default_factory=dict)  # the architecture's cost per environment
    data_class: str = STRICTEST  # the class its calls ran under

    def as_dict(self) -> dict:
        return asdict(self)


# ------------------------------------------------------------------------------------ context

def platform(services) -> dict:
    """The platform's constitution and AWS catalog, read from its governance repo."""
    s, p = services.settings, services.settings.platform
    src = services.governance.source
    sha = src.resolve(p.org, p.repo, p.ref)
    constitution = src.read(p.org, p.repo, s.platform_constitution_path, sha) or ""
    catalog = load_catalog(src.read(p.org, p.repo, s.platform_resources_path, sha, optional=True) or "")
    return {"constitution": constitution, "catalog": catalog, "sha": sha}


def existing_context(services, product_id: str) -> str:
    """For an existing product: what it is today, its repos and the decisions already made."""
    from ..graph.phase import design_knowledge

    snap = services.snapshot(product_id)
    p = snap.product
    _, knowledge = design_knowledge(services, snap)
    repos = "\n".join(f"- {r.name} ({r.role})" for r in p.repos)
    return (f"# The product today: {p.name} ({product_id}, org {p.org}, data class {p.data_class})\n"
            f"Repositories:\n{repos}\n\n{snap.product_context or ''}\n\n{knowledge or ''}").strip()


def context_for(services, vision: Vision, doc: str, *, brief: str = "", architecture: str = "",
                plat: dict | None = None) -> dict:
    """What the checks (and the stub templates) need for this document."""
    plat = plat or platform(services)
    pb = formats.product(brief) if brief else {}
    dc = formats.data_class(brief)[0] if brief else ""
    ctx = {"existing": vision.existing, "org": vision.org, "registered": set(services.settings.registry),
           "product_id": pb.get("id") or vision.product_id, "prefix": pb.get("prefix", ""),
           "budget_usd": pb.get("budget_usd", 0.0), "data_class": dc, "catalog": plat["catalog"]}
    if vision.existing and vision.product_id:
        ctx["product_id"] = vision.product_id
        try:
            repos = services.snapshot(vision.product_id).product.repos
            ctx["prefix"] = ctx["prefix"] or (repos[0].name.split("/", 1)[1].split("-", 1)[0] + "-" if repos else "")
        except Exception:
            pass
    if brief:
        ctx["goals"] = [g["id"] for g in formats.goals(brief) if g.get("id")]
        ctx["goal_lines"] = templates.goal_lines(brief)
    if architecture:
        ctx["components"] = [c["name"] for c in formats.components(architecture) if c.get("name")]
    return ctx


# --------------------------------------------------------------------------------- the calls

def _layers(services, role: str, skills: tuple[str, ...], constitution: str, product_context: str,
            feature_context: str, tail: str) -> PromptLayers:
    skill_text = "\n\n".join(load_skill(s, getattr(services.settings, "skills_dir", "") or None) for s in sorted(skills))
    return PromptLayers(tools="", skills=f"{persona(role)}\n\n{skill_text}", constitution=constitution,
                        product_context=product_context, feature_context=feature_context, tail=tail)


def _tags(vision: Vision, doc: str, role: str, data_class: str) -> CallTags:
    pid = vision.product_id or "INCEPTION"
    slug = re.sub(r"[^a-z0-9]+", "-", vision.name.lower()).strip("-")[:40] or "product"
    return CallTags(product_id=pid, feature_id=f"inception-{slug}", phase=f"inception_{doc}", role=role,
                    data_class=data_class, workspace=WORKSPACE, prompt_version=f"inception_{doc}@1")


def write(services, vision: Vision, doc: str, ctx: dict, *, parents: dict[str, str], constitution: str,
          product_context: str, data_class: str, feedback: list[str], previous: str) -> tuple[str, float]:
    role = ROLE[doc]
    tail = [f"Write the product {doc} for {vision.name}.",
            "The person's request:\n" + untrusted.wrap("vision", vision.as_request())]
    if vision.attachments_text:
        tail.append(vision.attachments_text)
    if doc == "architecture" and ctx.get("catalog"):
        from ..resources import catalog_guide

        tail.append(catalog_guide(ctx["catalog"], ctx.get("data_class", "")))
    if doc == "roadmap" and not vision.existing:
        tail.append("The first initiative is the thin working version: sign-in, one end-to-end path through the "
                    "components, and the deployment pipeline, deployed to dev.")
    tail.append(formats.contract(doc))
    if feedback:
        tail.append("Fix these findings from the checks and the critic:\n" + "\n".join(f"- {f}" for f in feedback))
    if previous:
        tail.append("Your previous draft, to revise:\n" + untrusted.wrap(f"previous-{doc}", previous))
    parent_text = "\n\n".join(f"<artifact kind=\"{KIND[k]}\">\n{v}\n</artifact>" for k, v in parents.items() if v)
    layers = _layers(services, role, SKILLS[doc], constitution, product_context, parent_text, "\n\n".join(tail))
    stub = templates.TEMPLATES[doc](vision, ctx)
    res = services.runtime_for(role).run(AgentTask(role=role, phase=f"inception_{doc}",
                                                   tags=_tags(vision, doc, role, data_class), layers=layers,
                                                   stub_text=stub, max_tokens=6000, skills=SKILLS[doc]))
    return res.text, float(res.cost_usd or 0.0)


def critique(services, vision: Vision, doc: str, text: str, *, constitution: str, data_class: str) -> tuple[bool, list[dict], list[str], float]:
    items = rubrics.RUBRICS[doc]
    ask = (f"Check this product {doc} against the checklist below and the constitution. Try to refute it: assume "
           "an item isn't met until the document's own words show it is. For each item, say whether it is met; "
           "when it is, copy a short exact quote that shows it; when it isn't, say specifically why."
           "\n\nChecklist:\n" + "\n".join(f"- {i.id}{'' if i.must else ' (should)'}: {i.text}" for i in items)
           + "\n\n" + structured.instruction(structured.CriticReply)
           + "\n\nThe document:\n" + untrusted.wrap(f"{doc}-document", text))
    layers = _layers(services, "critic", (), constitution, "", "", ask)
    res = services.runtime_for("critic").run(AgentTask(
        role="critic", phase=f"inception_{doc}", tags=_tags(vision, doc, "critic", data_class), layers=layers,
        stub_text=stub_verdict(text, items=items), max_tokens=1500, schema=structured.schema(structured.CriticReply)))
    reply, error = structured.parse(res.text, structured.CriticReply)
    if reply is None:
        return False, [], [f"The critic's reply didn't fit the checklist ({error})"], float(res.cost_usd or 0.0)
    passed, judged, findings = judge(reply, text, items)
    return passed, judged, findings + list(reply.notes), float(res.cost_usd or 0.0)


def monthly_cost(text: str, catalog: dict) -> tuple[dict, str]:
    """The architecture's resources priced from the catalog, per environment, and as a section."""
    s = summary(text, catalog, list(ENVIRONMENTS), None)
    if not s:
        return {}, ""
    rows = ["| Resource | Type | " + " | ".join(e.capitalize() for e in ENVIRONMENTS) + " |",
            "| --- | --- | " + " | ".join("---:" for _ in ENVIRONMENTS) + " |"]
    for r in s["resources"]:
        rows.append(f"| {r['name']} | {r['type']} | "
                    + " | ".join(f"${r['monthly_usd'][e]:,.0f} ({r['sizes'][e]})" for e in ENVIRONMENTS) + " |")
    rows.append("| Total | | " + " | ".join(f"${s['monthly_usd'][e]:,.0f}" for e in ENVIRONMENTS) + " |")
    section = ("## Monthly cost\n\nPriced by the tool from the platform's AWS catalog: planning figures per "
               "environment, not quotes. Compute for the components themselves is the platform's.\n\n" + "\n".join(rows))
    return s["monthly_usd"], section


def draft(services, vision: Vision, doc: str, *, brief: str = "", architecture: str = "",
          max_loops: int | None = None, feedback: str = "", previous: str = "") -> Draft:
    """One document, written and revised until the checks and the critic pass."""
    if doc not in DOCS:
        raise ValueError(f"No inception document {doc}; one of {', '.join(DOCS)}")
    if doc != "brief" and not brief:
        raise ValueError(f"The {doc} needs the approved brief")
    if doc == "roadmap" and not architecture:
        raise ValueError("The roadmap needs the architecture")
    plat = platform(services)
    ctx = context_for(services, vision, doc, brief=brief, architecture=architecture, plat=plat)
    # Strictest until the brief gate approves a data class; then the approved one.
    data_class = ctx["data_class"] if doc != "brief" and ctx["data_class"] else STRICTEST
    constitution = plat["constitution"]
    product_context = existing_context(services, vision.product_id) if vision.existing and vision.product_id else ""
    parents = {"brief": brief, "architecture": architecture}
    loops = max_loops or services.settings.limits.max_loops
    # A gate's feedback starts the loop: the people's words first, then the draft they read.
    text, cost, judged = previous, 0.0, []
    findings = [f"From the people at the gate: {feedback}"] if feedback else []
    for attempt in range(1, loops + 1):
        text, spent = write(services, vision, doc, ctx, parents=parents, constitution=constitution,
                            product_context=product_context, data_class=data_class, feedback=findings, previous=text)
        cost += spent
        findings = checks.run(doc, text, ctx)
        if findings:
            continue
        passed, judged, notes, spent = critique(services, vision, doc, text, constitution=constitution,
                                                data_class=data_class)
        cost += spent
        if passed:
            monthly = {}
            if doc == "architecture":
                monthly, cost_section = monthly_cost(text, plat["catalog"])
                if cost_section and "## Monthly cost" not in text:
                    text = text.rstrip() + "\n\n" + cost_section + "\n"
            return Draft(doc, text, "passed", attempt, [], judged, round(cost, 4), monthly, data_class)
        findings = notes
    return Draft(doc, text, "escalated", loops, findings, judged, round(cost, 4), {}, data_class)


def run(services, vision: Vision, *, brief: str = "", through: str = "roadmap") -> list[Draft]:
    """The next documents in order. Without an approved brief, only the brief is drafted: the brief
    gate decides before any architecture is written. With one, the architecture and then (unless
    `through` is "architecture") the roadmap, for the product gate."""
    if not brief:
        return [draft(services, vision, "brief")]
    a = draft(services, vision, "architecture", brief=brief)
    if a.status != "passed" or through == "architecture":
        return [a]
    return [a, draft(services, vision, "roadmap", brief=brief, architecture=a.text)]

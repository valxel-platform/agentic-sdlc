"""Builds the six prompt layers for an agent call (see gateway/prompt.py)."""

from __future__ import annotations

import os
import re
from functools import lru_cache
from pathlib import Path

from ..config import ProductConfig
from ..constitution import Constitution
from ..gateway import PromptLayers, fit, serialize_tools
from . import untrusted

# Design knowledge (decisions, contracts, catalog) is put in the prompt by the pipeline rather than
# offered as lookup tools: the corpus per product is small and bounded, and prefetching makes every
# agent see the same facts that the design check and G4 reviewers see.
_BASE_TOOLS: list[dict] = []
_ROLE_TOOLS = {
    "coder": [
        {"name": "code_index_query", "description": "Symbols, callers and tests for a query", "input_schema": {"type": "object", "properties": {"q": {"type": "string"}}}},
        {"name": "apply_patch", "description": "Apply a unified diff in the sandbox checkout", "input_schema": {"type": "object", "properties": {"diff": {"type": "string"}}}},
        {"name": "run_tests", "description": "Run tests; returns failing tests only", "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}}},
    ],
    "reviewer": [
        {"name": "read_diff", "description": "Read a PR diff", "input_schema": {"type": "object", "properties": {"pr": {"type": "string"}}}},
    ],
    "release": [
        {"name": "openchoreo_status", "description": "Workflow run and environment status", "input_schema": {"type": "object", "properties": {"component": {"type": "string"}}}},
    ],
}


def skill_path(name: str, base: str | None = None) -> Path | None:
    """Where `<plugin>:<skill>` lives under VALXEL_SKILLS_DIR: the plugin's skills/<skill>/SKILL.md,
    or, for a command-style skill, its commands/<skill>.md. None when it is not installed."""
    plugin, _, skill = name.partition(":")
    base = base if base is not None else os.environ.get("VALXEL_SKILLS_DIR")
    if not base or not plugin or not skill:
        return None
    for path in (Path(base) / plugin / "skills" / skill / "SKILL.md", Path(base) / plugin / "commands" / f"{skill}.md"):
        if path.is_file():
            return path
    return None


def skills_dir_of(services, variant: str = "") -> str:
    """The plugins folder the config's agents use (the canary's may pin another commit)."""
    stack = services._stack(variant) if variant and hasattr(services, "_stack") else services
    settings = getattr(stack, "settings", None)
    return getattr(settings, "skills_dir", "") or ""


def missing_skills(names, base: str | None = None) -> list[str]:
    return sorted(n for n in names if skill_path(n, base) is None)


@lru_cache(maxsize=512)
def load_skill(name: str, base: str | None = None) -> str:
    """The skill's instructions from `base` (VALXEL_SKILLS_DIR when None), or a stable placeholder
    (stub mode; live mode refuses to start while a skill is missing, see services.check_skills)."""
    path = skill_path(name, base or None)
    if path:
        return path.read_text(encoding="utf-8")
    plugin = name.partition(":")[0]
    return (
        f"# Skill {name}\n"
        f"Placeholder. Install the {plugin} plugin and set VALXEL_SKILLS_DIR to load the real SKILL.md.\n"
    )


def product_pack(product: ProductConfig, context: str = "", design_knowledge: str = "") -> str:
    """L4: the product's own context.md from its governance repo, plus its registered lane."""
    repos = "\n".join(f"- {r.name} ({r.role})" for r in product.repos) or (
        f"- None yet. Propose them in the HLD's 'New repos' section, named {product.org}/"
        f"{product.repo_prefix or '<prefix>'}<words>, each with an ADR.")
    return (
        f"# Product context: {product.name} ({product.product_id}, org {product.org})\n"
        f"Data class: {product.data_class}\n"
        f"Repositories:\n{repos}\n"
        f"High-risk paths: {', '.join(product.high_risk_paths)}\n"
        "Conventions: contracts first (expand, then contract); every acceptance criterion has a test; "
        "structured logs and traces in every service.\n"
        + (f"\n{context.strip()}\n" if context.strip() else "")
        + (f"\n{design_knowledge.strip()}\n" if design_knowledge.strip() else "")
    )


PERSONAS = Path(__file__).parent / "personas"
# Variants of a role (fallbacks, second opinions, pilot shadows) work from the same persona.
_VARIANT = re.compile(r"_(codex|open|second|shadow)$")
# Roles whose work runs as an agent session with a turn or time cap.
SESSION_ROLES = ("architect", "coder", "reviewer")

SAVE_BEFORE_CAP = """## Your turn budget
Your session stops at a fixed number of turns or minutes, possibly without warning, and whatever
you haven't replied by then is lost. Keep the last part of your budget for the reply: a reply
with fewer verified findings or a partial change described honestly always beats no reply. When
you are running low, stop investigating, list what you couldn't check as open questions, and
reply."""


def base_role(role: str) -> str:
    return _VARIANT.sub("", role)


@lru_cache(maxsize=64)
def persona(role: str) -> str:
    """Who the agent is (agents/personas/<role>.md), then the rules every role follows."""
    base = base_role(role)
    path = PERSONAS / f"{base}.md"
    text = path.read_text(encoding="utf-8").strip() if path.is_file() else f"# Role: {base}"
    parts = [text, untrusted.RULE]
    if base in SESSION_ROLES:
        parts.append(SAVE_BEFORE_CAP)
    return "\n\n".join(parts)


def build_layers(
    *,
    role: str,
    skills: tuple[str, ...],
    constitution: Constitution,
    product: ProductConfig,
    parent_texts: dict[str, str],
    tail: str,
    product_context: str = "",
    design_knowledge: str = "",
    skills_dir: str = "",
    caps: dict | None = None,
) -> PromptLayers:
    """The six layers, each of L2 to L5 cut to its cap (config `context_caps`) with a note saying
    what was left out; `meta["trimmed"]` lists the notes."""
    caps = caps or {}
    tools = serialize_tools(_BASE_TOOLS + _ROLE_TOOLS.get(role, []))
    skill_text = "\n\n".join(load_skill(s, skills_dir or None) for s in sorted(skills))
    feature = "\n\n".join(f"<artifact kind=\"{k}\">\n{parent_texts[k]}\n</artifact>" for k in sorted(parent_texts))
    trimmed = {}

    def capped(layer: str, text: str, label: str) -> str:
        out, note = fit(text, int(caps.get(layer) or 0), label)
        if note:
            trimmed[layer] = note
        return out

    return PromptLayers(
        tools=tools,
        skills=capped("L2", f"{persona(role)}\n\n{skill_text}", "The role's skills"),
        constitution=capped("L3", constitution.text, "The constitution"),
        product_context=capped("L4", product_pack(product, product_context, design_knowledge), "The product context"),
        feature_context=capped("L5", feature, "The feature context"),
        tail=tail,
        meta={"trimmed": trimmed} if trimmed else {},
    )


# --- L5 for one build task (the context design's C4) ------------------------------------------

def _repo_names(task: dict) -> tuple[str, ...]:
    repo = task.get("repo") or ""
    return (repo, repo.split("/", 1)[-1]) if repo else ()


def _mentions(line: str, words) -> bool:
    return any(re.search(rf"(?<![\w-]){re.escape(w)}(?![\w-])", line) for w in words if w)


# HLD sections whose lines are each about one repo or one requirement: only the task's lines stay.
_PER_LINE = ("Affected repos", "Contracts", "Endpoints", "Events", "Instrumentation", "Audit", "Dependency lanes",
             "New repos", "Coverage", "Threats")
# Plan sections filtered to the task's id and criteria.
_PLAN_LINES = ("Traceability", "Test cases", "Acceptance tests")
# UX sections that list one line per screen.
_UX_LINES = ("Screens", "States", "Coverage")


def _filter_section(body: str, keep) -> tuple[str, int]:
    """A section's lines: bullets kept when `keep(line)`, other lines (sub-headings, notes) kept."""
    out, dropped = [], 0
    for ln in body.splitlines():
        if ln.startswith("- ") and not keep(ln):
            dropped += 1
            continue
        out.append(ln)
    return "\n".join(out), dropped


def _by_section(text: str, rules: dict) -> tuple[str, list[str]]:
    """Each '## ' section through its rule: None drops it, a predicate filters its bullets,
    anything else keeps it whole. Returns the text and what was left out."""
    from .parse import split_sections

    head, sections = split_sections(text)
    parts, left = [head.rstrip()] if head.strip() else [], []
    for name, body in sections:
        rule = rules.get(name, True)
        if rule is None:
            left.append(name)
            continue
        if callable(rule):
            body, dropped = _filter_section(body, rule)
            if dropped:
                left.append(f"{dropped} {name} line{'s' if dropped > 1 else ''} about other tasks")
        parts.append(body.rstrip())
    return "\n\n".join(p for p in parts if p), left


def task_texts(parent_texts: dict[str, str], task: dict, where: str = "") -> dict[str, str]:
    """What one build task's coder or reviewer needs from the feature's PLAN, HLD and UX: its own
    plan entry, criteria and test cases; the HLD's decisions with its repo's lines (and the ADRs
    the planner tagged it with, or those naming its repo); the UX spec's screens it builds, or no UX
    spec at all for a task without screens. What was left out is said at the end of each document,
    with where the full one is (`where`, the session's folder; the agent's tools otherwise)."""
    tid, acs = task.get("id", ""), list(task.get("acs") or [])
    repos, screens = _repo_names(task), list(task.get("screens") or [])
    words = (tid, *acs, *repos)
    out = {}
    for kind, text in parent_texts.items():
        if kind == "PLAN":
            filtered, left = _plan_for(text, task)
        elif kind == "HLD":
            adrs = set(task.get("adrs") or [])

            def adr_line(ln, adrs=adrs):
                ident = ln[2:].split(":", 1)[0].strip()
                return ident in adrs if adrs else _mentions(ln, repos)
            filtered, left = _by_section(text, {
                # A line naming no repo (a threat, an NFR) is about every task.
                **{n: (lambda ln: _mentions(ln, words) or "/" not in ln) for n in _PER_LINE},
                "ADRs": adr_line,
                "Data": True if task.get("role") in ("provider", "event-worker") else None,
            })
        elif kind == "UX":
            if not screens:
                out[kind] = (f"[The UX spec is left out: {tid} builds no screens. Read it from "
                             f"{where or 'the feature'} if the change affects what people see.]")
                continue
            filtered, left = _by_section(text, {n: (lambda ln: _mentions(ln, screens)) for n in _UX_LINES})
        else:
            out[kind] = text
            continue
        out[kind] = filtered + (f"\n\n[For {tid}, left out: {'; '.join(left)}. The full {kind} is in "
                                f"{where or 'the feature'}.]" if left else "")
    return out


def _plan_for(plan: str, task: dict) -> tuple[str, list[str]]:
    """The plan with only this task in Tasks (the others named in a line) and its own lines."""
    import json as _json

    from .parse import _JSON_BLOCK

    tid, acs = task.get("id", ""), list(task.get("acs") or [])
    m = _JSON_BLOCK.search(plan)
    others = []
    if m:
        try:
            tasks = _json.loads(m.group(1)).get("tasks", [])
        except _json.JSONDecodeError:
            tasks = []
        mine = [t for t in tasks if t.get("id") == tid]
        others = [f"{t.get('id')} ({t.get('repo')})" for t in tasks if t.get("id") != tid]
        if mine:
            block = "```json\n" + _json.dumps({"tasks": mine}, indent=2) + "\n```"
            if others:
                block += "\nOther tasks in this feature: " + ", ".join(others) + "."
            plan = plan[:m.start()] + block + plan[m.end():]
    text, left = _by_section(plan, {n: (lambda ln: _mentions(ln, (tid, *acs))) for n in _PLAN_LINES})
    return text, left

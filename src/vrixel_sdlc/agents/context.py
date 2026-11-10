"""Builds the six prompt layers for an agent call (see gateway/prompt.py)."""

from __future__ import annotations

import os
import re
from functools import lru_cache
from pathlib import Path

from ..config import ProductConfig
from ..constitution import Constitution
from ..gateway import PromptLayers, serialize_tools
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
    """Where `<plugin>:<skill>` lives under VRIXEL_SKILLS_DIR: the plugin's skills/<skill>/SKILL.md,
    or, for a command-style skill, its commands/<skill>.md. None when it is not installed."""
    plugin, _, skill = name.partition(":")
    base = base if base is not None else os.environ.get("VRIXEL_SKILLS_DIR")
    if not base or not plugin or not skill:
        return None
    for path in (Path(base) / plugin / "skills" / skill / "SKILL.md", Path(base) / plugin / "commands" / f"{skill}.md"):
        if path.is_file():
            return path
    return None


def missing_skills(names, base: str | None = None) -> list[str]:
    return sorted(n for n in names if skill_path(n, base) is None)


@lru_cache(maxsize=256)
def load_skill(name: str) -> str:
    """The skill's instructions from VRIXEL_SKILLS_DIR, or a stable placeholder (stub mode; live
    mode refuses to start while a skill is missing, see services.check_skills)."""
    path = skill_path(name)
    if path:
        return path.read_text(encoding="utf-8")
    plugin = name.partition(":")[0]
    return (
        f"# Skill {name}\n"
        f"Placeholder. Install the {plugin} plugin and set VRIXEL_SKILLS_DIR to load the real SKILL.md.\n"
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
) -> PromptLayers:
    tools = serialize_tools(_BASE_TOOLS + _ROLE_TOOLS.get(role, []))
    skill_text = "\n\n".join(load_skill(s) for s in sorted(skills))
    feature = "\n\n".join(f"<artifact kind=\"{k}\">\n{parent_texts[k]}\n</artifact>" for k in sorted(parent_texts))
    return PromptLayers(
        tools=tools,
        skills=f"{persona(role)}\n\n{skill_text}",
        constitution=constitution.text,
        product_context=product_pack(product, product_context, design_knowledge),
        feature_context=feature,
        tail=tail,
    )

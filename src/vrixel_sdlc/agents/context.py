"""Builds the six prompt layers for an agent call (see gateway/prompt.py)."""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from ..config import ProductConfig
from ..constitution import Constitution
from ..gateway import PromptLayers, serialize_tools

_BASE_TOOLS = [
    {"name": "read_artifact", "description": "Read an artifact by ID", "input_schema": {"type": "object", "properties": {"id": {"type": "string"}}}},
    {"name": "search_catalog", "description": "Search the capability catalog", "input_schema": {"type": "object", "properties": {"q": {"type": "string"}}}},
]
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


@lru_cache(maxsize=256)
def load_skill(name: str) -> str:
    """Load <VRIXEL_SKILLS_DIR>/<plugin>/skills/<skill>/SKILL.md, or a stable placeholder."""
    plugin, _, skill = name.partition(":")
    base = os.environ.get("VRIXEL_SKILLS_DIR")
    if base:
        path = Path(base) / plugin / "skills" / skill / "SKILL.md"
        if path.is_file():
            return path.read_text(encoding="utf-8")
    return (
        f"# Skill {name}\n"
        f"Placeholder. Install the {plugin} plugin and set VRIXEL_SKILLS_DIR to load the real SKILL.md.\n"
    )


def product_pack(product: ProductConfig, context: str = "") -> str:
    """L4: the product's own context.md from its governance repo, plus its registered lane."""
    repos = "\n".join(f"- {r.name} ({r.role})" for r in product.repos)
    return (
        f"# Product context: {product.name} ({product.product_id}, org {product.org})\n"
        f"Data class: {product.data_class}\n"
        f"Repositories:\n{repos}\n"
        f"High-risk paths: {', '.join(product.high_risk_paths)}\n"
        "Conventions: contracts first (expand, then contract); every acceptance criterion has a test; "
        "structured logs and traces in every service.\n"
        + (f"\n{context.strip()}\n" if context.strip() else "")
    )


def build_layers(
    *,
    role: str,
    skills: tuple[str, ...],
    constitution: Constitution,
    product: ProductConfig,
    parent_texts: dict[str, str],
    tail: str,
    product_context: str = "",
) -> PromptLayers:
    tools = serialize_tools(_BASE_TOOLS + _ROLE_TOOLS.get(role, []))
    skill_text = "\n\n".join(load_skill(s) for s in sorted(skills))
    feature = "\n\n".join(f"<artifact kind=\"{k}\">\n{parent_texts[k]}\n</artifact>" for k in sorted(parent_texts))
    return PromptLayers(
        tools=tools,
        skills=f"# Role: {role}\n\n{skill_text}",
        constitution=constitution.text,
        product_context=product_pack(product, product_context),
        feature_context=feature,
        tail=tail,
    )

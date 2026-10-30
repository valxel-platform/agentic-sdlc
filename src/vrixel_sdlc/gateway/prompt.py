"""Cache-first prompt layout: six layers, most stable first, breakpoints after L2, L4 and L5.

L1 tools · L2 skills · BP1 · L3 constitutions · L4 product context · BP2 · L5 feature context · BP3 · L6 tail
Nothing volatile (timestamps, request IDs) may appear in L1-L5, or every call rewrites the prefix.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field

LAYER_NAMES = ("L1", "L2", "L3", "L4", "L5")
BREAKPOINTS = ("L2", "L4", "L5")  # cache entries are written at the end of these layers

_VOLATILE = re.compile(r"\b\d{4}-\d{2}-\d{2}T\d{2}:\d{2}")


def estimate_tokens(text: str) -> int:
    """Rough count for stub mode and planning (about 4 characters per token)."""
    return max(1, len(text) // 4) if text else 0


@dataclass(frozen=True)
class PromptLayers:
    tools: str  # L1: tool definitions, serialized deterministically
    skills: str  # L2: plugin SKILL.md bodies for the role
    constitution: str  # L3: platform + product constitution
    product_context: str  # L4: product glossary, architecture summary, conventions
    feature_context: str  # L5: repo maps, approved PRD and ADRs for this feature
    tail: str  # L6: the task, history and tool results
    meta: dict = field(default_factory=dict)

    def layers(self) -> dict[str, str]:
        return {
            "L1": self.tools,
            "L2": self.skills,
            "L3": self.constitution,
            "L4": self.product_context,
            "L5": self.feature_context,
        }

    def prefix_hashes(self) -> dict[str, str]:
        """Chained hash per layer: a layer's hash covers it and every layer before it."""
        out, parent = {}, ""
        for name, text in self.layers().items():
            parent = hashlib.sha256((parent + "\x1f" + text).encode()).hexdigest()[:12]
            out[name] = parent
        return out

    def token_counts(self) -> dict[str, int]:
        counts = {k: estimate_tokens(v) for k, v in self.layers().items()}
        counts["L6"] = estimate_tokens(self.tail)
        return counts

    def volatile_layers(self) -> list[str]:
        """Layers that contain timestamp-like values; these break caching."""
        return [k for k, v in self.layers().items() if _VOLATILE.search(v)]


def serialize_tools(tools: list[dict]) -> str:
    """Deterministic serialization: sorted tools and keys produce byte-identical L1."""
    return json.dumps(sorted(tools, key=lambda t: t["name"]), sort_keys=True, separators=(",", ":"))

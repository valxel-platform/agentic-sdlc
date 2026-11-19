"""Loops that stop making progress (T-75).

Agents work in loops: a writer and its critic (document phases), the coder and the reviewer
(build), and build and CI (red CI goes back to the coder). Each loop has a cap (the product's
max_loops) and escalates when it's spent. But a loop can spend every try without getting anywhere,
and the escalation then only says "5 evaluator loops failed". This stops it at the first sign and
says which sign it was:

  repeating  the try left the same findings as the one before it: nothing was fixed
  reopened   a finding fixed on an earlier try is back: the fixes are undoing each other

Each stall says which agents were going around (`between`), from what the findings are about: the
coder and the reviewer (the review blocks), the coder and the tests, the writer and the critic, or
build and CI. Findings are compared by their words, not their exact text, since a critic rarely
words the same point the same way twice.

The stall goes into the escalation's reason and into the phase's event (`stall`), so the tool's
health metrics (T-76) can count stalls by pattern, phase and pair of agents.
"""

from __future__ import annotations

import re

SAME = 0.8  # share of words two findings have in common (Jaccard) to count as the same finding
_WORD = re.compile(r"[a-z][a-z0-9_]+")
_FILLER = frozenset(["the", "a", "an", "and", "or", "of", "to", "in", "on", "for", "is", "are", "be", "it", "its", "this", "that", "with", "as", "at", "by", "not", "no"])


def words(finding: str) -> frozenset[str]:
    # Digits go (line numbers and counts shift between tries), and so do filler words.
    return frozenset(w for w in _WORD.findall(str(finding).lower()) if w not in _FILLER)


def same(a: str, b: str) -> bool:
    wa, wb = words(a), words(b)
    if not wa or not wb:
        return str(a).strip().lower() == str(b).strip().lower()
    return len(wa & wb) / len(wa | wb) >= SAME


def _in(finding: str, others: list[str]) -> bool:
    return any(same(finding, o) for o in others)


def between(findings: list[str], default: str) -> str:
    """Which agents a stalled loop was between, from what its findings are about."""
    text = " ".join(findings).lower()
    if "the review blocks" in text:
        return "coder and reviewer"
    if "tests failing" in text or "test" in text and "not on the branch" in text:
        return "coder and tests"
    return default


def stall(history: list[list[str]], current: list[str], *, default: str = "writer and critic") -> dict | None:
    """Whether the try that left `current` made no progress on the tries before it (`history`,
    oldest first). None while it's still getting somewhere, or when it passed."""
    if not current or not history:
        return None
    prev = history[-1]
    if prev and all(_in(f, prev) for f in current) and all(_in(p, current) for p in prev):
        n = len(current)
        return {"pattern": "repeating", "findings": list(current), "between": between(current, default),
                "detail": f"the same {n} finding{'s' if n != 1 else ''} as the try before: nothing was fixed"}
    older = [f for h in history[:-1] for f in h]
    back = [f for f in current if not _in(f, prev) and _in(f, older)]
    if back:
        return {"pattern": "reopened", "findings": back, "between": between(back, default),
                "detail": f"{len(back)} finding{'s' if len(back) != 1 else ''} fixed on an earlier try came back: "
                          "the fixes are undoing each other"}
    return None


def reason(s: dict, tries: int) -> str:
    """The escalation's reason for a stall."""
    return (f"No progress after {tries} tries ({s['pattern']}, {s['between']}): {s['detail']}. "
            f"First: {s['findings'][0][:200]}")

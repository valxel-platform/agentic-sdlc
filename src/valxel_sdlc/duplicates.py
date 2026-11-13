"""Finding requests the product already has: features in flight, released or stopped, and live
capabilities. Used at intake (so a person is told before submitting), when a feature is created
(a likely duplicate needs a stated reason), and at G1 (the approver sees the matches).

Matching is deterministic and explainable: words of the title and description, lower-cased,
without stop words, reduced to a stem, compared by weighted overlap (title words count double).
It catches repeats and rewordings that share vocabulary; a request that describes the same thing
in entirely different words is left to the G1 approver, who sees the closest matches anyway.
"""

from __future__ import annotations

import re

DUPLICATE = 0.6  # this high: almost certainly the same request
RELATED = 0.3  # worth showing

_WORD = re.compile(r"[a-z0-9]+")
_STOP = frozenset("""a an and are as at be by can do for from has have how i in into is it its of on or our so
that the their them then there these this to us we what when where which who why will with without
need needs want wants should would could able new add allow allows let lets make support feature""".split())
_SUFFIXES = ("ations", "ation", "ings", "ing", "ions", "ion", "ers", "er", "ies", "es", "ed", "s")


def stem(word: str) -> str:
    for suf in _SUFFIXES:
        if word.endswith(suf) and len(word) - len(suf) >= 4:
            return word[: -len(suf)] + ("y" if suf == "ies" else "")
    return word


def terms(text: str) -> set[str]:
    return {stem(w) for w in _WORD.findall((text or "").lower()) if w not in _STOP and len(w) > 2}


def similarity(a_title: str, a_desc: str, b_title: str, b_desc: str) -> float:
    """Weighted overlap in [0, 1]: shared terms over the smaller request's terms, title terms x2."""
    at, bt = terms(a_title), terms(b_title)
    ad, bd = terms(a_desc) - at, terms(b_desc) - bt

    def weights(t: set[str], d: set[str]) -> dict[str, int]:
        return {**{w: 1 for w in d}, **{w: 2 for w in t}}

    wa, wb = weights(at, ad), weights(bt, bd)
    if not wa or not wb:
        return 0.0
    shared = sum(min(wa[w], wb[w]) for w in wa.keys() & wb.keys())
    return round(shared / min(sum(wa.values()), sum(wb.values())), 3)


def find_similar(title: str, description: str, features: list[dict], capabilities: list[dict],
                 exclude: str = "", limit: int = 5) -> list[dict]:
    """Matches at or above RELATED, best first. Each: {kind, id, title, status, score, duplicate}."""
    out = []
    for f in features:
        if f["feature_id"] == exclude:
            continue
        score = similarity(title, description, f.get("title", ""), f.get("description", ""))
        if score >= RELATED:
            out.append({"kind": "feature", "id": f["feature_id"], "title": f.get("title", ""),
                        "status": f.get("status", ""), "stage": f.get("stage", ""), "score": score,
                        "duplicate": score >= DUPLICATE and f.get("status") != "stopped"})
    released = {f["feature_id"] for f in features}
    for c in capabilities:
        if c.get("feature") in released or c.get("status") not in ("live", None):
            continue  # a feature's own capability is represented by the feature
        score = similarity(title, description, c.get("name", ""), "")
        if score >= RELATED:
            out.append({"kind": "capability", "id": c["id"], "title": c.get("name", ""), "status": "live",
                        "stage": "", "score": score, "duplicate": score >= DUPLICATE})
    return sorted(out, key=lambda m: -m["score"])[:limit]


def describe(m: dict) -> str:
    where = {"released": "already released", "in_progress": f"in progress{', at ' + m['stage'] if m.get('stage') else ''}",
             "stopped": "stopped earlier", "live": "live"}.get(m["status"], m["status"])
    return f"{m['id']} “{m['title']}” ({where})"


class DuplicateIntent(Exception):
    """A new feature looks like one the product already has; creating it needs a reason."""

    def __init__(self, matches: list[dict]) -> None:
        self.matches = matches
        super().__init__("This looks like " + "; ".join(describe(m) for m in matches if m["duplicate"])
                         + ". Add to that feature instead, or say why this one is different.")

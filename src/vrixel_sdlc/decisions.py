"""Decisions that take minutes: what a gate's approver reads first.

    summary    four or five lines: what it does, who for, what it moves, the main risk. Written once
               per document version by a small model (stub mode: taken from the document itself)
               and kept beside the version, so opening the gate twice costs nothing.
    changes    what changed since the version before (the one sent back), line by line, with the
               words people sent it back with
    comments   comments on specific lines go back with "Revise": each becomes a line of the
               feedback the agents get, quoting the line it's about
"""

from __future__ import annotations

import difflib
import json
import re
from pathlib import Path

from .gateway import CallTags, PromptLayers

ROLE = "knowledge"  # a small, quick model: summarising a document the person will read anyway
MAX_LINES = 5
CONTEXT = 1  # unchanged lines shown around each change


def _skim(text: str) -> list[str]:
    """A summary from the document itself: its first sentence, then the first point of each main section."""
    out: list[str] = []
    body = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    paras = [p.strip() for p in re.split(r"\n\s*\n", body) if p.strip() and not p.strip().startswith("#")]
    if paras:
        first = re.split(r"(?<=[.!?])\s", paras[0].replace("\n", " "), maxsplit=1)[0]
        out.append(first.lstrip("-* ").strip())
    for m in re.finditer(r"^##\s+(.+?)\s*$\n(.*?)(?=^##\s|\Z)", body, re.M | re.S):
        head, sec = m.group(1).strip(), m.group(2)
        if re.match(r"(agent notes|changes from review|monthly cost|sources)", head, re.I):
            continue
        items = [ln.strip()[2:].strip() for ln in sec.splitlines() if ln.strip().startswith(("- ", "* "))]
        if len(items) > 2:
            out.append(f"{head}: {len(items)} items, starting with “{items[0][:90]}”")
        elif items or sec.strip():
            line = (items[0] if items else sec.strip().splitlines()[0]).strip()
            out.append(f"{head}: {line[:140]}")
        if len(out) >= MAX_LINES:
            break
    return [o for o in out if o][:MAX_LINES]


def _path(ref: dict, suffix: str) -> Path:
    p = Path(ref["uri"])
    return p.with_name(p.name.replace(".md", f".{suffix}.json"))


def summary(services, ref: dict, *, product_id: str = "", data_class: str = "phi") -> dict:
    """{'lines': [...], 'by': 'agent' | 'document'}, cached beside the document version."""
    cache = _path(ref, "summary")
    if cache.exists():
        try:
            return json.loads(cache.read_text(encoding="utf-8"))
        except ValueError:
            pass
    text = services.artifacts.load(ref)
    out = {"lines": _skim(text), "by": "document"}
    if services.settings.mode == "live":
        try:
            tail = ("Summarise this document for the person who must approve it, in at most five short lines: what "
                    "it does, for whom, which goal it moves, and the one risk or open question that matters most. "
                    "Plain words, no headings, no marketing. Reply with JSON only: {\"lines\": [str]}.\n\n"
                    f"<document>\n{text[:30000]}\n</document>")
            layers = PromptLayers(tools="", skills="", constitution="", product_context="", feature_context="", tail=tail)
            tags = CallTags(product_id=product_id or "DECISIONS", feature_id=ref["id"].split(".")[0], phase="summary",
                            role=ROLE, data_class=data_class, workspace="wrkspc-decisions")
            res = services.gateway.complete(tags, layers, max_tokens=400)
            m = re.search(r"\{.*\}", res.text or "", re.S)
            lines = [str(x).strip() for x in (json.loads(m.group(0)).get("lines") or [])] if m else []
            if lines:
                out = {"lines": lines[:MAX_LINES], "by": "agent"}
        except Exception:  # a summary is a convenience: the document is right below it
            pass
    try:
        cache.write_text(json.dumps(out), encoding="utf-8")
    except OSError:
        pass
    return out


def previous(ref: dict) -> tuple[int, str] | None:
    """The version before this one, if there is one: (version, text)."""
    v = int(ref.get("version") or 0)
    if v < 2:
        return None
    p = Path(ref["uri"])
    prev = p.with_name(re.sub(r"\.v\d+\.md$", f".v{v - 1}.md", p.name))
    return (v - 1, prev.read_text(encoding="utf-8")) if prev.exists() else None


def changes(before: str, after: str) -> list[dict]:
    """The lines that changed, with a line of context around each: [{'op': ' '|'-'|'+'|'…', 'text'}]."""
    a, b = before.splitlines(), after.splitlines()
    out: list[dict] = []
    for group in difflib.SequenceMatcher(None, a, b, autojunk=False).get_grouped_opcodes(CONTEXT):
        if out:
            out.append({"op": "…", "text": ""})
        for tag, i1, i2, j1, j2 in group:
            if tag == "equal":
                out += [{"op": " ", "text": t} for t in a[i1:i2]]
            if tag in ("replace", "delete"):
                out += [{"op": "-", "text": t} for t in a[i1:i2]]
            if tag in ("replace", "insert"):
                out += [{"op": "+", "text": t} for t in b[j1:j2]]
    return out


def brief(services, feature_id: str, ref: dict, *, history: list[dict] | None = None, product_id: str = "",
          data_class: str = "phi") -> dict:
    """Everything the top of a gate shows: the summary, and what changed since the version sent back."""
    out = {"summary": summary(services, ref, product_id=product_id, data_class=data_class), "version": ref.get("version")}
    prev = previous(ref)
    if prev:
        sent_back = [h for h in history or [] if h.get("decision") in ("revise", "takeover", "handback")]
        out["changed"] = {"from_version": prev[0], "lines": changes(prev[1], services.artifacts.load(ref)),
                          "asked": (sent_back[-1].get("feedback") if sent_back else "") or ""}
    return out


def with_comments(feedback: str, comments: list[dict] | None) -> str:
    """The feedback the agents get: the person's words, then each comment quoting its line."""
    lines = []
    for c in comments or []:
        text = str(c.get("text") or "").strip()
        if not text:
            continue
        quote = str(c.get("quote") or "").strip()
        lines.append(f"- On “{quote[:200]}”: {text}" if quote else f"- {text}")
    if not lines:
        return feedback
    return (feedback.strip() + "\n\n" if feedback.strip() else "") + "Comments on specific lines:\n" + "\n".join(lines)


# ----------------------------------------------------------------------------- batch approval

# Gates a person may approve several of at once, when each is low risk. The initiative, release,
# production (G6) and product decisions are always made one at a time.
BATCH_GATES = ("g1", "g2", "g3", "g5")
MIN_SCORE = 0.8


def low_risk(item: dict, data_class: str) -> tuple[bool, str]:
    """Whether a waiting decision may be approved in a batch, and if not, why."""
    if item.get("type") != "gate" or item.get("gate") not in BATCH_GATES:
        return False, "decided one at a time"
    if data_class != "general":
        return False, f"{data_class} product"
    if item.get("risk_tier") not in (None, "", "low"):
        return False, f"{item['risk_tier']} risk"
    if item.get("findings"):
        return False, "the evaluators found something"
    if item.get("score") is not None and float(item["score"]) < MIN_SCORE:
        return False, "low evaluator score"
    if item.get("revisions") or item.get("stuck"):
        return False, "sent back before"
    if item.get("new_repos") or ((item.get("aws_resources") or {}).get("resources")):
        return False, "creates repositories or AWS services"
    if item.get("design_review") and (item["design_review"].get("conflicts") or item["design_review"].get("issues")):
        return False, "the design review raised something"
    return True, "critic passed, general data, low risk"

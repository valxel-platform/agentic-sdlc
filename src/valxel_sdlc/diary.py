"""What the agents assumed, changed, traded off and couldn't settle: their notes for the people at
the gate.

A reviewer reading a design can't see what the agent took as given, where it departed from the
brief, which options it weighed, or what it wasn't sure of. So every document ends with an
`## Agent notes` section, and the coder's reply carries the same notes for each pull request:

    - Assumed: <something not stated that the agent took as given>
    - Changed: <where it departed from the brief, the design or a rule, and why>
    - Traded off: <an option it weighed, and why it chose as it did>
    - Open: <a question for the people at the gate>

The tool reads them into the feature (`diaries`, per phase), shows them at the gate the phase goes
to (open questions first) and in the pull request description. A note is the agent's account,
not a check: the evaluators and reviewers still decide.
"""

from __future__ import annotations

import re

KINDS = {"assumed": "Assumed", "changed": "Changed", "traded off": "Traded off", "open": "Open"}
ORDER = ("open", "changed", "assumed", "traded off")
_LINE = re.compile(r"^\s*[-*]\s*(assumed|changed|traded off|open)\s*:\s*(.+?)\s*$", re.I)
_SECTION = re.compile(r"^##\s+Agent notes\s*$(.*?)(?=^##\s|\Z)", re.M | re.S)

CONTRACT = """## Agent notes                 (for the people at the gate; only what they couldn't see otherwise)
- Assumed: <something not stated that you took as given>
- Changed: <where you departed from the brief, the design or a rule, and why>
- Traded off: <an option you weighed, and why you chose as you did>
- Open: <a question the people at the gate should answer>"""

CODER = ("notes: what a reviewer should know that the diff doesn't show, each starting 'Assumed:', 'Changed:', "
         "'Traded off:' or 'Open:' (empty when there is nothing to say).")


def entries(lines: list[str]) -> list[dict]:
    out = []
    for line in lines:
        m = _LINE.match(line if line.lstrip().startswith(("-", "*")) else f"- {line}")
        if m and m.group(2).strip():
            out.append({"kind": m.group(1).lower(), "text": m.group(2).strip()})
    return sorted(out, key=lambda e: ORDER.index(e["kind"]))


def parse(document: str) -> list[dict]:
    """The notes in a document's `## Agent notes` section."""
    m = _SECTION.search(document or "")
    return entries(m.group(1).splitlines()) if m else []


def markdown(notes: list[dict]) -> str:
    if not notes:
        return ""
    return "## Agent notes\n" + "\n".join(f"- {KINDS[n['kind']]}: {n['text']}" for n in notes) + "\n"


def summary(notes: list[dict]) -> str:
    counts = {k: sum(1 for n in notes if n["kind"] == k) for k in ORDER}
    return ", ".join(f"{v} {KINDS[k].lower()}" for k, v in counts.items() if v) or "none"

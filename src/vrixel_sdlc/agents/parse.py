"""Parsers for the structured sections agents write; shared by templates and evaluators."""

from __future__ import annotations

import json
import re

from ..repo_roles import ROLE_PATTERN

_AC = re.compile(r"^- (AC-\d+):\s*(.+)$", re.M)
_REPO = re.compile(r"^- ([A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9._-]+) \((" + ROLE_PATTERN + r")\)\s*$", re.M)
_TRACE = re.compile(r"^- (AC-\d+) -> (TASK-\d+) / (TEST-\d+)\s*$", re.M)
_JSON_BLOCK = re.compile(r"```json\n(.*?)\n```", re.S)


def section(text: str, heading: str) -> str:
    """Body of a '## heading' section, up to the next '## ' heading."""
    m = re.search(rf"^## {re.escape(heading)}\s*$(.*?)(?=^## |\Z)", text, re.M | re.S)
    return m.group(1).strip() if m else ""


def bullets(text: str) -> list[str]:
    return [ln[2:].strip() for ln in text.splitlines() if ln.startswith("- ")]


def parse_acs(prd: str) -> dict[str, str]:
    return dict(_AC.findall(section(prd, "Acceptance criteria")))


def is_testable(ac: str) -> bool:
    low = ac.lower()
    return low.startswith("given ") and " when " in low and " then " in low


def parse_affected_repos(hld: str) -> list[dict]:
    return [{"name": n, "role": r} for n, r in _REPO.findall(section(hld, "Affected repos"))]


def parse_trace(plan: str) -> list[tuple[str, str, str]]:
    return _TRACE.findall(section(plan, "Traceability"))


def parse_tasks(plan: str) -> list[dict]:
    m = _JSON_BLOCK.search(plan)
    if not m:
        return []
    try:
        data = json.loads(m.group(1))
    except json.JSONDecodeError:
        return []
    return list(data.get("tasks", []))


_NEW_REPO = re.compile(r"^- ([A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9._-]+) \((" + ROLE_PATTERN + r"|[a-z][a-z-]*)\):\s*(.*?)"
                       r"(?:\s*\[(ADR-[^\]]+)\])?\s*$", re.M)


def parse_new_repos(hld: str) -> list[dict]:
    """`## New repos` lines: `- org/name (role): purpose [ADR-id]`. The ADR must exist in the
    HLD's ADRs section and name the repo, or the proposal carries no ADR."""
    adrs = {ln.split(":", 1)[0].strip(): ln for ln in bullets(section(hld, "ADRs"))}
    out = []
    for name, role, purpose, adr in _NEW_REPO.findall(section(hld, "New repos")):
        ok = adr if adr and adr in adrs and name in adrs[adr] else ""
        out.append({"name": name, "role": role, "purpose": purpose.strip(), "adr": ok})
    return out

"""Parsers for the structured sections agents write; shared by templates and evaluators."""

from __future__ import annotations

import json
import re

from ..repo_roles import ROLE_PATTERN

# '- AC-1 (G1): Given ...' (the goal it serves, in brackets, is optional for older documents)
_AC = re.compile(r"^- (AC-\d+)(?:\s*\(([^)]*)\))?:\s*(.+)$", re.M)
# '- NFR-1 (performance, G1): <measurable target>'
_NFR = re.compile(r"^- (NFR-\d+)\s*\(\s*([a-z][a-z -]*?)\s*(?:,\s*(G\d+)\s*)?\):\s*(.+)$", re.M)
NFR_KINDS = ("performance", "availability", "reliability", "scalability", "security", "privacy",
             "accessibility", "compliance", "usability", "maintainability", "observability", "other")
# '- TC-1 (AC-1, error): <what it checks> -> TASK-1 tests/test_x.py::test_name'
_CASE = re.compile(r"^- (TC-\d+)\s*\(\s*((?:AC|NFR)-\d+)\s*,\s*([a-z][a-z -]*?)\s*\):\s*(.+?)\s*->\s*(.+?)\s*$", re.M)
CASE_KINDS = ("happy", "error", "edge", "permission", "dependency", "data", *NFR_KINDS)
_DEFER = re.compile(r"^- ((?:AC|NFR)-\d+)\s*->\s*deferred\s*:\s*(.*?)\s*$", re.M | re.I)
_REPO = re.compile(r"^- ([A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9._-]+) \((" + ROLE_PATTERN + r")\)\s*$", re.M)
_TRACE = re.compile(r"^- ((?:AC|NFR)-\d+) -> (TASK-\d+)(?: / (\S+))?\s*$", re.M)
_JSON_BLOCK = re.compile(r"```json\n(.*?)\n```", re.S)


def section(text: str, heading: str) -> str:
    """Body of a '## heading' section, up to the next '## ' heading."""
    m = re.search(rf"^## {re.escape(heading)}\s*$(.*?)(?=^## |\Z)", text, re.M | re.S)
    return m.group(1).strip() if m else ""


def split_sections(text: str) -> tuple[str, list[tuple[str, str]]]:
    """The text before the first '## ' heading, and each section as (heading, full text)."""
    parts = re.split(r"(?m)^(?=## )", text or "")
    head = parts[0] if parts and not parts[0].startswith("## ") else ""
    return head, [(p.splitlines()[0][3:].strip(), p) for p in parts if p.startswith("## ")]


def merge_sections(previous: str, revision: str) -> str:
    """The previous document with the revision's sections in place: a section with the same
    heading is replaced, a new one is added at the end, and the rest is kept as it was. A revision
    that starts with a '# ' title replaces the text before the first section too."""
    head, old = split_sections(previous)
    new_head, new = split_sections(revision)
    if not new:  # not sections at all: the whole document again
        return revision
    replaced = dict(new)
    out = [new_head if new_head.lstrip().startswith("# ") else head]
    out += [replaced.pop(h, body) for h, body in old]
    out += [body for h, body in new if h in replaced]
    return "\n\n".join(p.strip("\n") for p in out if p.strip()) + "\n"


def bullets(text: str) -> list[str]:
    return [ln[2:].strip() for ln in text.splitlines() if ln.startswith("- ")]


def parse_acs(prd: str) -> dict[str, str]:
    return {ac: text for ac, _, text in _AC.findall(section(prd, "Acceptance criteria"))}


def parse_ac_goals(prd: str) -> dict[str, list[str]]:
    """Each criterion's goals, from '- AC-1 (G1): ...' ('(G1, G2)' for several)."""
    return {ac: re.findall(r"\bG\d+\b", goals or "") for ac, goals, _ in _AC.findall(section(prd, "Acceptance criteria"))}


def parse_nfrs(prd: str) -> dict[str, dict]:
    """'## Non-functional requirements' lines: id -> {kind, goal, text}."""
    return {n: {"kind": kind.strip(), "goal": goal or "", "text": text.strip()}
            for n, kind, goal, text in _NFR.findall(section(prd, "Non-functional requirements"))}


def parse_cases(plan: str) -> list[dict]:
    """'## Test cases' lines. The test reference after '->' is one of:
    'TASK-1 <path>::<test name>' (a test in that task's repo), 'acceptance <file> "<title>"' (the
    product's acceptance repo), 'suite <name>' (a quality suite: performance, perf-smoke, sast, ...),
    or 'manual: <why>' (a check people do; it needs an override at G6)."""
    out = []
    for cid, req, kind, what, ref in _CASE.findall(section(plan, "Test cases")):
        case = {"id": cid, "req": req, "kind": kind.strip(), "what": what, "ref": ref, "via": "unknown"}
        if m := re.match(r"^(TASK-\d+)\s+(\S+?)::(\S+)$", ref):
            case.update(via="test", task=m.group(1), file=m.group(2), test=m.group(3))
        elif m := re.match(r'^acceptance\s+(\S+)(?:\s+"([^"]+)")?$', ref):
            case.update(via="acceptance", file=m.group(1), test=m.group(2) or "")
        elif m := re.match(r"^suite\s+([\w./-]+)$", ref):
            case.update(via="suite", suite=m.group(1))
        elif m := re.match(r"^manual\s*:\s*(.+)$", ref):
            case.update(via="manual", why=m.group(1))
        out.append(case)
    return out


def parse_deferred(plan: str) -> dict[str, str]:
    """Requirements the plan deliberately leaves out: id -> reason ('' when none was given)."""
    return dict(_DEFER.findall(section(plan, "Traceability")))


def is_testable(ac: str) -> bool:
    low = ac.lower()
    return low.startswith("given ") and " when " in low and " then " in low


def parse_affected_repos(hld: str) -> list[dict]:
    return [{"name": n, "role": r} for n, r in _REPO.findall(section(hld, "Affected repos"))]


_AUDIT = re.compile(r"^- ([a-z][a-z0-9_]*(?:\.[a-z0-9_]+)+)\s*:\s*(.+?)\s*$", re.M)


def parse_audit(hld: str) -> list[dict]:
    """'## Audit' bullets: '- patient.read: patient (who opened which chart)' -> action and record type."""
    return [{"action": a, "record": r.split("(", 1)[0].strip()} for a, r in _AUDIT.findall(section(hld, "Audit"))]


_ENDPOINT = re.compile(r"^- ([\w.-]+/[\w.-]+)\s*:\s*([a-z, ]+?)\s*(?:;\s*calls\s+(.+?))?\s*$", re.M)
VISIBILITIES = ("none", "project", "namespace", "internal", "external")


def parse_endpoints(hld: str) -> list[dict]:
    """'## Endpoints' bullets: '- acme-clinic/cln-bff: external; calls cln-intake-api, cln-web' ->
    who may reach each component (OpenChoreo endpoint visibility) and what it calls (VX-17)."""
    out = []
    for repo, vis, calls in _ENDPOINT.findall(section(hld, "Endpoints")):
        out.append({"repo": repo, "visibility": [v.strip() for v in vis.split(",") if v.strip()],
                    "calls": [c.strip() for c in (calls or "").split(",") if c.strip()]})
    return out


_EVENT = re.compile(r"^- (vrixel\.[a-z0-9_-]+\.[a-z0-9_-]+\.v\d+)\s*:\s*(.+?)\s*$", re.M)


def parse_events(hld: str) -> list[dict]:
    """'## Events' bullets: '- vrixel.consent.recorded.v1: published by cln-intake-api, applied by
    cln-consent-worker' -> event type and description (VX-16)."""
    return [{"type": t, "about": a} for t, a in _EVENT.findall(section(hld, "Events"))]


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

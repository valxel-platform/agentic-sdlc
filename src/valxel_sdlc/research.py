"""Research before a new product or initiative: who has the problem, how they solve it today, what
the market says, and what the plan is betting on, each claim with a source and a confidence.

    research note (this module)  ->  the brief (inception) or the initiative draft cites it:
        ## Evidence      - <claim> (E1, E3)              every claim names the research it rests on
        ## Assumptions   - A1: We believe ...; test: ...  what the plan bets on, and how we'd know
    the brief or G0 gate shows the note; the initiative's review lists its assumptions

Depth, chosen by the person who starts it:

    quick      a few web searches (default)
    thorough   more searches, more alternatives, pricing where it's public
    own        no searching: the person's own research (in the request and attachments) put into
               the same form

Web search runs where the runtime has it (Claude Code, or the Claude API); elsewhere, and in stub
mode, the note says so and every claim not from the person is low confidence and unsourced.
Searches send text to a search engine, which no BAA covers: for PHI products (and for inception,
which runs as PHI until the brief gate approves a data class) the attachments stay out of a
research call that searches; the brief's and initiative's agents still read them.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field

from .agents import untrusted
from .agents.parse import section

DEPTHS = ("quick", "thorough", "own")
SEARCHES = {"quick": 5, "thorough": 15, "own": 0}
MIN_SOURCES = {"quick": 2, "thorough": 5, "own": 0}
MIN_EVIDENCE = 3
SKILLS = ("design:research-synthesis", "product-management:competitive-brief")
CONFIDENCE = ("high", "medium", "low")
PERSON = ("the request", "an attachment")  # sources the person gave

FORMAT = """# Research: <subject>
## Who has the problem
<who, how often, and what it costs them today>
## How they solve it today
- <alternative: a product, a workaround, doing nothing>: <what it does>; <price, or 'price not public'> (S1)
## Market signals
- <a trend, a regulation, a number about the market> (S2)
## Evidence
- E1: <one claim> (confidence: high | medium | low; source: S1 | the request | an attachment | unsourced)
## Assumptions
- A1: We believe <what the plan bets on>; test: <how we'd know: a number, an event, a conversation, and by when>
## Open questions
- <what the research couldn't answer>
## Sources
- S1: [<title>](<https URL>)                      or '- None: <why>' when nothing was searched"""

_EVIDENCE = re.compile(r"^(E\d+):\s*(.+?)\s*\(confidence:\s*([a-z]+)\s*;\s*source:\s*(.+?)\)\s*$", re.I)
_ASSUMPTION = re.compile(r"^(A\d+):\s*(.+?);\s*test:\s*(.+)$", re.I)
_SOURCE = re.compile(r"^(S\d+):\s*(?:\[(.+?)\]\((https?://\S+?)\)|(https?://\S+))\s*(.*)$")
_EIDS = re.compile(r"\bE\d+\b")


@dataclass
class Research:
    text: str
    status: str  # passed | escalated
    attempts: int
    depth: str
    web: bool  # whether the agent could search
    findings: list[str] = field(default_factory=list)
    cost_usd: float = 0.0
    attachments_withheld: bool = False

    def as_dict(self) -> dict:
        return asdict(self)


def _bullets(text: str, heading: str) -> list[str]:
    return [ln.strip()[2:].strip() for ln in section(text, heading).splitlines() if ln.strip().startswith("- ")]


def evidence(text: str) -> list[dict]:
    out = []
    for line in _bullets(text, "Evidence"):
        m = _EVIDENCE.match(line)
        out.append({"line": line, "id": m.group(1).upper() if m else "", "claim": m.group(2) if m else line,
                    "confidence": m.group(3).lower() if m else "", "source": m.group(4).strip() if m else ""})
    return out


def assumptions(text: str) -> list[dict]:
    out = []
    for line in _bullets(text, "Assumptions"):
        m = _ASSUMPTION.match(line)
        out.append({"line": line, "id": m.group(1).upper() if m else "", "belief": m.group(2).strip() if m else line,
                    "test": m.group(3).strip() if m else ""})
    return out


def sources(text: str) -> dict[str, dict]:
    out = {}
    for line in _bullets(text, "Sources"):
        m = _SOURCE.match(line)
        if m:
            out[m.group(1).upper()] = {"title": (m.group(2) or m.group(4) or "").strip(), "url": m.group(3) or m.group(4)}
    return out


def check(text: str, *, depth: str, web: bool) -> list[str]:
    """What's wrong with a research note, for the agent to fix."""
    out = [f"Missing the '## {h}' section" for h in ("Who has the problem", "How they solve it today", "Evidence",
                                                      "Assumptions", "Sources") if not section(text, h)]
    srcs = sources(text)
    ev = evidence(text)
    for e in ev:
        if not e["id"]:
            out.append(f"Evidence: '{e['line'][:80]}' isn't in the form '- E1: <claim> (confidence: high; source: S1)'")
            continue
        if e["confidence"] not in CONFIDENCE:
            out.append(f"{e['id']}: confidence must be high, medium or low")
        cited = re.findall(r"\bS\d+\b", e["source"], re.I)
        if cited:
            if not web:
                out.append(f"{e['id']}: nothing was searched, so it can't cite {cited[0]}: cite the request, an "
                           "attachment, or write 'unsourced' with low confidence")
            for s in cited:
                if s.upper() not in srcs:
                    out.append(f"{e['id']}: {s} isn't under Sources as '- {s}: [title](https://...)'")
        elif e["source"].lower().startswith("unsourced"):
            if e["confidence"] != "low":
                out.append(f"{e['id']}: an unsourced claim can only be low confidence")
        elif not e["source"].lower().startswith(PERSON):
            out.append(f"{e['id']}: source must be S<n>, 'the request', 'an attachment' or 'unsourced'")
    if section(text, "Evidence") and len([e for e in ev if e["id"]]) < MIN_EVIDENCE:
        out.append(f"Evidence: at least {MIN_EVIDENCE} claims, each with its confidence and source")
    asm = assumptions(text)
    for a in asm:
        if not a["id"] or not a["test"]:
            out.append(f"Assumptions: '{a['line'][:80]}' isn't in the form '- A1: We believe ...; test: ...'")
    if section(text, "Assumptions") and not asm:
        out.append("Assumptions: at least one, in the form '- A1: We believe ...; test: ...'")
    if web and len(srcs) < MIN_SOURCES.get(depth, 0):
        out.append(f"Sources: a {depth} search finds at least {MIN_SOURCES[depth]} sources; list each as "
                   "'- S1: [title](https://...)'")
    return out


def citations(doc_text: str, research_text: str) -> list[str]:
    """Problems with a brief's or initiative's Evidence and Assumptions against its research note."""
    out = []
    known = {e["id"] for e in evidence(research_text) if e["id"]}
    lines = _bullets(doc_text, "Evidence")
    if not lines:
        out.append("Evidence: list the claims the plan rests on, each citing the research note: "
                   "'- <claim> (E1, E3)'")
    for line in lines:
        ids = set(_EIDS.findall(line))
        if not ids:
            out.append(f"Evidence: '{line[:80]}' cites no research (E1, E2, ...); move it to Assumptions if "
                       "nothing backs it")
        for i in sorted(ids - known):
            out.append(f"Evidence: {i} isn't in the research note")
    asm = assumptions(doc_text)
    if not asm:
        out.append("Assumptions: list what the plan bets on as '- A1: We believe ...; test: <how we'd know>'")
    for a in asm:
        if not a["id"] or not a["test"]:
            out.append(f"Assumptions: '{a['line'][:80]}' isn't in the form '- A1: We believe ...; test: ...'")
    return out


DOC_SECTIONS = ("## Evidence (each line a claim the plan rests on, citing the research note: '- <claim> (E1, E3)'; "
                "anything nothing backs goes under Assumptions), ## Assumptions ('- A1: We believe <...>; test: "
                "<how we'd know: a number, an event or a goal, and by when>'; carry over the research note's and "
                "add the plan's own)")


# ----------------------------------------------------------------------------------- the call

def can_search(services, role: str) -> bool:
    """Whether the role's runtime can search the web here."""
    settings = services.settings
    if getattr(settings, "mode", "") == "stub":
        return False
    runtime = services.runtime_for(role)
    name = getattr(runtime, "name", "")
    if name == "claude_code":
        return True
    if name == "generic":
        return settings.role(role).provider == "anthropic"
    return False


def stub_note(subject: str, request: str, depth: str) -> str:
    first = re.split(r"(?<=[.!?])\s+|\n", request.strip(), maxsplit=1)[0].strip() or subject
    return "\n".join([
        f"# Research: {subject}", "",
        "## Who has the problem", first, "",
        "## How they solve it today",
        "- Doing it by hand or with general tools: spreadsheets, phone calls, paper; price not public", "",
        "## Market signals", "- None found: nothing was searched", "",
        "## Evidence",
        f"- E1: {first.rstrip('.')} (confidence: medium; source: the request)",
        "- E2: People manage this today by hand or with general-purpose tools (confidence: low; source: unsourced)",
        "- E3: No competitor's pricing was checked (confidence: low; source: unsourced)", "",
        "## Assumptions",
        f"- A1: We believe {subject} is worth building for the people the request names; test: the goal's "
        "number moves toward its target within a quarter of release",
        "- A2: We believe they'll switch from what they do today; test: most of those offered it use it twice "
        "in their first month", "",
        "## Open questions", "- How many people have the problem, and what it costs them", "",
        "## Sources", f"- None: web search wasn't available here ({depth} research, stub mode)",
    ]) + "\n"


def run(services, *, role: str, subject: str, request: str, depth: str, data_class: str, tags, layers_for,
        attachments_text: str = "", existing: str = "", max_loops: int | None = None) -> Research:
    """Write the research note, checked by machine and revised until it passes or the loops run out.

    `layers_for(skills, tail)` builds the prompt layers the caller's documents use; `tags` are the
    call's CallTags (phase 'research')."""
    from .runtime import AgentTask

    depth = depth if depth in DEPTHS else "quick"
    web = depth != "own" and can_search(services, role)
    withheld = bool(attachments_text) and web and data_class in ("phi", "")
    parts = [f"Research for: {subject}.",
             "The person's request:\n" + untrusted.wrap("request", request)]
    if attachments_text and not withheld:
        parts.append(attachments_text)
    if withheld:
        parts.append("The person attached files; they stay out of this call because it searches the web. Don't "
                     "search for anything that could identify a patient or a person.")
    if existing:
        parts.append(existing)
    if depth == "own":
        parts.append("Don't search. Put the person's own research, from the request and any attachments, into the "
                     "form below; claims they didn't make are unsourced and low confidence.")
    elif web:
        scope = ("up to 15 searches: cover at least five alternatives with their public pricing, and the size of "
                 "the market" if depth == "thorough" else
                 "up to 5 searches: the main alternatives and one or two market signals")
        parts.append(f"Search the web ({scope}). Cite only pages you read; never invent a URL. Search for the "
                     "problem and the market, never for a person.")
    else:
        parts.append("You can't search the web here. Don't cite web pages: cite the request or an attachment, or mark "
                     "a claim unsourced with low confidence, and write '- None: no web search here' under Sources.")
    parts.append("Prefer evidence that the problem is real and costly over evidence that a solution is possible. "
                 "Say plainly when something couldn't be found.")
    parts.append("Write Markdown in exactly this form; checks read the bullet lines by machine:\n" + FORMAT)
    loops = max_loops or services.settings.limits.max_loops
    text, cost, findings = "", 0.0, []
    for attempt in range(1, loops + 1):
        tail = "\n\n".join(parts + (["Fix these findings:\n" + "\n".join(f"- {f}" for f in findings),
                                     "Your previous note, to revise:\n" + untrusted.wrap("previous-research", text)]
                                    if findings else []))
        res = services.runtime_for(role).run(AgentTask(
            role=role, phase="research", tags=tags, layers=layers_for(SKILLS, tail), skills=SKILLS,
            stub_text=stub_note(subject, request, depth), max_tokens=8000 if depth == "thorough" else 5000,
            web_searches=SEARCHES[depth] if web else 0))
        text, cost = res.text, cost + float(res.cost_usd or 0.0)
        findings = check(text, depth=depth, web=web)
        if not findings:
            return Research(text, "passed", attempt, depth, web, [], round(cost, 4), withheld)
    return Research(text, "escalated", loops, depth, web, findings, round(cost, 4), withheld)


def summary(r: dict | None) -> str:
    """One line for a gate: how the research was done."""
    if not r:
        return ""
    how = {"own": "from your own research", "quick": "a quick web search", "thorough": "a thorough web search"}
    line = how.get(r.get("depth", ""), "research")
    if r.get("depth") != "own" and not r.get("web"):
        line = "no web search available here: claims not from you are unsourced"
    if r.get("attachments_withheld"):
        line += "; attachments weren't sent to the search"
    if r.get("status") == "escalated":
        line += f"; still has problems: {'; '.join(r.get('findings') or [])[:300]}"
    return line

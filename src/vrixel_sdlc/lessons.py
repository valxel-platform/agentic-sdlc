"""Learning from people's corrections, so the same correction isn't needed twice.

Every time a person sends work back (a gate's revise, an escalation's retry), takes a phase over
and writes the document themselves, or resolves an escalation with their own version, the tool
records it as a correction. A correction becomes a proposed lesson when:
- the person ticks "remember this for future features" (optionally writing the lesson in a line);
- a person took a phase over and changed the agents' draft (what they changed is the evidence);
- the same phase is sent back for the same reason on several features (REPEAT_FEATURES).

A proposed lesson is distilled into one instruction an agent can follow (an LLM in live mode, a
deterministic rule in stub mode) and proposed as a pull request to the product's governance repo,
one file per lesson under lessons/. People review the wording and merge it, or close it. Merged
lessons are part of the product's governance, so a feature pins them at intake like its
constitution: they go into that phase's agent prompts, and a lesson marked `check: true` becomes
a must-have item on the critic's checklist for that phase.

    # lessons/L-004.yaml
    id: L-004
    phase: spec
    text: Every acceptance criterion names the user role it applies to.
    check: true
    sources: [FARM-FEAT-12 G2 revise by alice (missing_requirement)]
"""

from __future__ import annotations

import difflib
import json
import re
import threading
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone

import yaml

LESSONS_DIR = "lessons"
# The same phase sent back for the same reason on this many features proposes a lesson.
REPEAT_FEATURES = 3
_ID = re.compile(r"^L-(\d+)$")
REASON_LABELS = {
    "missing_requirement": "something required was missing", "wrong_scope": "the scope was wrong",
    "wrong_approach": "the approach was wrong", "unclear": "it was unclear or ambiguous",
    "quality": "the quality wasn't good enough", "format": "the format or structure was wrong",
    "compliance": "compliance or security",
}


class LessonError(ValueError):
    pass


@dataclass(frozen=True)
class Lesson:
    id: str
    phase: str
    text: str
    check: bool = False  # also a must-have item on the critic's checklist for the phase
    sources: tuple[str, ...] = ()

    def as_dict(self) -> dict:
        return {"id": self.id, "phase": self.phase, "text": self.text, "check": self.check,
                "sources": list(self.sources)}


def parse(text: str, path: str = "") -> Lesson:
    try:
        raw = yaml.safe_load(text or "") or {}
    except yaml.YAMLError as exc:
        raise LessonError(f"{path}: not valid YAML ({exc})") from exc
    if not isinstance(raw, dict) or not raw.get("id") or not raw.get("phase") or not str(raw.get("text", "")).strip():
        raise LessonError(f"{path}: a lesson needs id, phase and text")
    return Lesson(id=str(raw["id"]), phase=str(raw["phase"]), text=" ".join(str(raw["text"]).split()),
                  check=bool(raw.get("check", False)), sources=tuple(str(s) for s in raw.get("sources") or []))


def render(lesson: Lesson) -> str:
    return yaml.safe_dump(lesson.as_dict(), sort_keys=False, allow_unicode=True, width=100)


def load_dir(source, org: str, repo: str, sha: str, directory: str = LESSONS_DIR) -> tuple[Lesson, ...]:
    """The approved lessons in a governance repo at a commit (none when it has no lessons/ folder)."""
    try:
        paths = [p for p in source.list(org, repo, directory, sha) if p.endswith((".yaml", ".yml"))]
    except Exception:  # no folder yet
        return ()
    return tuple(parse(source.read(org, repo, p, sha) or "", p) for p in paths)


def for_phase(lessons, phase: str) -> tuple[Lesson, ...]:
    return tuple(lesson for lesson in lessons or () if lesson.phase == phase)


def instruction(lessons, phase: str) -> str:
    mine = for_phase(lessons, phase)
    if not mine:
        return ""
    return ("Lessons from earlier features of this product (people approved these; follow them):\n"
            + "\n".join(f"- {lesson.id}: {lesson.text}" for lesson in mine))


def checklist(lessons, phase: str):
    from .evaluators.rubrics import Item

    return tuple(Item(f"lesson-{lesson.id}", lesson.text, must=True) for lesson in for_phase(lessons, phase)
                 if lesson.check)


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


# Stores: corrections (every person's correction, across features) and proposals (lessons proposed).

class InMemoryLessonStore:
    def __init__(self) -> None:
        self._corrections: list[dict] = []
        self._proposals: list[dict] = []
        self._lock = threading.Lock()

    def add_correction(self, c: dict) -> None:
        with self._lock:
            self._corrections.append({"at": now(), **c})

    def corrections(self, product_id: str) -> list[dict]:
        with self._lock:
            return [dict(c) for c in self._corrections if c.get("product_id") == product_id]

    def add_proposal(self, p: dict) -> None:
        with self._lock:
            self._proposals.append({"at": now(), **p})

    def proposals(self, product_id: str) -> list[dict]:
        with self._lock:
            return [dict(p) for p in self._proposals if p.get("product_id") == product_id]


class PostgresLessonStore:
    DDL = (
        """CREATE TABLE IF NOT EXISTS vrixel_corrections (
             id BIGSERIAL PRIMARY KEY, product_id TEXT NOT NULL, feature_id TEXT NOT NULL,
             body JSONB NOT NULL, at TIMESTAMPTZ NOT NULL DEFAULT now())""",
        "CREATE INDEX IF NOT EXISTS vrixel_corrections_product ON vrixel_corrections (product_id)",
        """CREATE TABLE IF NOT EXISTS vrixel_lesson_proposals (
             product_id TEXT NOT NULL, lesson_id TEXT NOT NULL, body JSONB NOT NULL,
             at TIMESTAMPTZ NOT NULL DEFAULT now(), PRIMARY KEY (product_id, lesson_id))""",
    )

    def __init__(self, pool) -> None:
        self.pool = pool
        with pool.connection() as conn:
            for stmt in self.DDL:
                conn.execute(stmt)

    def add_correction(self, c: dict) -> None:
        with self.pool.connection() as conn:
            conn.execute("INSERT INTO vrixel_corrections (product_id, feature_id, body) VALUES (%s, %s, %s::jsonb)",
                         (c["product_id"], c.get("feature_id", ""), json.dumps(c)))

    def corrections(self, product_id: str) -> list[dict]:
        with self.pool.connection() as conn:
            rows = conn.execute("SELECT body, at FROM vrixel_corrections WHERE product_id = %s ORDER BY id",
                                (product_id,)).fetchall()
        return [{**r["body"], "at": r["at"].isoformat()} for r in rows]

    def add_proposal(self, p: dict) -> None:
        with self.pool.connection() as conn:
            conn.execute("INSERT INTO vrixel_lesson_proposals (product_id, lesson_id, body) VALUES (%s, %s, %s::jsonb) "
                         "ON CONFLICT DO NOTHING", (p["product_id"], p["id"], json.dumps(p)))

    def proposals(self, product_id: str) -> list[dict]:
        with self.pool.connection() as conn:
            rows = conn.execute("SELECT body, at FROM vrixel_lesson_proposals WHERE product_id = %s ORDER BY at",
                                (product_id,)).fetchall()
        return [{**r["body"], "at": r["at"].isoformat()} for r in rows]


# What changed when a person took over.

def changes(before: str, after: str) -> dict:
    """The sections a person added, changed or removed in the agents' draft, and a short diff."""
    from .agents.parse import split_sections

    a, b = dict(split_sections(before or "")[1]), dict(split_sections(after or "")[1])
    out = {"added": [h for h in b if h not in a], "removed": [h for h in a if h not in b],
           "changed": [h for h in b if h in a and a[h].strip() != b[h].strip()]}
    diff = "\n".join(difflib.unified_diff((before or "").splitlines(), (after or "").splitlines(),
                                          "agents", "person", lineterm="", n=1))
    out["diff"] = diff[:4000]
    out["any"] = bool(out["added"] or out["removed"] or out["changed"] or diff)
    return out


# Which corrections become lessons.

@dataclass
class Candidate:
    phase: str
    why: str  # remember | takeover | repeat
    key: str  # one proposal per key
    corrections: list[dict] = field(default_factory=list)
    reason: str = ""


def candidates(store, product_id: str, new: list[dict]) -> list[Candidate]:
    out: list[Candidate] = []
    proposed = {p.get("key") for p in store.proposals(product_id)}
    for c in new:
        ref = f"{c['feature_id']}:{c.get('gate') or c['phase']}:{c.get('n', 0)}"
        if c.get("remember"):
            out.append(Candidate(c["phase"], "remember", f"remember:{ref}", [c], c.get("reason", "")))
        elif c.get("kind") in ("takeover", "resolved") and (c.get("changes") or {}).get("any"):
            out.append(Candidate(c["phase"], "takeover", f"takeover:{ref}", [c], c.get("reason", "")))
    groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for c in store.corrections(product_id):
        if c.get("reason") and c["reason"] != "other" and c.get("kind") in ("revise", "retry", "takeover"):
            groups[(c["phase"], c["reason"])].append(c)
    touched = {(c["phase"], c.get("reason", "")) for c in new}
    for (phase, reason), cs in groups.items():
        key = f"repeat:{phase}:{reason}"
        if (phase, reason) in touched and key not in proposed and len({c["feature_id"] for c in cs}) >= REPEAT_FEATURES:
            out.append(Candidate(phase, "repeat", key, cs[-5:], reason))
    return [c for c in out if c.key not in proposed]


def _reply_model():
    from pydantic import BaseModel, Field

    class Reply(BaseModel):
        # One instruction an agent can follow in future features, in the product's terms; no names
        # of people or of this feature.
        text: str = Field(min_length=8, max_length=400)
        # Whether a reader can check it in the finished document (it then joins the critic's checklist).
        check: bool = False
        # True when the correction was specific to this feature and teaches nothing general.
        skip: bool = False

    return Reply


def _first_sentence(text: str) -> str:
    text = " ".join((text or "").split())
    m = re.match(r"(.+?[.!?])(\s|$)", text)
    return (m.group(1) if m else text)[:300]


def stub_lesson(c: Candidate) -> dict:
    """Deterministic lesson for stub mode (and when the model's reply doesn't fit)."""
    first = c.corrections[-1]
    if c.why == "remember" and (first.get("lesson") or "").strip():
        return {"text": " ".join(first["lesson"].split())[:400], "check": True, "skip": False}
    if c.why == "takeover":
        ch = first.get("changes") or {}
        parts = [f"cover {', '.join(ch[k])}" for k in ("added", "changed") if ch.get(k)]
        said = _first_sentence(first.get("feedback", ""))
        text = (f"When writing the {c.phase} document, " + " and ".join(parts) + " the way people did when they "
                "took it over" if parts else f"Follow how people rewrote the {c.phase} document")
        return {"text": (text + (f": {said}" if said else ".")).strip()[:400], "check": False, "skip": False}
    if c.why == "repeat":
        said = "; ".join(dict.fromkeys(_first_sentence(x.get("feedback", "")) for x in c.corrections if x.get("feedback")))
        label = REASON_LABELS.get(c.reason, c.reason)
        return {"text": f"{c.phase.capitalize()} documents keep being sent back because {label}. People asked: "
                        f"{said or 'see the sources'}"[:400], "check": False, "skip": False}
    said = _first_sentence(first.get("feedback", ""))
    return {"text": said or f"Follow people's correction to the {c.phase} document.", "check": bool(said), "skip": not said}


def distill(services, state: dict, c: Candidate) -> dict:
    """One lesson from the candidate's corrections: an LLM writes it (the knowledge role), the stub
    rule stands in when it can't."""
    from .agents import structured
    from .graph.phase import call_agent

    stub = stub_lesson(c)
    model = _reply_model()
    evidence = []
    for x in c.corrections:
        line = f"- {x['feature_id']} {(x.get('gate') or x['phase']).upper()} {x.get('kind')}"
        if x.get("reason"):
            line += f" ({x['reason']})"
        if x.get("feedback"):
            line += f": {x['feedback']}"
        if x.get("lesson"):
            line += f"\n  The person's own lesson: {x['lesson']}"
        if (x.get("changes") or {}).get("diff"):
            line += "\n  What they changed in the agents' draft:\n" + x["changes"]["diff"][:2500]
        evidence.append(line)
    tail = (f"People corrected the agents' {c.phase} work ({'they asked to remember it' if c.why == 'remember' else 'they took it over and rewrote it' if c.why == 'takeover' else 'the same correction on several features'}). "
            "Write one lesson for future features of this product: an instruction the agent writing this phase "
            "can follow, general enough to apply to other features, without people's names. Say whether a reader "
            "could check it in the finished document. If the correction only made sense for this one feature, "
            "set skip.\n\nCorrections:\n" + "\n".join(evidence) + "\n\n" + structured.instruction(model))
    ctx = {"product_id": state["product_id"], "feature_id": state["feature_id"], "governance": state.get("governance")}
    try:
        res = call_agent(services, role="knowledge", phase=c.phase, ctx=ctx, skills=(), tail=tail,
                         stub_text=json.dumps(stub), max_tokens=512, schema=structured.schema(model))
        reply, _ = structured.parse(res.text, model)
    except Exception:
        reply = None
    if reply is None:
        return stub
    return {"text": " ".join(reply.text.split()), "check": reply.check, "skip": reply.skip}


def _next_ids(existing: list[str], n: int) -> list[str]:
    nums = [int(m.group(1)) for i in existing if (m := _ID.match(i))]
    start = max(nums, default=0) + 1
    return [f"L-{k:03d}" for k in range(start, start + n)]


def _source_line(x: dict) -> str:
    return (f"{x['feature_id']} {(x.get('gate') or x['phase']).upper()} {x.get('kind')} by {x.get('approver', 'unknown')}"
            + (f" ({x['reason']})" if x.get("reason") else ""))


def propose(services, state: dict, cands: list[Candidate]) -> list[dict]:
    """Distil each candidate and open one governance pull request with the new lessons."""
    store = services.lessons
    pid = state["product_id"]
    gov = services.settings.entry(pid).governance
    src = services.governance.source
    sha = src.resolve(gov.org, gov.repo, gov.ref)
    taken = [lesson.id for lesson in load_dir(src, gov.org, gov.repo, sha)] + [p["id"] for p in store.proposals(pid)]
    picked = []
    for c in cands:
        d = distill(services, state, c)
        if not d.get("skip") and d.get("text"):
            picked.append((c, d))
    if not picked:
        return []
    ids = _next_ids(taken, len(picked))
    branch = f"vrixel/lessons-{state['feature_id']}-{ids[0]}"
    files, made = {}, []
    for lid, (c, d) in zip(ids, picked):
        lesson = Lesson(id=lid, phase=c.phase, text=d["text"], check=bool(d.get("check")),
                        sources=tuple(dict.fromkeys(_source_line(x) for x in c.corrections)))
        path = f"{LESSONS_DIR}/{lid}.yaml"
        files[path] = render(lesson)
        made.append((lesson, c, path))
    gh = services.github
    for path, content in files.items():
        gh.put_file(repo=gov.full_name, branch=branch, path=path, content=content,
                    message=f"{state['feature_id']}: propose {path.rsplit('/', 1)[-1]}")
    why = {"remember": "a person asked to remember it", "takeover": "a person took the phase over and rewrote it",
           "repeat": f"the same correction came up on {REPEAT_FEATURES} or more features"}
    body = ("People corrected the agents' work. These lessons are proposed so future features don't need the same "
            "correction:\n\n"
            + "\n".join(f"- **{lesson.id}** ({lesson.phase}{', on the checklist' if lesson.check else ''}): {lesson.text}\n"
                        f"  Why: {why[c.why]}. From: {'; '.join(lesson.sources)}" for lesson, c, _ in made)
            + "\n\nEdit the wording if needed (set `check: false` if a reader can't check it in the document), then "
              "merge. Merged lessons apply to features started after the merge: their agents follow them, and "
              "lessons with `check: true` join the critic's checklist. Close the pull request to drop them.")
    pr = gh.open_pr(repo=gov.full_name, branch=branch, title=f"{state['feature_id']}: propose lessons {', '.join(ids)}",
                    body=body, files=sorted(files), labels=["vrixel", "lessons"])
    out = []
    for lesson, c, path in made:
        p = {"id": lesson.id, "product_id": pid, "feature_id": state["feature_id"], "phase": lesson.phase,
             "text": lesson.text, "check": lesson.check, "why": c.why, "key": c.key, "reason": c.reason,
             "sources": list(lesson.sources), "path": path, "pr": pr["url"]}
        store.add_proposal(p)
        out.append(p)
    return out


def learn(services, state: dict, corrections: list[dict]) -> list[dict]:
    """Record people's corrections and propose lessons from them. Returns events for the thread;
    never raises (a lesson is a proposal and must not stop the feature)."""
    from .graph.phase import event

    store = getattr(services, "lessons", None)
    if store is None or not corrections:
        return []
    try:
        for c in corrections:
            store.add_correction({k: v for k, v in c.items() if k != "changes"}
                                 | {"changes": {k: v for k, v in (c.get("changes") or {}).items() if k != "diff"}})
        made = propose(services, state, candidates(store, state["product_id"], corrections))
        return [event("lessons", "lesson_proposed", p["text"], lesson=p["id"], phase=p["phase"], pr=p["pr"], why=p["why"])
                for p in made]
    except Exception as exc:
        return [event("lessons", "lesson_failed", f"{type(exc).__name__}: {exc}")]


def overview(services, product_id: str) -> dict:
    """Approved lessons (in the governance repo now) and proposals still waiting for review."""
    gov = services.settings.entry(product_id).governance
    src = services.governance.source
    sha = src.resolve(gov.org, gov.repo, gov.ref)
    approved = load_dir(src, gov.org, gov.repo, sha)
    have = {lesson.id for lesson in approved}
    proposals = services.lessons.proposals(product_id) if getattr(services, "lessons", None) else []
    by_id = {p["id"]: p for p in proposals}
    return {
        "product_id": product_id,
        "approved": [{**lesson.as_dict(), "pr": by_id.get(lesson.id, {}).get("pr", ""),
                      "feature_id": by_id.get(lesson.id, {}).get("feature_id", "")} for lesson in approved],
        "proposed": [p for p in proposals if p["id"] not in have],
        "corrections": len(services.lessons.corrections(product_id)) if getattr(services, "lessons", None) else 0,
    }

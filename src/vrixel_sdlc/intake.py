"""Guided intake: a chat that turns an intent creator's idea into a feature.

The agent asks for what the pipeline needs (lane, problem, users, success measure), drafts the
title and description, checks what the product already has (features in flight, released or
stopped, and live capabilities), and submits only when the creator confirms. A draft that looks
like an existing feature can be submitted only with a reason it is different. In stub mode it is a deterministic interviewer; in live mode it is a Claude call
through the model gateway that returns JSON, merged with the same validation.
"""

from __future__ import annotations

import json
import re
import threading
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

from .gateway import CallTags, PromptLayers

FIELDS = ("product_id", "problem", "users", "success_metric")
QUESTIONS = {
    "product_id": "Which product is this for? You can work in: {lanes}.",
    "users": "Who will use this, and in what situation?",
    "success_metric": "How will we know it worked? A number or an observable change is best.",
}
_WORD = re.compile(r"[a-z0-9]+")
_JSON = re.compile(r"\{.*\}", re.S)
_TITLE_EDIT = re.compile(r"^\s*(?:change\s+(?:the\s+)?)?title\s*(?:to|:)\s*(.+)$", re.I)


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class Draft:
    product_id: str = ""
    title: str = ""
    problem: str = ""
    users: str = ""
    success_metric: str = ""
    related_capability: str = ""  # the closest related feature or capability, noted in the description
    similar: list = field(default_factory=list)  # matches from vrixel_sdlc.duplicates
    not_duplicate_reason: str = ""

    def missing(self) -> list[str]:
        return [f for f in FIELDS if not getattr(self, f)]

    def description(self) -> str:
        parts = [self.problem.strip(), "", f"Users: {self.users.strip()}", f"Success measure: {self.success_metric.strip()}"]
        if self.related_capability:
            parts.append(f"Related: {self.related_capability}")
        if self.not_duplicate_reason:
            parts.append(f"Not a duplicate because: {self.not_duplicate_reason}")
        return "\n".join(parts).strip()


@dataclass
class IntakeSession:
    id: str
    owner: str
    lanes: list[str]  # lanes this person may submit to
    messages: list[dict] = field(default_factory=list)
    draft: Draft = field(default_factory=Draft)
    awaiting: str = ""
    overlap_checked: bool = False
    feature_id: str = ""
    created_at: str = field(default_factory=now)
    attachments: list = field(default_factory=list)  # vrixel_sdlc.attachments: names and checks, never contents

    @property
    def duplicate(self) -> bool:
        """Looks like an existing feature and has no reason why it is different."""
        return any(m["duplicate"] for m in self.draft.similar) and not self.draft.not_duplicate_reason

    @property
    def ready(self) -> bool:
        return not self.draft.missing() and not self.feature_id and not self.duplicate

    def to_dict(self) -> dict:
        d = asdict(self)
        d["ready"] = self.ready
        d["missing"] = self.draft.missing()
        return d


def session_from(d: dict) -> IntakeSession:
    d = {k: v for k, v in d.items() if k not in ("ready", "missing")}
    d["draft"] = Draft(**(d.get("draft") or {}))
    return IntakeSession(**d)


class IntakeStore:
    """In-memory sessions: one process (drafts are lost on restart; submitted features are not).
    PostgresIntakeStore keeps them for every replica."""

    def __init__(self) -> None:
        self._s: dict[str, IntakeSession] = {}
        self._lock = threading.Lock()

    def create(self, owner: str, lanes: list[str]) -> IntakeSession:
        s = IntakeSession(id=uuid.uuid4().hex[:12], owner=owner, lanes=lanes)
        with self._lock:
            self._s[s.id] = s
        return s

    def get(self, sid: str, owner: str) -> IntakeSession:
        with self._lock:
            s = self._s.get(sid)
        if s is None or s.owner != owner:
            raise KeyError(f"No intake conversation {sid}")
        return s

    def list(self, owner: str) -> list[IntakeSession]:
        with self._lock:
            return sorted((s for s in self._s.values() if s.owner == owner), key=lambda s: s.created_at, reverse=True)

    def save(self, s: IntakeSession) -> None:
        """Sessions are changed in place; in memory there is nothing more to do."""


class PostgresIntakeStore:
    """Intake chats in Postgres: any replica continues a conversation, and drafts survive restarts."""

    def __init__(self, pool) -> None:
        self.pool = pool
        with pool.connection() as conn:
            conn.execute("create table if not exists vrixel_intake (id text primary key, owner text not null,"
                         " data jsonb not null, created_at timestamptz not null default now())")

    def create(self, owner: str, lanes: list[str]) -> IntakeSession:
        s = IntakeSession(id=uuid.uuid4().hex[:12], owner=owner, lanes=lanes)
        self.save(s)
        return s

    def save(self, s: IntakeSession) -> None:
        with self.pool.connection() as conn:
            conn.execute("insert into vrixel_intake (id, owner, data) values (%s, %s, %s) on conflict (id)"
                         " do update set data = excluded.data", (s.id, s.owner, json.dumps(asdict(s))))

    def get(self, sid: str, owner: str) -> IntakeSession:
        with self.pool.connection() as conn:
            row = conn.execute("select data from vrixel_intake where id = %s and owner = %s", (sid, owner)).fetchone()
        if not row:
            raise KeyError(f"No intake conversation {sid}")
        data = row["data"] if isinstance(row, dict) else row[0]
        return session_from(data)

    def list(self, owner: str) -> list[IntakeSession]:
        with self.pool.connection() as conn:
            rows = conn.execute("select data from vrixel_intake where owner = %s order by created_at desc limit 50",
                                (owner,)).fetchall()
        return [session_from(r["data"] if isinstance(r, dict) else r[0]) for r in rows]


def _title_from(problem: str) -> str:
    first = re.split(r"(?<=[.!?])\s|\n", problem.strip(), maxsplit=1)[0].rstrip(".!?")
    words = first.split()
    title = " ".join(words[:9]) + ("…" if len(words) > 9 else "")
    return title[:1].upper() + title[1:]


class IntakeAgent:
    def __init__(self, services) -> None:
        self.services = services

    # Lane vocabulary: id, words of the product name, and the org name after "vrixel-".
    def _lane_words(self, lane_id: str) -> set[str]:
        entry = self.services.settings.entry(lane_id)
        words = {lane_id.lower(), entry.governance.org.lower().removeprefix("vrixel-")}
        try:
            words |= {w for w in _WORD.findall(self.services.snapshot(lane_id).product.name.lower()) if len(w) >= 3}
        except Exception:
            pass
        return words

    def lane_names(self, lanes: list[str]) -> dict[str, str]:
        out = {}
        for lane in lanes:
            try:
                out[lane] = self.services.snapshot(lane).product.name
            except Exception:
                out[lane] = lane
        return out

    def detect_lane(self, text: str, lanes: list[str]) -> str:
        words = set(_WORD.findall(text.lower()))
        hits = [lane for lane in lanes if words & self._lane_words(lane)]
        return hits[0] if len(hits) == 1 else ""

    def _overlap(self, d: Draft) -> str:
        """Find what the product already has that looks like this draft; returns the closest
        related item for the description (duplicates are handled by the caller)."""
        from .duplicates import describe, find_similar

        if not d.product_id:
            d.similar = []
            return ""
        lookup = getattr(self.services, "features", None)
        features = lookup(d.product_id) if callable(lookup) else []
        d.similar = find_similar(d.title, d.problem, features, self.services.catalog.lookup(d.product_id))
        related = [m for m in d.similar if not m["duplicate"]]
        return describe(related[0]) if related else ""

    def _duplicate_question(self, s: IntakeSession) -> str:
        from .duplicates import describe

        s.awaiting = "not_duplicate_reason"
        dups = [m for m in s.draft.similar if m["duplicate"]]
        return ("This looks like " + "; ".join(describe(m) for m in dups) + ". If it's the same need, add to that "
                "feature instead of starting a new one. If it's different, tell me how, and I'll note it for the "
                "G1 approver.")

    def _next_question(self, s: IntakeSession) -> str:
        for f in FIELDS:
            if not getattr(s.draft, f) and f in QUESTIONS:
                s.awaiting = f
                names = self.lane_names(s.lanes)
                return QUESTIONS[f].format(lanes=", ".join(f"{n} ({k})" for k, n in names.items()))
        s.awaiting = ""
        return ""

    def _summary(self, s: IntakeSession) -> str:
        d = s.draft
        name = self.lane_names([d.product_id]).get(d.product_id, d.product_id)
        lines = [f"Here's the intent for {name}:", f"“{d.title}”"]
        if d.related_capability:
            lines.append(f"It looks related to {d.related_capability}; I've noted that so the agents build on it.")
        if d.not_duplicate_reason:
            lines.append(f"I've noted why it's different: {d.not_duplicate_reason}")
        lines.append("Submit it when it looks right, or tell me what to change.")
        return "\n".join(lines)

    def respond(self, s: IntakeSession, text: str) -> str:
        if s.feature_id:
            return f"This intent was submitted as {s.feature_id}. Start a new conversation for another one."
        s.messages.append({"role": "user", "text": text, "at": now()})
        if s.awaiting == "not_duplicate_reason":  # the creator's answer to "this looks like X"
            s.draft.not_duplicate_reason = text.strip()
            s.awaiting = ""
            reply = self._summary(s)
            s.messages.append({"role": "agent", "text": reply, "at": now()})
            return reply
        if self.services.settings.mode == "live":
            reply = self._respond_live(s)
        else:
            reply = self._respond_stub(s, text)
        s.messages.append({"role": "agent", "text": reply, "at": now()})
        return reply

    def _respond_stub(self, s: IntakeSession, text: str) -> str:
        d = s.draft
        lane = self.detect_lane(text, s.lanes)
        if s.awaiting == "product_id":
            if lane:
                d.product_id = lane
            else:
                names = self.lane_names(s.lanes)
                return "I couldn't tell which product. Reply with one of: " + ", ".join(
                    f"{n} ({k})" for k, n in names.items()) + "."
        elif s.awaiting in ("users", "success_metric"):
            setattr(d, s.awaiting, text.strip())
        elif not d.problem:
            d.problem, d.title = text.strip(), _title_from(text)
            if lane:
                d.product_id = lane
            elif len(s.lanes) == 1:  # only one lane this person can submit to: no need to ask
                d.product_id = s.lanes[0]
        elif m := _TITLE_EDIT.match(text):
            d.title = m.group(1).strip().rstrip(".")
            return "Title updated. " + self._summary(s)
        else:
            d.problem = f"{d.problem.rstrip()}\n\n{text.strip()}"
            return "Added that to the problem statement. " + (self._summary(s) if not d.missing() else self._next_question(s))

        if not d.missing() and not s.overlap_checked:
            s.overlap_checked = True
            d.related_capability = self._overlap(d)
        if not d.missing() and s.duplicate:
            return self._duplicate_question(s)
        question = self._next_question(s)
        if question:
            lead = "Got it. " if len(s.messages) > 1 else "Thanks. I'll draft this as we go. "
            return lead + question
        return self._summary(s)

    def _respond_live(self, s: IntakeSession) -> str:
        names = self.lane_names(s.lanes)
        tail = (
            "You help a product person turn an idea into a feature intent for an agentic SDLC. "
            "Collect: product_id (one of " + json.dumps(names) + "), problem, users, success_metric, and a short title. "
            "Ask one question at a time, in plain language. Do not invent facts. "
            'Reply with JSON only: {"reply": str, "draft": {"product_id", "title", "problem", "users", "success_metric"}}.\n\n'
            f"Current draft: {json.dumps(asdict(s.draft))}\n\n"
            + (f"Files attached (you see names only; the feature's agents read them after submit): "
               f"{', '.join(a['name'] for a in s.attachments)}\n\n" if s.attachments else "")
            + "Conversation:\n"
            + "\n".join(f"{m['role']}: {m['text']}" for m in s.messages)
        )
        layers = PromptLayers(tools="", skills="product-management:write-spec", constitution="",
                              product_context="", feature_context="", tail=tail)
        tags = CallTags(product_id="INTAKE", feature_id=f"intake-{s.id}", phase="intake", role="intake",
                        data_class="phi", workspace="wrkspc-intake")
        res = self.services.gateway.complete(tags, layers, max_tokens=800)
        m = _JSON.search(res.text or "")
        try:
            data = json.loads(m.group(0)) if m else {}
        except json.JSONDecodeError:
            data = {}
        for k, v in (data.get("draft") or {}).items():
            if k == "product_id" and v not in s.lanes:
                continue
            if k in ("product_id", "title", "problem", "users", "success_metric") and isinstance(v, str) and v.strip():
                setattr(s.draft, k, v.strip())
        if not s.draft.missing() and not s.overlap_checked:
            s.overlap_checked = True
            s.draft.related_capability = self._overlap(s.draft)
        if not s.draft.missing() and s.duplicate:
            return self._duplicate_question(s)
        return str(data.get("reply") or "Could you say a bit more about what you need?")

    def update_draft(self, s: IntakeSession, changes: dict) -> None:
        if s.feature_id:
            raise ValueError("This intent was already submitted")
        for k, v in changes.items():
            if k not in ("product_id", "title", "problem", "users", "success_metric"):
                raise ValueError(f"Unknown field {k}")
            if k == "product_id" and v and v not in s.lanes:
                raise PermissionError(f"You can't submit intents for {v}")
            setattr(s.draft, k, str(v).strip())
        if set(changes) & {"product_id", "title", "problem"}:
            s.overlap_checked = False  # what it looks like may have changed
            s.draft.not_duplicate_reason = ""
            s.draft.related_capability = self._overlap(s.draft)
            s.overlap_checked = True

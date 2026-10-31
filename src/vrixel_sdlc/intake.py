"""Guided intake: a chat that turns an intent creator's idea into a feature.

The agent asks for what the pipeline needs (lane, problem, users, success measure), drafts the
title and description, checks the capability catalog for overlap, and submits only when the
creator confirms. In stub mode it is a deterministic interviewer; in live mode it is a Claude call
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
    related_capability: str = ""

    def missing(self) -> list[str]:
        return [f for f in FIELDS if not getattr(self, f)]

    def description(self) -> str:
        parts = [self.problem.strip(), "", f"Users: {self.users.strip()}", f"Success measure: {self.success_metric.strip()}"]
        if self.related_capability:
            parts.append(f"Related capability: {self.related_capability}")
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

    @property
    def ready(self) -> bool:
        return not self.draft.missing() and not self.feature_id

    def to_dict(self) -> dict:
        d = asdict(self)
        d["ready"] = self.ready
        d["missing"] = self.draft.missing()
        return d


class IntakeStore:
    """In-memory sessions (drafts are lost on restart; submitted features are not)."""

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
        if not d.product_id:
            return ""
        text = set(w for w in _WORD.findall(f"{d.title} {d.problem}".lower()) if len(w) >= 5)
        for cap in self.services.catalog.lookup(d.product_id):
            if text & set(w for w in _WORD.findall(cap["name"].lower()) if len(w) >= 5):
                return f"{cap['id']} ({cap['name']})"
        return ""

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
            lines.append(f"It looks related to {d.related_capability}, so I've noted that it extends it.")
        lines.append("Submit it when it looks right, or tell me what to change.")
        return "\n".join(lines)

    def respond(self, s: IntakeSession, text: str) -> str:
        if s.feature_id:
            return f"This intent was submitted as {s.feature_id}. Start a new conversation for another one."
        s.messages.append({"role": "user", "text": text, "at": now()})
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
            f"Current draft: {json.dumps(asdict(s.draft))}\n\nConversation:\n"
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
        s.overlap_checked = False

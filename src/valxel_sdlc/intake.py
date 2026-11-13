"""Guided intake: the one front door. A chat that turns someone's idea into a feature, an
initiative or a new product, whichever it is.

After the first message the agent sizes the request (valxel_sdlc.intake.size_of): one change is a
feature, several features toward a goal is an initiative, new users or a new business is a new
product. The person confirms or picks another size, and can switch at any time; each size asks for
what its flow needs, one question at a time, with quick replies and "leave the rest to the agent":

    feature      product, problem, users, success measure (+ the experience round)  -> a feature
    initiative   product, the outcome, the number that shows it worked, when, research -> G0
    product      name, GitHub org, the idea, users, the first goal, research          -> inception

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
SIZES = ("feature", "initiative", "product")
# What each size needs before it can be submitted, in the order the agent asks.
NEEDS = {
    "feature": FIELDS,
    "initiative": ("product_id", "problem", "success_metric", "horizon", "research"),
    "product": ("name", "org", "problem", "users", "success_metric", "research"),
}
AGENT_PROPOSES = "Proposed by the agent"  # a field the person left to the agent
SIZE_OPTIONS = {"initiative": "Plan it as an initiative", "feature": "Just one feature for now",
                "product": "It's a new product"}
SIZE_QUESTIONS = {
    "initiative": ("This sounds like an initiative: {why} It becomes a goal you can measure and the features that "
                   "move it, on {product}'s roadmap."),
    "product": ("This sounds like a new product: {why} It starts with research, then a brief, an architecture and a "
                "first roadmap, each approved by the product council."),
    "feature": "I'll treat this as one feature. If it's bigger than that, say so and I'll plan it as an initiative.",
}
SIZED_QUESTIONS = {
    "initiative": {
        "product_id": ("Which product's roadmap is it for? You can plan for: {lanes}.", []),
        "success_metric": ("Which number would tell you it worked? Name it and, if you can, where it should get to "
                           "and by when.", ["Leave it to the agent"]),
        "horizon": ("When should it start?", ["Now", "Next", "Later", "You decide"]),
        "research": ("Before the plan, an agent researches the problem and the alternatives. How deep?",
                     ["Quick web research", "Thorough research", "I've done my own"]),
    },
    "product": {
        "name": ("What's the product called? A working name is fine.", []),
        "org": ("Which GitHub org will it live in? It must already exist.", []),
        "users": ("Who is it for, and in what situation do they use it?", []),
        "success_metric": ("What's the first goal, with a number? For example: 50 paying farms by June.",
                           ["Leave it to the agent"]),
        "research": ("Before the brief, an agent researches the problem, the alternatives and the market. How deep?",
                     ["Quick web research", "Thorough research", "I've done my own"]),
    },
}
_SIZE_PRODUCT = re.compile(r"\b(new product|new app|new business|new company|an? (?:[\w-]+ ){0,3}(?:app|platform|"
                           r"service|marketplace|saas) for|"
                           r"a marketplace|a saas|start(?:ing)? an? (?:business|company|product|startup)|"
                           r"launch an? (?:product|app|service)|build an? (?:new )?(?:product|app|platform|marketplace))\b",
                           re.I)
_SIZE_GOAL = re.compile(r"\b(reduce|cut|increase|grow|halve|double|fewer|faster|improve|lower|raise)\b[^.]*\d|"
                        r"\b(by|within) (q[1-4]|end of|\d+ (weeks|months))\b", re.I)
_SIZE_SAY = re.compile(r"\b(?:make|plan|treat|turn|start)\b.{0,20}\b(initiative|feature|new product|product)\b|"
                       r"^\s*(plan it as an initiative|just one feature|it'?s a new product)", re.I)


def size_of(text: str, lane: str = "") -> tuple[str, str]:
    """A first guess at the request's size, and why, from what the person wrote."""
    if _SIZE_PRODUCT.search(text) and not lane:
        return "product", "it describes new users or a new business rather than a change to a product you have."
    parts = [p for p in re.split(r";|\n|\. |, and | and also ", text) if len(p.strip()) > 12]
    if len(parts) >= 3:
        return "initiative", f"it has {len(parts)} parts, which is probably several features working toward one goal."
    if _SIZE_GOAL.search(text):
        return "initiative", "it names an outcome to move, which usually takes more than one feature."
    return "feature", "it reads as one change."


def size_said(text: str) -> str:
    """The size the person picked or asked to switch to, if any."""
    m = _SIZE_SAY.search(text)
    if not m:
        return ""
    said = (m.group(1) or m.group(2) or "").lower()
    return "initiative" if "initiative" in said else "feature" if "feature" in said else "product"


RESEARCH_SAID = {"quick": "quick", "thorough": "thorough", "own": "own", "my own": "own", "done": "own"}


def research_said(text: str) -> str:
    t = text.lower()
    if re.search(r"\b(my own|done (my|our) own|i'?ve done|own research|own)\b", t):
        return "own"
    if "thorough" in t or "deep" in t:
        return "thorough"
    if re.search(r"\b(quick|light|yes|ok|sure|you decide)\b", t):
        return "quick"
    return ""

QUESTIONS = {
    "product_id": "Which product is this for? You can work in: {lanes}.",
    "users": "Who will use this, and in what situation?",
    "success_metric": "How will we know it worked? A number or an observable change is best.",
}
_WORD = re.compile(r"[a-z0-9]+")
_JSON = re.compile(r"\{.*\}", re.S)
_TITLE_EDIT = re.compile(r"^\s*(?:change\s+(?:the\s+)?)?title\s*(?:to|:)\s*(.+)$", re.I)

# The experience round: asked only when the intent changes what people see, so the design agent
# works from what the person wants instead of deciding everything itself. Each answer is optional;
# "you decide" leaves that part (or the rest) to the design agent, and the brief says so.
EXPERIENCE = (
    ("where", "Screen or flow"),
    ("main_task", "Main task"),
    ("context", "Where they use it"),
    ("look", "Look"),
)
EXPERIENCE_QUESTIONS = {
    "where": ("This changes what people see in {product}. Which screen or flow should change, or is it a new one? "
              "A screenshot helps: attach it with the paperclip.", ["A new screen", "You decide the rest"]),
    "main_task": ("On that screen, what's the one thing a person must be able to do or see first?",
                  ["You decide the rest"]),
    "context": ("Where are they when they use it?", ["On a phone, on the move", "At a desk", "Both", "You decide the rest"]),
    "look": ("Should it follow {product}'s current design, or do you want something different? Name anything to "
             "mirror or avoid.", ["Follow the current design", "You decide"]),
}
DELEGATED = "Left to the design agent"
_DELEGATE = re.compile(r"\b(you decide|up to you|no preference|don'?t mind|not sure|skip|whatever works|your call)\b", re.I)
_DELEGATE_REST = re.compile(r"\b(the rest|everything|all of (it|them)|rest)\b", re.I)
_UI_WORDS = re.compile(r"\b(screens?|pages?|forms?|buttons?|dashboards?|views?|displays?|shows?|see|sees|alerts?|"
                       r"notif\w*|menus?|layouts?|ui|ux|design|tablets?|phones?|mobile|tap|click|"
                       r"report|chart|list|search|sign[- ]?in|onboarding|checkout)\b", re.I)
UI_ROLES = ("consumer", "bff", "mobile")


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
    similar: list = field(default_factory=list)  # matches from valxel_sdlc.duplicates
    not_duplicate_reason: str = ""
    ui: bool = False  # the intent changes what people see: the experience round applies
    experience: dict = field(default_factory=dict)  # EXPERIENCE keys -> the person's answer, or DELEGATED
    size: str = ""  # feature | initiative | product (SIZES); "" until the person confirms one
    size_suggested: str = ""  # the agent's guess, and why
    size_why: str = ""
    horizon: str = ""  # initiative: now | next | later | AGENT_PROPOSES
    research: str = ""  # initiative and product: quick | thorough | own (valxel_sdlc.research)
    name: str = ""  # product: its name
    org: str = ""  # product: the GitHub org it will live in
    budget_usd: float = 0.0  # product: the first year's features (optional)

    @property
    def experience_done(self) -> bool:
        return all(self.experience.get(k) for k, _ in EXPERIENCE)

    def missing(self) -> list[str]:
        if self.size in ("initiative", "product"):
            return [f for f in NEEDS[self.size] if not getattr(self, f)]
        out = [f for f in FIELDS if not getattr(self, f)]
        if self.ui and not self.experience_done:
            out.append("experience")
        return out if self.size else ["size", *out]

    def request(self) -> str:
        """An initiative's request: the outcome, then who it's for."""
        parts = [self.problem.strip()]
        if self.users:
            parts += ["", f"Users: {self.users.strip()}"]
        if self.not_duplicate_reason:
            parts.append(f"Not a duplicate because: {self.not_duplicate_reason}")
        return "\n".join(parts).strip()

    def brief(self) -> str:
        """The experience brief, for the description the agents and approvers read."""
        if not self.ui:
            return ""
        if self.experience and all(self.experience.get(k) == DELEGATED for k, _ in EXPERIENCE):
            return "Experience brief: left to the design agent, within the product's current design."
        lines = ["Experience brief:"]
        for k, label in EXPERIENCE:
            lines.append(f"- {label}: {self.experience.get(k) or 'not said'}")
        return "\n".join(lines)

    def description(self) -> str:
        parts = [self.problem.strip(), "", f"Users: {self.users.strip()}", f"Success measure: {self.success_metric.strip()}"]
        if self.related_capability:
            parts.append(f"Related: {self.related_capability}")
        if self.not_duplicate_reason:
            parts.append(f"Not a duplicate because: {self.not_duplicate_reason}")
        if self.brief():
            parts += ["", self.brief()]
        return "\n".join(parts).strip()


@dataclass
class IntakeSession:
    id: str
    owner: str
    lanes: list[str]  # lanes this person may submit to
    messages: list[dict] = field(default_factory=list)
    draft: Draft = field(default_factory=Draft)
    awaiting: str = ""
    options: list = field(default_factory=list)  # quick replies offered with the agent's last question
    overlap_checked: bool = False
    ui_checked: bool = False  # whether the experience round applies has been decided (people can change it)
    feature_id: str = ""
    created_at: str = field(default_factory=now)
    attachments: list = field(default_factory=list)  # valxel_sdlc.attachments: names and checks, never contents
    starter: bool = False  # may start a new product (registry inception.starters)
    kind: str = ""  # what was submitted: feature | initiative | inception

    @property
    def duplicate(self) -> bool:
        """Looks like an existing feature and has no reason why it is different."""
        return any(m["duplicate"] for m in self.draft.similar) and not self.draft.not_duplicate_reason

    @property
    def ready(self) -> bool:
        if self.draft.size == "product" and not self.starter:
            return False
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

    def create(self, owner: str, lanes: list[str], starter: bool = False) -> IntakeSession:
        s = IntakeSession(id=uuid.uuid4().hex[:12], owner=owner, lanes=lanes, starter=starter)
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
            conn.execute("create table if not exists valxel_intake (id text primary key, owner text not null,"
                         " data jsonb not null, created_at timestamptz not null default now())")

    def create(self, owner: str, lanes: list[str], starter: bool = False) -> IntakeSession:
        s = IntakeSession(id=uuid.uuid4().hex[:12], owner=owner, lanes=lanes, starter=starter)
        self.save(s)
        return s

    def save(self, s: IntakeSession) -> None:
        with self.pool.connection() as conn:
            conn.execute("insert into valxel_intake (id, owner, data) values (%s, %s, %s) on conflict (id)"
                         " do update set data = excluded.data", (s.id, s.owner, json.dumps(asdict(s))))

    def get(self, sid: str, owner: str) -> IntakeSession:
        with self.pool.connection() as conn:
            row = conn.execute("select data from valxel_intake where id = %s and owner = %s", (sid, owner)).fetchone()
        if not row:
            raise KeyError(f"No intake conversation {sid}")
        data = row["data"] if isinstance(row, dict) else row[0]
        return session_from(data)

    def list(self, owner: str) -> list[IntakeSession]:
        with self.pool.connection() as conn:
            rows = conn.execute("select data from valxel_intake where owner = %s order by created_at desc limit 50",
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

    # Lane vocabulary: id, words of the product name, and the org name after "valxel-".
    def _lane_words(self, lane_id: str) -> set[str]:
        entry = self.services.settings.entry(lane_id)
        words = {lane_id.lower(), entry.governance.org.lower().removeprefix("valxel-")}
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
        if s.draft.size in SIZED_QUESTIONS:
            names = self.lane_names(s.lanes)
            for f in NEEDS[s.draft.size]:
                if getattr(s.draft, f):
                    continue
                if f == "product_id" and len(s.lanes) == 1:
                    s.draft.product_id = s.lanes[0]
                    continue
                if f == "problem":
                    s.awaiting = "problem"
                    return "What outcome do you want, and for whom?"
                question, options = SIZED_QUESTIONS[s.draft.size][f]
                s.awaiting, s.options = f, list(options)
                if f in ("success_metric", "horizon", "research") and not s.options[-1].startswith("Leave"):
                    s.options.append("Leave the rest to the agent")
                return question.format(lanes=", ".join(f"{n} ({k})" for k, n in names.items()))
            s.awaiting = ""
            return ""
        for f in FIELDS:
            if not getattr(s.draft, f) and f in QUESTIONS:
                s.awaiting = f
                names = self.lane_names(s.lanes)
                return QUESTIONS[f].format(lanes=", ".join(f"{n} ({k})" for k, n in names.items()))
        if s.draft.ui:
            for k, _ in EXPERIENCE:
                if not s.draft.experience.get(k):
                    s.awaiting = f"experience:{k}"
                    question, options = EXPERIENCE_QUESTIONS[k]
                    s.options = list(options)
                    name = self.lane_names([s.draft.product_id]).get(s.draft.product_id, "the product")
                    return question.format(product=name)
        s.awaiting = ""
        return ""

    def touches_ui(self, d: Draft) -> bool:
        """Whether the intent changes what people see: the product has a web or mobile app, and the
        request talks about something people look at or do."""
        if not d.product_id:
            return False
        try:
            repos = self.services.snapshot(d.product_id).product.repos
        except Exception:
            return False
        if not any(r.role in UI_ROLES for r in repos):
            return False
        # The product's own name ("Farm app", "Clinic records") says nothing about the change.
        text = f"{d.title} {d.problem} {d.users}".lower()
        for w in self._lane_words(d.product_id):
            text = re.sub(rf"\b{re.escape(w)}\b", " ", text)
        return bool(_UI_WORDS.search(text))

    def _answer_experience(self, s: IntakeSession, key: str, text: str) -> None:
        exp = dict(s.draft.experience)
        if _DELEGATE.search(text):
            exp[key] = DELEGATED
            if _DELEGATE_REST.search(text) or key == "look":
                for k, _ in EXPERIENCE:
                    exp.setdefault(k, DELEGATED)
                    exp[k] = exp[k] or DELEGATED
        else:
            exp[key] = text.strip()
        s.draft.experience = exp

    def _summary(self, s: IntakeSession) -> str:
        d = s.draft
        if d.size == "initiative":
            name = self.lane_names([d.product_id]).get(d.product_id, d.product_id)
            return "\n".join([f"Here's the initiative for {name}: “{d.title}”.",
                              f"Research: {d.research}. The agent drafts the goal, budget and features from it; product "
                              "owners approve it at G0.", "Draft it when it looks right, or tell me what to change."])
        if d.size == "product":
            if not s.starter:
                return ("Starting a new product is for the inception starters. I can plan it as an initiative of a "
                        "product you work on instead: say 'plan it as an initiative'.")
            return "\n".join([f"Here's the new product: {d.name}, in {d.org}.",
                              f"Research: {d.research}. Then agents write the brief for the product council.",
                              "Start it when it looks right, or tell me what to change."])
        name = self.lane_names([d.product_id]).get(d.product_id, d.product_id)
        lines = [f"Here's the intent for {name}:", f"“{d.title}”"]
        if d.related_capability:
            lines.append(f"It looks related to {d.related_capability}; I've noted that so the agents build on it.")
        if d.not_duplicate_reason:
            lines.append(f"I've noted why it's different: {d.not_duplicate_reason}")
        if d.ui:
            said = [label.lower() for k, label in EXPERIENCE if d.experience.get(k) not in ("", None, DELEGATED)]
            lines.append("The design agent will work from what you said about " + ", ".join(said) + "." if said else
                         "You've left the design to the design agent; G3 approvers will see that.")
        lines.append("Submit it when it looks right, or tell me what to change.")
        return "\n".join(lines)

    def respond(self, s: IntakeSession, text: str) -> str:
        if s.feature_id:
            return f"This intent was submitted as {s.feature_id}. Start a new conversation for another one."
        s.messages.append({"role": "user", "text": text, "at": now()})
        s.options = []
        if s.awaiting == "not_duplicate_reason":  # the creator's answer to "this looks like X"
            s.draft.not_duplicate_reason = text.strip()
            s.awaiting = ""
            reply = self._summary(s)
            s.messages.append({"role": "agent", "text": reply, "at": now()})
            return reply
        # Initiatives and products ask a fixed set of questions; switching size is a fixed step too.
        structured = s.draft.size in SIZED_QUESTIONS or s.awaiting == "size" or bool(s.draft.problem and size_said(text))
        if self.services.settings.mode == "live" and not structured:
            reply = self._respond_live(s)
        else:
            reply = self._respond_stub(s, text)
        msg = {"role": "agent", "text": reply, "at": now()}
        if s.options:
            msg["options"] = list(s.options)[:4]
        s.messages.append(msg)
        return reply

    def set_size(self, s: IntakeSession, size: str) -> None:
        """Switch the request to another size, keeping everything said so far."""
        if size not in SIZES:
            raise ValueError(f"Size is one of {', '.join(SIZES)}")
        d = s.draft
        if size == d.size:
            return
        d.size = size
        if size == "product":
            d.product_id = ""
        elif not d.product_id and len(s.lanes) == 1:
            d.product_id = s.lanes[0]
        s.overlap_checked = False

    def _delegate_rest(self, s: IntakeSession) -> None:
        """'Leave the rest to the agent': what the agent can propose, it proposes."""
        d = s.draft
        if not d.success_metric:
            d.success_metric = AGENT_PROPOSES
        if d.size == "initiative" and not d.horizon:
            d.horizon = AGENT_PROPOSES
        if not d.research:
            d.research = "quick"
        if d.size == "product" and not d.users:
            d.users = AGENT_PROPOSES

    def _answer_sized(self, s: IntakeSession, text: str, lane: str) -> str:
        """An answer in an initiative or product conversation; returns a reply to stop at, or ''."""
        d, f = s.draft, s.awaiting
        delegate_rest = bool(_DELEGATE.search(text) or re.search(r"\bleave (it|the rest)\b", text, re.I)) and \
            bool(_DELEGATE_REST.search(text) or "leave the rest" in text.lower())
        if delegate_rest:
            self._delegate_rest(s)
            return ""
        if f == "product_id":
            if not lane:
                names = self.lane_names(s.lanes)
                return "I couldn't tell which product. Reply with one of: " + ", ".join(
                    f"{n} ({k})" for k, n in names.items()) + "."
            d.product_id = lane
        elif f == "success_metric":
            d.success_metric = AGENT_PROPOSES if re.search(r"\bleave it\b|agent", text, re.I) else text.strip()
        elif f == "horizon":
            t = text.strip().lower()
            d.horizon = next((h for h in ("now", "next", "later") if h in t), AGENT_PROPOSES)
        elif f == "research":
            d.research = research_said(text) or "quick"
        elif f == "org":
            org = re.sub(r"^https?://github\.com/", "", text.strip()).strip("/ ")
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]*", org):
                return "That doesn't look like a GitHub org name (letters, digits and dashes). Which org?"
            d.org = org
        elif f == "name":
            d.name = text.strip().strip("\"'“”")[:120]
            d.title = d.title or d.name
        elif f in ("users", "problem"):
            setattr(d, f, text.strip())
        else:
            d.problem = f"{d.problem.rstrip()}\n\n{text.strip()}"
        return ""

    def _named(self, text: str, lane: str) -> str:
        """The lane, if the text names it by id or full name (a shared word like 'app' doesn't count)."""
        if not lane:
            return ""
        name = self.lane_names([lane]).get(lane, lane)
        t = text.lower()
        return lane if re.search(rf"\b{re.escape(lane.lower())}\b", t) or name.lower() in t else ""

    def _size_reply(self, s: IntakeSession) -> str:
        d = s.draft
        name = self.lane_names([d.product_id]).get(d.product_id, "the product") if d.product_id else "a product"
        s.awaiting = "size"
        first = [d.size_suggested] + [k for k in SIZES if k != d.size_suggested]
        s.options = [SIZE_OPTIONS[k] for k in first]
        return SIZE_QUESTIONS[d.size_suggested].format(why=d.size_why[:1].upper() + d.size_why[1:], product=name)

    def _respond_stub(self, s: IntakeSession, text: str) -> str:
        d = s.draft
        lane = self.detect_lane(text, s.lanes)
        said = size_said(text) if d.problem else ""
        if said and (s.awaiting == "size" or said != d.size):
            self.set_size(s, said)
            s.awaiting = ""
            if said == "product" and not s.starter:
                return self._summary(s)
            q = self._next_question(s)
            lead = {"initiative": "Planning it as an initiative.", "feature": "One feature, then.",
                    "product": "A new product, then."}[said]
            return f"{lead} {q}" if q else f"{lead} " + self._summary(s)
        if s.awaiting == "size":  # anything else: take the suggestion and treat the text as more detail
            self.set_size(s, d.size_suggested)
            s.awaiting = ""
        if d.size in SIZED_QUESTIONS and d.problem:
            stop = self._answer_sized(s, text, lane)
            if stop:
                return stop
            q = self._next_question(s)
            return ("Got it. " + q) if q else self._summary(s)
        if s.awaiting == "product_id":
            if lane:
                d.product_id = lane
            else:
                names = self.lane_names(s.lanes)
                return "I couldn't tell which product. Reply with one of: " + ", ".join(
                    f"{n} ({k})" for k, n in names.items()) + "."
        elif s.awaiting in ("users", "success_metric"):
            setattr(d, s.awaiting, text.strip())
        elif s.awaiting.startswith("experience:"):
            self._answer_experience(s, s.awaiting.split(":", 1)[1], text)
        elif not d.problem:
            d.problem, d.title = text.strip(), _title_from(text)
            if lane:
                d.product_id = lane
            elif len(s.lanes) == 1:  # only one lane this person can submit to: no need to ask
                d.product_id = s.lanes[0]
            if not d.size:
                d.size_suggested, d.size_why = size_of(text, self._named(text, lane))
                if d.size_suggested != "feature":
                    return self._size_reply(s)
                d.size = "feature"
        elif m := _TITLE_EDIT.match(text):
            d.title = m.group(1).strip().rstrip(".")
            return "Title updated. " + self._summary(s)
        else:
            d.problem = f"{d.problem.rstrip()}\n\n{text.strip()}"
            return "Added that to the problem statement. " + (self._summary(s) if not d.missing() else self._next_question(s))

        if not [f for f in FIELDS if not getattr(d, f)] and not s.ui_checked:
            s.ui_checked = True
            d.ui = self.touches_ui(d)
        if not d.missing() and not s.overlap_checked:
            s.overlap_checked = True
            d.related_capability = self._overlap(d)
        if not d.missing() and s.duplicate:
            return self._duplicate_question(s)
        question = self._next_question(s)
        if question:
            lead = ("Got it. " if len([m for m in s.messages if m["role"] == "user"]) > 1 else
                    "Thanks. I'll draft this as one feature as we go; if it's bigger than that, say so. ")
            return lead + question
        return self._summary(s)

    def _respond_live(self, s: IntakeSession) -> str:
        names = self.lane_names(s.lanes)
        tail = (
            "You help a product person turn an idea into a feature intent for an agentic SDLC. "
            "Collect: product_id (one of " + json.dumps(names) + "), problem, users, success_metric, and a short title. "
            "Ask one question at a time, in plain language. Do not invent facts. "
            + (f"This intent changes what people see, so once those are in, also ask about the experience, one "
               f"question at a time: {', '.join(k for k, _ in EXPERIENCE)} (which screen or flow; the one thing a "
               f"person must do or see first; where they use it; the look, following the product's current design "
               f"unless they want something else). When the person isn't sure, offer two or three concrete "
               f"directions as options to pick from. If they say you decide, set that part, or the rest, to "
               f"{json.dumps(DELEGATED)}. " if s.draft.ui else "")
            + ("First decide the request's size: 'feature' (one change), 'initiative' (several features toward one "
               "goal you can measure) or 'product' (new users or a new business). " if not s.draft.size else "")
            + 'Reply with JSON only: {"reply": str, "options": [short quick replies, at most 4, may be empty], '
            + ('"size": "feature|initiative|product", "size_why": "one short sentence", ' if not s.draft.size else "")
            + '"draft": {"product_id", "title", "problem", "users", "success_metric"'
            + (', "experience": {' + ", ".join(f'"{k}"' for k, _ in EXPERIENCE) + "}" if s.draft.ui else "")
            + "}}.\n\n"
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
        if not s.draft.size and s.draft.problem:
            size = str(data.get("size") or "").lower()
            s.draft.size_suggested, s.draft.size_why = size_of(s.draft.problem, self._named(s.draft.problem, s.draft.product_id))
            if size in SIZES:
                s.draft.size_suggested = size
                s.draft.size_why = str(data.get("size_why") or s.draft.size_why)[:300]
            if s.draft.size_suggested != "feature":
                return self._size_reply(s)
            s.draft.size = "feature"
        exp = (data.get("draft") or {}).get("experience")
        if s.draft.ui and isinstance(exp, dict):
            s.draft.experience = {**s.draft.experience, **{k: str(v).strip()[:500] for k, v in exp.items()
                                                           if k in dict(EXPERIENCE) and isinstance(v, str) and v.strip()}}
        if not [f for f in FIELDS if not getattr(s.draft, f)] and not s.ui_checked:
            s.ui_checked = True
            s.draft.ui = self.touches_ui(s.draft)
        s.options = [str(o)[:80] for o in (data.get("options") or []) if isinstance(o, str) and o.strip()][:4]
        if not s.draft.missing() and not s.overlap_checked:
            s.overlap_checked = True
            s.draft.related_capability = self._overlap(s.draft)
        if not s.draft.missing() and s.duplicate:
            s.options = []
            return self._duplicate_question(s)
        return str(data.get("reply") or "Could you say a bit more about what you need?")

    def update_draft(self, s: IntakeSession, changes: dict) -> None:
        if s.feature_id:
            raise ValueError("This intent was already submitted")
        if "ui" in changes:
            s.draft.ui, s.ui_checked = bool(changes.pop("ui")), True
        if "experience" in changes:
            exp = changes.pop("experience") or {}
            if not isinstance(exp, dict) or set(exp) - dict(EXPERIENCE).keys():
                raise ValueError(f"experience takes {', '.join(k for k, _ in EXPERIENCE)}")
            s.draft.experience = {**s.draft.experience, **{k: str(v).strip()[:500] for k, v in exp.items()}}
        if "size" in changes:
            self.set_size(s, str(changes.pop("size")))
            if s.awaiting == "size":
                s.awaiting = ""
        if "research" in changes:
            r = str(changes.pop("research") or "")
            if r not in ("", "quick", "thorough", "own"):
                raise ValueError("research is quick, thorough or own")
            s.draft.research = r
        if "horizon" in changes:
            h = str(changes.pop("horizon") or "")
            if h not in ("", "now", "next", "later", AGENT_PROPOSES):
                raise ValueError("horizon is now, next or later")
            s.draft.horizon = h
        if "org" in changes:
            org = str(changes.pop("org") or "").strip()
            if org and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]*", org):
                raise ValueError("A GitHub org is letters, digits and dashes")
            s.draft.org = org
        if "name" in changes:
            s.draft.name = str(changes.pop("name") or "").strip()[:120]
        if "budget_usd" in changes:
            s.draft.budget_usd = max(0.0, float(changes.pop("budget_usd") or 0))
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

"""Resume an agent's session, or hand off to a fresh one (the context design, C5 and C6).

A session is resumed by its id only for a quick retry of the same task by the same role and model:
the coder whose pull request failed CI, or whose review asked for changes, picks it up with
everything it already read. That needs its transcript, which the tool keeps for 24 hours in the
feature's artifact store (Valxel's side, never the customer's repos: a transcript can hold
untrusted text an agent read and, by mistake, a secret) and then deletes.

Everywhere else the next session starts fresh from a handoff document: what the last one did,
what's open, what it decided and why, the files that matter and what not to try again. It's small,
any model can read it, and a person can too.

The rule (`may_resume`): same task, role, model and runtime; within 24 hours; the last session under
70% of its window; its transcript still kept; and a runtime that can resume (Claude Code for now).
"""

from __future__ import annotations

import gzip
import re
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, Field

from .context import RESUME_FILL

RESUME_WINDOW_S = 24 * 3600
RESUMABLE_RUNTIMES = ("claude_code",)
_SAFE = re.compile(r"^[A-Za-z0-9_.-]+$")


class Handoff(BaseModel):
    """What a session leaves for the next one on the same task (agents/structured.CoderReply)."""

    done: list[str] = Field(default_factory=list, description="What this session finished")
    open: list[str] = Field(default_factory=list, description="What's left, most important first")
    decisions: list[str] = Field(default_factory=list, description="Choices made and why, one each")
    files: list[str] = Field(default_factory=list, description="The files that matter for what's left")
    avoid: list[str] = Field(default_factory=list, description="What was tried and didn't work: don't repeat it")

    def empty(self) -> bool:
        return not any((self.done, self.open, self.decisions, self.files, self.avoid))

    def markdown(self, title: str) -> str:
        parts = [f"# Handoff: {title}"]
        for name, items in (("Done", self.done), ("Open", self.open), ("Decisions", self.decisions),
                            ("Files that matter", self.files), ("Don't try again", self.avoid)):
            if items:
                parts.append(f"## {name}\n" + "\n".join(f"- {i}" for i in items))
        return "\n\n".join(parts) + "\n"


HANDOFF_INSTRUCTION = (
    "Fill in `handoff` for whoever works on this task next (maybe another model, maybe you without "
    "this conversation): what you finished, what's open, the decisions you made and why, the files "
    "that matter, and what you tried that didn't work. Short lines; leave it empty only if nothing "
    "is left to do.")


def picking_up(handoff_text: str) -> str:
    """The start of a fresh session's task, when an earlier one left a handoff."""
    from .agents import untrusted

    return ("\n\nAn earlier session worked on this task and left this handoff. Start from it: don't "
            "redo what's done, and don't repeat what it says didn't work.\n"
            + untrusted.wrap("handoff", handoff_text))


@dataclass(frozen=True)
class Decision:
    resume: bool
    why: str


def may_resume(prev: dict | None, *, task_id: str, role: str, model: str, runtime: str,
               transcripts: TranscriptStore | None = None, feature_id: str = "",
               now: float | None = None) -> Decision:
    """Whether the next session on `task_id` resumes `prev` (a session record) or starts fresh."""
    now = time.time() if now is None else now
    if not prev or not prev.get("session_id"):
        return Decision(False, "no earlier session")
    for key, want in (("task", task_id), ("role", role), ("model", model), ("runtime", runtime)):
        if prev.get(key) != want:
            return Decision(False, f"a different {key} ({prev.get(key) or 'none'} before, {want} now)")
    if runtime not in RESUMABLE_RUNTIMES:
        return Decision(False, f"{runtime} sessions aren't resumed yet")
    if now - float(prev.get("at") or 0) > RESUME_WINDOW_S:
        return Decision(False, "more than 24 hours since it ran")
    if float(prev.get("fill") or 0) >= RESUME_FILL:
        return Decision(False, f"it was {float(prev['fill']):.0%} full, at or past {RESUME_FILL:.0%}")
    if transcripts is not None and not transcripts.has(feature_id, prev["session_id"]):
        return Decision(False, "its transcript is gone")
    return Decision(True, "same task, role and model, recent, room left")


class TranscriptStore:
    """Session transcripts beside the feature's artifacts (<artifact root>/<feature>/sessions/),
    gzipped, for 24 hours. A transcript is everything one Claude Code session file holds."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def _dir(self, feature_id: str) -> Path:
        from .tenancy import thread_folder

        if not _SAFE.match(feature_id):
            raise ValueError(f"Unsafe feature id {feature_id!r}")
        return thread_folder(self.root, feature_id) / "sessions"

    def _path(self, feature_id: str, session_id: str) -> Path:
        if not _SAFE.match(session_id):
            raise ValueError(f"Unsafe session id {session_id!r}")
        return self._dir(feature_id) / f"{session_id}.jsonl.gz"

    def has(self, feature_id: str, session_id: str) -> bool:
        try:
            return self._path(feature_id, session_id).is_file()
        except ValueError:
            return False

    def keep(self, feature_id: str, session_id: str, transcript: Path) -> bool:
        """Copy a session's transcript in. False when there's none to copy."""
        if not session_id or not transcript.is_file():
            return False
        path = self._path(feature_id, session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        with transcript.open("rb") as src, gzip.open(path, "wb") as dst:
            shutil.copyfileobj(src, dst)
        return True

    def restore(self, feature_id: str, session_id: str, into: Path) -> Path:
        """Write the transcript to `into` (a file path) for the runtime to resume from."""
        into.parent.mkdir(parents=True, exist_ok=True)
        with gzip.open(self._path(feature_id, session_id), "rb") as src, into.open("wb") as dst:
            shutil.copyfileobj(src, dst)
        return into

    def expire(self, *, max_age_s: float = RESUME_WINDOW_S, now: float | None = None) -> int:
        """Delete transcripts older than a day; the watcher calls this. Returns how many went."""
        now = time.time() if now is None else now
        gone = 0
        for path in [*self.root.glob("*/*/sessions/*.jsonl.gz"), *self.root.glob("*/sessions/*.jsonl.gz")]:
            if now - path.stat().st_mtime > max_age_s:
                path.unlink(missing_ok=True)
                gone += 1
        return gone


# --- Claude Code's own files ----------------------------------------------------------------

def claude_projects(home: str) -> Path:
    """Where Claude Code keeps session transcripts for a home folder."""
    return Path(home) / ".claude" / "projects"


def claude_project_dir(home: str, cwd: str) -> Path:
    """The folder Claude Code keeps a working directory's sessions in: the path with every
    character other than a letter or digit as '-'."""
    return claude_projects(home) / re.sub(r"[^A-Za-z0-9]", "-", str(cwd))


def find_transcript(home: str, session_id: str) -> Path | None:
    if not home or not session_id or not _SAFE.match(session_id):
        return None
    return next(iter(sorted(claude_projects(home).glob(f"*/{session_id}.jsonl"))), None)

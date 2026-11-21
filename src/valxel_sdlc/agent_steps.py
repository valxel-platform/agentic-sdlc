"""What an agent is doing, as it does it (T-112): short steps on the feature's page while a session runs.

Claude Code's messages become steps ("Editing src/queue.py", "Running the tests", "Tests: 3 failing,
40 passing", "Pushing the branch"). Only what's safe to show goes out: file paths relative to the
checkout and what kind of thing is happening, never file contents or the text of a command (which
can carry a token). A session in a sandbox sends its steps back line by line with its outcome
(session_runner's streamed /run); the worker publishes each as a `feature.progress` live event and
keeps the last few, so a page opened mid-session shows where it is. The live event names only the
feature: the steps themselves are read through the API, which checks the reader's access.

Codex sessions report no steps yet: `codex exec` gives the tool its outcome only.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from collections.abc import Callable
from typing import Any

log = logging.getLogger(__name__)
Step = Callable[[str], None]

KEEP = 15  # steps kept per feature for a page that opens mid-session
_TESTS = re.compile(r"\b(pytest|vitest|jest|go test|cargo test|mvn( -\S+)* (verify|test)|gradle(w)? test|"
                    r"npm (run )?test|pnpm (run )?test|yarn test|tox|dotnet test|rspec|phpunit)\b")
_FAILED = re.compile(r"(\d+) (failed|failing|failures?)\b", re.IGNORECASE)
_PASSED = re.compile(r"(\d+) (passed|passing)\b", re.IGNORECASE)


def _rel(path: str) -> str:
    """A path as the checkout knows it: the sandbox's own folders cut off."""
    p = str(path or "")
    for marker in ("/repo/", "/work/", "/checkout/"):
        if marker in p:
            p = p.split(marker, 1)[1]
    p = re.sub(r"^/tmp/[^/]+/", "", p)
    return p[-120:]


def command_step(cmd: str) -> tuple[str, str]:
    """(the step, its kind) for a shell command, without the command's text."""
    c = " ".join(str(cmd or "").split())
    if _TESTS.search(c):
        return "Running the tests", "tests"
    if re.search(r"\bgit push\b", c):
        return "Pushing the branch", "git"
    if re.search(r"\bgit commit\b", c):
        return "Committing", "git"
    if re.search(r"\b(npm|pnpm|yarn) (ci|install|add)\b|\bpip install\b|\buv (sync|add)\b|\bgo mod\b", c):
        return "Installing packages", "install"
    if re.search(r"\b(ruff|eslint|prettier|black|mypy|tsc|golangci-lint|checkstyle)\b", c):
        return "Checking style and types", "lint"
    if re.search(r"\b(npm run build|vite build|go build|mvn( -\S+)* package|gradle(w)? build|cargo build)\b", c):
        return "Building", "build"
    first = c.split(" ", 1)[0].rsplit("/", 1)[-1] if c else ""
    return (f"Running {first}" if re.fullmatch(r"[\w.-]{1,30}", first or "") else "Running a command"), "other"


def tool_step(name: str, args: dict) -> tuple[str, str]:
    a = args if isinstance(args, dict) else {}
    path = _rel(a.get("file_path") or a.get("path") or a.get("notebook_path") or "")
    if name in ("Read", "NotebookRead"):
        return (f"Reading {path}" if path else "Reading"), "read"
    if name in ("Edit", "MultiEdit", "NotebookEdit"):
        return (f"Editing {path}" if path else "Editing"), "edit"
    if name == "Write":
        return (f"Writing {path}" if path else "Writing"), "edit"
    if name in ("Grep", "Glob", "LS"):
        return "Searching the code", "search"
    if name == "Bash":
        return command_step(a.get("command") or "")
    if name == "Skill":
        return f"Using the {str(a.get('skill') or a.get('name') or 'a')[:40]} skill", "skill"
    if name in ("WebFetch", "WebSearch"):
        return "Looking something up", "web"
    if name in ("Task", "Agent"):
        return "Handing part of it to a helper", "agent"
    return "", ""


def test_summary(text: str) -> str:
    """'Tests: 3 failing, 40 passing' from a test run's output, or ''."""
    t = str(text or "")[-4000:]
    failed = sum(int(m.group(1)) for m in _FAILED.finditer(t))
    passed = max((int(m.group(1)) for m in _PASSED.finditer(t)), default=0)
    if not failed and not passed:
        return ""
    if failed:
        return f"Tests: {failed} failing" + (f", {passed} passing" if passed else "")
    return f"Tests: all {passed} passing"


class Reader:
    """Turns Claude Code SDK messages into steps, remembering which tool calls ran the tests."""

    def __init__(self, on_step: Step) -> None:
        self.on_step, self.tests, self.last = on_step, set(), ""

    def _say(self, step: str) -> None:
        if step and step != self.last:  # a run of reads of one file is one step
            self.last = step
            try:
                self.on_step(step)
            except Exception:  # progress never stops the session
                log.debug("a step wasn't passed on", exc_info=True)

    def feed(self, message: Any) -> None:
        for block in getattr(message, "content", None) or []:
            name = getattr(block, "name", None)
            if name is not None and hasattr(block, "input"):  # a tool use
                step, kind = tool_step(name, getattr(block, "input", {}) or {})
                if kind == "tests":
                    self.tests.add(getattr(block, "id", ""))
                self._say(step)
            elif hasattr(block, "tool_use_id") and getattr(block, "tool_use_id", "") in self.tests:
                content = getattr(block, "content", "")
                text = content if isinstance(content, str) else json.dumps(content, default=str)
                self._say(test_summary(text))


# --------------------------------------------------------------------------- the worker's side

_sink: Callable[[dict], None] | None = None
_recent: Callable[[str, list[dict]], None] | None = None
_lock = threading.Lock()
_kept: dict[str, list[dict]] = {}
_saved_at: dict[str, float] = {}


def configure(publish: Callable[[dict], None] | None, keep: Callable[[str, list[dict]], None] | None = None) -> None:
    """The engine's live-event publisher, and where the last steps are kept for other processes."""
    global _sink, _recent
    _sink, _recent = publish, keep


def reporter(feature_id: str, phase: str, role: str, repo: str = "") -> Step:
    """A step callback for one session: publishes each step and keeps the last KEEP per feature."""
    def step(text: str) -> None:
        ev = {"type": "feature.progress", "feature_id": feature_id, "phase": phase, "role": role,
              "repo": repo.rsplit("/", 1)[-1], "step": text[:160],
              "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        with _lock:
            kept = (_kept.get(feature_id, []) + [ev])[-KEEP:]
            _kept[feature_id] = kept
            due = time.monotonic() - _saved_at.get(feature_id, 0) >= 3
            if due:
                _saved_at[feature_id] = time.monotonic()
        if _sink:
            try:  # the live stream reaches every signed-in user: it says only that there's a new step,
                # and the page reads the steps where its access is checked (/api/features/{id}/steps)
                _sink({"type": "feature.progress", "feature_id": feature_id})
            except Exception:  # progress never stops the session
                log.debug("a step wasn't passed on", exc_info=True)
        if due and _recent:
            try:
                _recent(feature_id, kept)
            except Exception:  # kept again on the next step
                log.debug("steps not kept", exc_info=True)

    return step


def recent(feature_id: str) -> list[dict]:
    with _lock:
        return list(_kept.get(feature_id, []))


# --------------------------------------------------------------------------- the stream's lines

def line(obj: dict) -> bytes:
    return (json.dumps(obj, separators=(",", ":"), default=str) + "\n").encode()


def read(lines, on_step: Step | None) -> dict:
    """A streamed /run: {"step": ...} lines, then {"outcome": {...}}. The outcome, or a failure."""
    outcome = None
    for raw in lines:
        raw = raw.decode() if isinstance(raw, bytes) else raw
        if not raw.strip():
            continue
        try:
            msg = json.loads(raw)
        except ValueError:
            continue
        if "step" in msg and on_step:
            try:
                on_step(str(msg["step"]))
            except Exception:  # progress never stops the session
                log.debug("a step wasn't passed on", exc_info=True)
        elif "outcome" in msg:
            outcome = msg["outcome"]
    return outcome if isinstance(outcome, dict) else {"ok": False, "error": "StreamEnded", "transient": True}

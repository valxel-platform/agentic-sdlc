"""Secrets never reach a feature's stored history (tracker T-121).

A feature's checkpoints are every step's state: what agents wrote back (summaries, handoffs, review
findings, notes) and what CI reported (failing tests' messages). Any of those can repeat a
credential the agent or the build saw: a token in a log line, a key in a test fixture. Checkpoints
last as long as the feature, and for regulated lanes they're archived for six years under Object
Lock (checkpoint_archive.py), where nothing can take a secret back out.

So the checkpointer's serializer scrubs them before anything is written: the credential shapes the
platform hands out or a product is likely to hold (the LLM gateway's redaction list, plus whole
private-key blocks), each replaced by `x`s of the same length. Same length, because the serialized
checkpoint is msgpack, whose strings carry their byte lengths: a same-length substitution leaves it
valid. The stored value then reads `ghs_xxxxxxxx…`, which says what was there without what it was.

`scrub` does the same for text, for any other store that keeps what an agent or a build wrote.
"""

from __future__ import annotations

import re
from typing import Any

# The LLM gateway's list (ss-llm-gateway redaction.py), kept in step by tests/test_secret_scrub.py's
# corpus, plus a private key's whole block rather than its first line.
_PATTERN = (
    r"vgs_[A-Za-z0-9_-]{20,}"  # LLM gateway session tokens
    r"|gh[pousr]_[A-Za-z0-9]{36,}"  # GitHub: personal, OAuth, user-to-server, installation, refresh
    r"|github_pat_[A-Za-z0-9_]{22,}"  # GitHub fine-grained personal tokens
    r"|(?:AKIA|ASIA)[0-9A-Z]{16}"  # AWS access key ids: long-lived and temporary
    r"|(?:IQoJb3JpZ2lu|FwoGZXIvYXdz)[A-Za-z0-9/+=]{80,}"  # AWS session tokens
    r"|sk-ant-[A-Za-z0-9_-]{20,}"  # Anthropic
    r"|sk-(?:proj-|svcacct-|admin-)?[A-Za-z0-9_-]{32,}"  # OpenAI
    r"|-----BEGIN [A-Z ]*PRIVATE KEY-----(?:.|\n|\\n){0,8000}?-----END [A-Z ]*PRIVATE KEY-----"
    r"|xox[baprs]-[A-Za-z0-9-]{10,}"  # Slack
    r"|eyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"  # JWTs
)
_TEXT = re.compile(_PATTERN)
_BYTES = re.compile(_PATTERN.encode(), re.DOTALL)
_KEEP = 4  # characters of the shape left in place, so the record still says what it was


def _blank(n: int, head: bytes) -> bytes:
    return head[:_KEEP] + b"x" * (n - min(n, _KEEP))


def scrub_bytes(data: bytes) -> bytes:
    """Every secret in serialized data, as `x`s of the same length (the structure stays valid)."""
    return _BYTES.sub(lambda m: _blank(len(m.group()), m.group()), data)


def scrub(text: str) -> str:
    """Every secret in text, as `x`s of the same length."""
    return _TEXT.sub(lambda m: m.group()[:_KEEP] + "x" * max(0, len(m.group()) - _KEEP), text)


def found(text: str) -> list[str]:
    """The secrets text holds (for tests and checks)."""
    return [m.group() for m in _TEXT.finditer(text)]


class ScrubbingSerde:
    """The checkpointer's serializer, scrubbing secrets on the way to storage. Reading is unchanged."""

    def __init__(self, inner: Any = None) -> None:
        if inner is None:
            from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

            inner = JsonPlusSerializer()
        self.inner = inner

    def dumps_typed(self, obj: Any) -> tuple[str, bytes]:
        kind, data = self.inner.dumps_typed(obj)
        return kind, scrub_bytes(bytes(data)) if isinstance(data, (bytes, bytearray)) else data

    def loads_typed(self, data: tuple[str, bytes]) -> Any:
        return self.inner.loads_typed(data)

    def __getattr__(self, name: str) -> Any:  # anything else the checkpointer asks of its serializer
        return getattr(self.inner, name)


# ------------------------------------------------------------------------- code excerpts (T-121)
# Agents' notes may quote code: a failing test's message, a finding that cites a line. A feature's
# history keeps a short excerpt of it, never a whole file: each fenced block keeps its first
# EXCERPT_LINES lines, and each note at most NOTE_CHARS characters. Documents are not notes and are
# kept whole (they are what the product's record is made of).

NOTE_CHANNELS = frozenset({"diaries", "findings", "escalations", "events", "ci_failures", "task_results",
                           "reviews", "reason"})
EXCERPT_LINES = 20
NOTE_CHARS = 4000
_FENCE = re.compile(r"```[^\n]*\n(.*?)(?:```|\Z)", re.DOTALL)


def excerpt(text: str) -> str:
    """The text with each fenced code block cut to its first EXCERPT_LINES lines, and the whole to NOTE_CHARS."""
    def cut(m: re.Match) -> str:
        lines = m.group(1).splitlines()
        if len(lines) <= EXCERPT_LINES:
            return m.group(0)
        head = m.group(0)[: m.start(1) - m.start(0)]
        return head + "\n".join(lines[:EXCERPT_LINES]) + f"\n… ({len(lines) - EXCERPT_LINES} more lines not kept)\n```"

    out = _FENCE.sub(cut, text)
    return out if len(out) <= NOTE_CHARS else out[:NOTE_CHARS] + f" … ({len(out) - NOTE_CHARS} characters not kept)"


def _excerpts(value: Any) -> Any:
    if isinstance(value, str):
        return excerpt(value)
    if isinstance(value, dict):
        return {k: _excerpts(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return type(value)(_excerpts(v) for v in value)
    return value


def _note(channel: str, value: Any) -> Any:
    return _excerpts(value) if channel in NOTE_CHANNELS else value


def keep_excerpts(saver: Any) -> Any:
    """`saver`, storing only excerpts of code in agents' notes: checkpoints and pending writes alike."""
    put, put_writes = saver.put, saver.put_writes
    aput, aput_writes = getattr(saver, "aput", None), getattr(saver, "aput_writes", None)

    def trimmed(checkpoint):
        values = checkpoint.get("channel_values") or {}
        return {**checkpoint, "channel_values": {k: _note(k, v) for k, v in values.items()}}

    def put_(config, checkpoint, metadata, new_versions):
        return put(config, trimmed(checkpoint), metadata, new_versions)

    def put_writes_(config, writes, task_id, task_path=""):
        return put_writes(config, [(c, _note(c, v)) for c, v in writes], task_id, task_path)

    saver.put, saver.put_writes = put_, put_writes_
    if aput is not None:
        async def aput_(config, checkpoint, metadata, new_versions):
            return await aput(config, trimmed(checkpoint), metadata, new_versions)
        saver.aput = aput_
    if aput_writes is not None:
        async def aput_writes_(config, writes, task_id, task_path=""):
            return await aput_writes(config, [(c, _note(c, v)) for c, v in writes], task_id, task_path)
        saver.aput_writes = aput_writes_
    return saver

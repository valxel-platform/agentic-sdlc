"""The events a graph run records (`state["events"]`): one shape for every seam's graphs.

The delivery pipeline, the roadmap's graphs and ops (gradual releases, incidents) all write them,
so they're the core's (ADR 0005).
"""

from __future__ import annotations

from datetime import UTC, datetime


def now() -> str:
    return datetime.now(UTC).isoformat()


def event(node: str, kind: str, detail: str = "", **extra) -> dict:
    return {"at": now(), "node": node, "kind": kind, "detail": detail, **extra}

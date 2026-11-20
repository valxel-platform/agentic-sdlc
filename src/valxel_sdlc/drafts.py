"""The product's repos hold the record; the tool's copies are working drafts (T-89).

While a feature is in flight the tool keeps every version of its documents (artifacts.py): agents
read them, people compare versions, gates send them back. What each gate approves goes to the
product's governance repo (records.py, T-54), and from then on that's the copy of record. Once the
feature is finished (released or stopped) and VALXEL_DRAFT_RETENTION_DAYS have passed (30 by
default), the daily job deletes the tool's copies:

  a gate's documents (every version, the rejected ones too)   when that gate's record pull request
                                                              merged; if it's still open or failed,
                                                              the tool may hold the only approved
                                                              copy, so they stay until it merges
  documents of a gate never approved (a stopped feature)       never the product's record: deleted
  build handoffs (HANDOFF-<task>)                             the tool's own working notes: deleted

After that, people opening a recorded document see it read from the product's repo (`load`).
Prompts, agent transcripts and evaluator notes never go to the product (records.py) and are
handled elsewhere (sessions.py expires transcripts after a day).
"""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime, timedelta

from .records import GATE_KINDS, NAMES

log = logging.getLogger(__name__)

DONE = ("done", "aborted")
NOTE = "drafts:removed:{}"
# Each kind of document, the gate whose record holds it, and its file there.
GATE_OF = {kind: gate for gate, kinds in GATE_KINDS.items() for kind in kinds} | {"PROTOTYPE": "g3", "MOCKUPS": "g3",
                                                                                      "SKETCH": "g1"}
RECORDED_AS = dict(NAMES) | {"PROTOTYPE": "prototype.html", "SKETCH": "sketch.html"}


def retention_days(env=None) -> int:
    try:
        return max(0, int((env if env is not None else os.environ).get("VALXEL_DRAFT_RETENTION_DAYS", "30")))
    except ValueError:
        return 30


def finished_at(state: dict) -> str:
    """When a finished feature finished (its last event), or "" while it's in flight."""
    if state.get("status") not in DONE:
        return ""
    events = state.get("events") or []
    return str(events[-1].get("at") or "") if events else ""


def recorded(state: dict) -> dict[str, bool]:
    """Per gate: True when its record pull request merged, False when it was approved but its record
    didn't merge (open, failed, or switched off). Gates never approved aren't in it."""
    out: dict[str, bool] = {}
    for e in state.get("events") or []:
        gate = e.get("node", "")
        if e.get("kind") == "approved" and gate in GATE_KINDS:
            out.setdefault(gate, False)
        elif e.get("kind") == "documents_recorded" and gate in GATE_KINDS:
            out[gate] = bool(e.get("merged")) or e.get("detail") == "Nothing to record"
        elif e.get("kind") == "documents_not_recorded" and gate in GATE_KINDS:
            out[gate] = False
    return out


def plan(state: dict) -> dict[str, list[str]]:
    """Which kinds may go now and which must stay, with why: {"remove": [...], "keep": [...]}."""
    done = recorded(state)
    remove, keep = [], []
    for kind in sorted(state.get("artifacts") or {}):
        gate = GATE_OF.get(kind)
        # the tool's own notes; a gate never approved (never the product's record); a merged record
        if kind.startswith("HANDOFF-") or gate is None or gate not in done or done[gate]:
            remove.append(kind)
        else:
            keep.append(kind)
    return {"remove": remove, "keep": keep}


def due(state: dict, *, now: datetime | None = None, env=None) -> bool:
    at = finished_at(state)
    if not at:
        return False
    now = now or datetime.now(UTC)
    return datetime.fromisoformat(at) <= now - timedelta(days=retention_days(env))


def remove(services, cluster, state: dict, *, now: datetime | None = None, env=None) -> dict | None:
    """Delete a finished feature's drafts once they're due. Returns what was done, or None."""
    fid = state.get("feature_id", "")
    if not fid or not due(state, now=now, env=env):
        return None
    note = cluster.get(NOTE.format(fid)) or {}
    p = plan(state)
    todo = [k for k in p["remove"] if k not in (note.get("removed") or [])]
    if not todo and note.get("keep") == p["keep"]:
        return None
    files = sum(services.artifacts.drop(fid, kind) for kind in todo)
    out = {"at": (now or datetime.now(UTC)).isoformat(), "removed": sorted(set(note.get("removed") or []) | set(todo)),
           "keep": p["keep"], "files": int(note.get("files") or 0) + files}
    cluster.put(NOTE.format(fid), out)
    if files:
        log.info("Deleted %d draft file(s) of %s: %s%s", files, fid, ", ".join(todo),
                 f"; kept {', '.join(p['keep'])} (its record isn't merged)" if p["keep"] else "")
    return out


def load(services, state: dict, kind: str) -> str:
    """A feature's document: the tool's copy while there is one, else the product's record of it."""
    ref = (state.get("artifacts") or {}).get(kind)
    if not ref:
        raise KeyError(f"{state.get('feature_id')} has no {kind} artifact")
    try:
        return services.artifacts.load(ref)
    except (FileNotFoundError, OSError):
        pass
    text = from_record(services, state, kind)
    if text is None:
        raise FileNotFoundError(f"{state.get('feature_id')}'s {kind} is no longer kept here or in the product's repo")
    return text


def from_record(services, state: dict, kind: str) -> str | None:
    name = RECORDED_AS.get(kind)
    if not name:
        return None
    try:
        repo = services.settings.entry(state["product_id"]).governance.full_name
        return services.github.get_file(repo=repo, path=f"features/{state['feature_id']}/{name}", ref="")
    except Exception as exc:  # noqa: BLE001 - unreachable: say it isn't here
        log.info("%s's %s not read from its record: %s", state.get("feature_id"), kind, exc)
        return None

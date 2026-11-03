"""The architecture record: what a product has decided, kept in its governance repo.

    architecture/
      overview.md                      the current design: context, building blocks, key flows
      decisions/ADR-<id>.md            one architecture decision record per file

ADRs use MADR's sections with YAML front matter, so people read them as documents and the tool
reads them as data:

    ---
    id: ADR-EMR-0001
    title: FHIR R4 at every system boundary
    status: accepted            # proposed | accepted | superseded | deprecated
    date: 2026-07-01
    repos: [vrixel-emr/emr-intake-api]
    supersedes: []
    superseded_by: ""
    feature: ""                 # the feature that made the decision, when the SDLC did
    ---
    ## Context
    ## Decision
    ## Consequences
"""

from __future__ import annotations

import re

import yaml

STATUSES = ("proposed", "accepted", "superseded", "deprecated")
_FRONT = re.compile(r"\A---\n(.*?)\n---\n?(.*)\Z", re.S)
ADR_ID = re.compile(r"\bADR-[A-Z0-9]+(?:-[A-Z0-9]+)*\b")


def _section(body: str, heading: str) -> str:
    m = re.search(rf"^## {re.escape(heading)}\s*$(.*?)(?=^## |\Z)", body, re.M | re.S)
    return m.group(1).strip() if m else ""


def parse_adr(text: str, path: str = "") -> dict | None:
    """An ADR file as data, or None when it has no front matter with an id."""
    m = _FRONT.match(text.replace("\r\n", "\n"))
    if not m:
        return None
    try:
        meta = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError:
        return None
    if not isinstance(meta, dict) or not meta.get("id"):
        return None
    body = m.group(2)
    decision = _section(body, "Decision")
    status = str(meta.get("status") or "proposed").lower()
    return {
        "id": str(meta["id"]),
        "title": str(meta.get("title") or ""),
        "status": status if status in STATUSES else "proposed",
        "date": str(meta.get("date") or ""),
        "repos": [str(r) for r in meta.get("repos") or []],
        "supersedes": [str(r) for r in meta.get("supersedes") or []],
        "superseded_by": str(meta.get("superseded_by") or ""),
        "feature": str(meta.get("feature") or ""),
        "decision": decision.split("\n\n", 1)[0][:600],
        "path": path,
    }


def render_adr(adr: dict, *, context: str = "", decision: str = "", consequences: str = "") -> str:
    meta = {k: adr.get(k) for k in ("id", "title", "status", "date", "repos", "supersedes", "superseded_by",
                                    "feature")}
    meta = {k: v for k, v in meta.items() if v not in (None, "", [])}
    front = yaml.safe_dump(meta, sort_keys=False, allow_unicode=True).strip()
    return (f"---\n{front}\n---\n# {adr['id']}: {adr['title']}\n\n"
            f"## Context\n\n{context.strip() or 'See the feature HLD.'}\n\n"
            f"## Decision\n\n{decision.strip() or adr['title']}\n\n"
            f"## Consequences\n\n{consequences.strip() or 'See the feature HLD.'}\n")


def mark_superseded(text: str, by: str) -> str:
    """Set status: superseded and superseded_by on an ADR file, keeping everything else."""
    m = _FRONT.match(text.replace("\r\n", "\n"))
    if not m:
        return text
    meta = yaml.safe_load(m.group(1)) or {}
    meta["status"], meta["superseded_by"] = "superseded", by
    return f"---\n{yaml.safe_dump(meta, sort_keys=False, allow_unicode=True).strip()}\n---\n{m.group(2)}"

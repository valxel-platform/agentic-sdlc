"""API contracts as the repos hold them: the OpenAPI document's version and operations."""

from __future__ import annotations

import re

import yaml

METHODS = ("get", "put", "post", "delete", "patch", "head", "options")
_SEMVER = re.compile(r"^v?(\d+)\.(\d+)(?:\.(\d+))?")


def parse_openapi(text: str) -> dict:
    doc = yaml.safe_load(text) or {}
    if not isinstance(doc, dict) or not (doc.get("openapi") or doc.get("swagger")):
        raise ValueError("not an OpenAPI document")
    ops = sorted(f"{m.upper()} {p}" for p, item in (doc.get("paths") or {}).items() if isinstance(item, dict)
                 for m in item if m in METHODS)
    return {"title": str((doc.get("info") or {}).get("title") or ""),
            "version": str((doc.get("info") or {}).get("version") or ""), "operations": ops}


def semver(v: str) -> tuple[int, int, int] | None:
    m = _SEMVER.match((v or "").strip())
    return (int(m.group(1)), int(m.group(2)), int(m.group(3) or 0)) if m else None

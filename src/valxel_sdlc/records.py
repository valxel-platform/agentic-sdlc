"""The product keeps its own record of every approved feature document (tracker T-54).

When a gate is approved, the documents it approved go to the product's governance repo, under
`features/<feature>/`, with who approved them and when:

  G1  discovery (OPP) and the files attached to the intent
  G2  the PRD
  G3  the UX design and its mockups (one HTML page per screen)
  G4  the HLD
  G5  the plan the build followed
  G6  the release record

Each gate is one pull request from `valxel/records/<feature>-<gate>`, which the tool merges at once:
the gate was the review. If the repo's rules want another review, the pull request stays open for
the product's people to merge, and the feature goes on. A failure here never holds a feature up; it
is an event on the feature (`documents_not_recorded`, with the reason) for a person to follow up.

Only approved documents go: never agent transcripts, prompts, tool output or evaluator notes, which
stay with the tool. Attachments of PHI or payments products are listed by name and digest, not
committed (git keeps every version forever; the product's own document store is the place for them),
and so are attachments above VALXEL_RECORD_ATTACHMENT_MB (default 5).
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone

GATE_KINDS = {"g1": ("OPP",), "g2": ("PRD",), "g3": ("UX",), "g4": ("HLD",), "g5": ("PLAN",), "g6": ("REL",)}
NAMES = {"OPP": "discovery.md", "PRD": "prd.md", "UX": "ux.md", "HLD": "hld.md", "PLAN": "plan.md", "REL": "release.md"}
SENSITIVE = ("phi", "payments")


def enabled(env=None) -> bool:
    return (env if env is not None else os.environ).get("VALXEL_RECORD_DOCUMENTS", "on").lower() not in (
        "0", "off", "false", "no")


def _attachment_limit(env=None) -> int:
    try:
        return int(float((env if env is not None else os.environ).get("VALXEL_RECORD_ATTACHMENT_MB", "5")) * 2**20)
    except ValueError:
        return 5 * 2**20


def files_for(services, state: dict, gate: str, *, env=None) -> tuple[dict[str, str | bytes], list[dict]]:
    """path -> content for what this gate approved, and what was left out (and why)."""
    from .mockups import KIND as MOCKUPS

    base = f"features/{state['feature_id']}"
    arts = state.get("artifacts") or {}
    files: dict[str, str | bytes] = {}
    left: list[dict] = []
    for kind in GATE_KINDS.get(gate, ()):
        ref = arts.get(kind)
        if ref:
            files[f"{base}/{NAMES[kind]}"] = services.artifacts.load(ref)
    if gate == "g3" and arts.get(MOCKUPS):
        try:
            pages = json.loads(services.artifacts.load(arts[MOCKUPS]))
        except (ValueError, OSError):
            pages = {}
        for sid, html in sorted(pages.items()):
            if isinstance(html, str) and sid.replace("-", "").replace("_", "").isalnum():
                files[f"{base}/mockups/{sid}.html"] = html
    if gate == "g1":
        sensitive = str(state.get("data_class", "")).lower() in SENSITIVE
        limit = _attachment_limit(env)
        store = getattr(services, "attachments", None)
        for meta in state.get("attachments") or []:
            why = ("the product's data class is " + str(state.get("data_class"))) if sensitive else (
                "larger than the limit" if int(meta.get("size", 0)) > limit else "")
            if not why and store is not None:
                try:
                    files[f"{base}/attachments/{meta['id']}-{meta['name']}"] = store.path(
                        meta, feature_id=state["feature_id"]).read_bytes()
                    continue
                except (OSError, ValueError):
                    why = "the file is no longer there"
            left.append({"name": meta.get("name", ""), "sha256": meta.get("sha256", ""),
                         "size": meta.get("size", 0), "why": why or "no attachment store"})
    return files, left


def approval_record(state: dict, gate: str, approvers: str, files: dict, left: list[dict], at: str) -> str:
    arts = state.get("artifacts") or {}
    lines = [f"# {state['feature_id']}: {gate.upper()} approved", "",
             f"- Feature: {state.get('title') or state['feature_id']}",
             f"- Product: {state.get('product_id', '')}",
             f"- Approved by: {approvers or 'unknown'}",
             f"- At: {at}", "", "## Documents", ""]
    for kind in GATE_KINDS.get(gate, ()):
        ref = arts.get(kind)
        if ref:
            lines.append(f"- {NAMES[kind]}: {ref['id']} (version {ref['version']})")
    base = f"features/{state['feature_id']}/"
    lines += [f"- {p[len(base):]}" for p in sorted(files) if "/mockups/" in p or "/attachments/" in p]
    if left:
        lines += ["", "## Not committed", "",
                  "Kept with the tool, not in this repo; the digest identifies the file.", ""]
        lines += [f"- {x['name']} (sha256 {x['sha256']}, {x['size']} bytes): {x['why']}" for x in left]
    return "\n".join(lines) + "\n"


def record(services, state: dict, gate: str, approvers: str, *, env=None) -> dict:
    """Commit what `gate` approved to the product's governance repo. Returns an event's fields."""
    gh = services.github
    entry = services.settings.entry(state["product_id"])
    repo = entry.governance.full_name
    files, left = files_for(services, state, gate, env=env)
    if not files and not left:
        return {"status": "nothing"}
    at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    path = f"features/{state['feature_id']}/approvals/{gate}.md"
    files[path] = approval_record(state, gate, approvers, files, left, at)
    branch = f"valxel/records/{state['feature_id']}-{gate}"
    message = f"{state['feature_id']}: documents approved at {gate.upper()}"
    for p, content in files.items():
        if isinstance(content, bytes):
            gh.put_bytes(repo=repo, branch=branch, path=p, content=content, message=message)
        else:
            gh.put_file(repo=repo, branch=branch, path=p, content=content, message=message)
    body = (f"{gate.upper()} of {state['feature_id']} was approved by {approvers or 'unknown'} in the Valxel SDLC; "
            "these are the documents it approved, for the product's own record. Agent transcripts are not "
            "part of it.")
    pr = gh.open_pr(repo=repo, branch=branch, title=message, body=body, files=sorted(files),
                    labels=["valxel", "records"])
    try:
        merged = gh.merge(repo=repo, number=pr["number"], actor=approvers or "valxel", sha=pr.get("head_sha", ""))
        status = "merged" if merged.get("state") == "merged" or merged.get("merged") else "open"
    except Exception as exc:  # noqa: BLE001 — the repo's rules may want another review; the PR waits for it
        status, pr = "open", {**pr, "why": str(exc)[:300]}
    return {"status": status, "repo": repo, "url": pr.get("url", ""), "files": len(files),
            "left_out": len(left), **({"why": pr["why"]} if "why" in pr else {})}

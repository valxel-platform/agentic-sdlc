"""The roadmap record: a product's goals, initiatives and releases, kept in its governance repo.

    roadmap/
      goals.yaml                    outcomes for a period: id, title, measure, period, metric (how the
                                    tool reads it: vrixel_sdlc.outcomes), parent (later, for a
                                    portfolio goal the product goal rolls up to)
      initiatives/INI-CLN-1.md      one approved initiative per file
      releases/REL-CLN-1.md         one release per file: a launch group or a release train

Initiatives and releases are Markdown with YAML front matter, like ADRs: people read documents,
the tool reads data. They change through pull requests, which the pipeline opens when an
initiative is approved at its gate and when a release is created or completes.

Scope is the product today. Ids carry the product (INI-CLN-1), goals may name a `parent`, and the
`scope` field is reserved, so a portfolio level can sit above products later without a rewrite.
"""

from __future__ import annotations

import re

import yaml

HORIZONS = ("now", "next", "later")
INITIATIVE_STATUSES = ("proposed", "approved", "active", "done", "dropped")
RELEASE_MODELS = ("launch", "train")
RELEASE_STATUSES = ("planned", "ready", "approved", "shipped", "cancelled")
_FRONT = re.compile(r"\A---\n(.*?)\n---\n?(.*)\Z", re.S)


def _split(text: str) -> tuple[dict, str] | None:
    m = _FRONT.match((text or "").replace("\r\n", "\n"))
    if not m:
        return None
    try:
        meta = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError:
        return None
    return (meta, m.group(2)) if isinstance(meta, dict) and meta.get("id") else None


def section(body: str, heading: str) -> str:
    m = re.search(rf"^## {re.escape(heading)}\s*$(.*?)(?=^## |\Z)", body, re.M | re.S)
    return m.group(1).strip() if m else ""


def candidates(body: str) -> list[dict]:
    """'## Candidate features' bullets: '- Title: what it does'."""
    out = []
    for line in section(body, "Candidate features").splitlines():
        line = line.strip()
        if line.startswith("- "):
            title, _, desc = line[2:].partition(":")
            if title.strip():
                out.append({"title": title.strip(), "description": desc.strip()})
    return out


def parse_goals(text: str) -> list[dict]:
    raw = yaml.safe_load(text or "") or {}
    out = []
    for g in raw.get("goals") or []:
        if isinstance(g, dict) and g.get("id"):
            out.append({"id": str(g["id"]), "title": str(g.get("title") or ""), "measure": str(g.get("measure") or ""),
                        "period": str(g.get("period") or ""), "parent": str(g.get("parent") or ""),
                        "metric": g.get("metric")})  # checked by vrixel_sdlc.outcomes.parse_metric
    return out


def parse_initiative(text: str, path: str = "") -> dict | None:
    sp = _split(text)
    if not sp:
        return None
    meta, body = sp
    horizon = str(meta.get("horizon") or "later").lower()
    status = str(meta.get("status") or "proposed").lower()
    model = str(meta.get("release_model") or "launch").lower()
    return {
        "id": str(meta["id"]), "title": str(meta.get("title") or ""), "goal": str(meta.get("goal") or ""),
        "horizon": horizon if horizon in HORIZONS else "later",
        "status": status if status in INITIATIVE_STATUSES else "proposed",
        "budget_usd": float(meta.get("budget_usd") or 0), "lanes": [str(x) for x in meta.get("lanes") or []],
        "release_model": model if model in RELEASE_MODELS else "launch",
        "measure": str(meta.get("measure") or ""), "owner": str(meta.get("owner") or ""),
        "scope": str(meta.get("scope") or "product"),
        "approved_by": str(meta.get("approved_by") or ""), "approved_at": str(meta.get("approved_at") or ""),
        # When the product council reviews whether its goal is moving (outcome loop, step 5).
        "review_by": str(meta.get("review_by") or ""),
        "outcome": section(body, "Outcome"), "candidates": candidates(body), "path": path,
        # What the plan bet on, checked at its review (vrixel_sdlc.research).
        "assumptions": section(body, "Assumptions"),
    }


def render_initiative(i: dict, *, body: str = "") -> str:
    meta = {k: i.get(k) for k in ("id", "title", "goal", "horizon", "status", "budget_usd", "lanes", "release_model",
                                  "measure", "owner", "scope", "approved_by", "approved_at", "review_by")}
    meta = {k: v for k, v in meta.items() if v not in (None, "", [])}
    front = yaml.safe_dump(meta, sort_keys=False, allow_unicode=True).strip()
    if not body:
        body = (f"## Outcome\n\n{i.get('outcome') or i['title']}\n\n## Candidate features\n\n"
                + "\n".join(f"- {c['title']}: {c.get('description', '')}".rstrip(": ") for c in i.get("candidates", []))
                + "\n")
    return f"---\n{front}\n---\n# {i['id']}: {i['title']}\n\n{body.strip()}\n"


def parse_release(text: str, path: str = "") -> dict | None:
    sp = _split(text)
    if not sp:
        return None
    meta, body = sp
    model = str(meta.get("model") or "launch").lower()
    status = str(meta.get("status") or "planned").lower()
    return {
        "id": str(meta["id"]), "title": str(meta.get("title") or ""),
        "model": model if model in RELEASE_MODELS else "launch",
        "status": status if status in RELEASE_STATUSES else "planned",
        "target_date": str(meta.get("target_date") or ""), "features": [str(f) for f in meta.get("features") or []],
        "initiative": str(meta.get("initiative") or ""), "approved_by": str(meta.get("approved_by") or ""),
        "notes": section(body, "Release notes"), "path": path,
    }


def render_release(r: dict, *, notes: str = "", evidence: str = "") -> str:
    meta = {k: r.get(k) for k in ("id", "title", "model", "status", "target_date", "initiative", "features",
                                  "approved_by", "approved_at")}
    meta = {k: v for k, v in meta.items() if v not in (None, "", [])}
    front = yaml.safe_dump(meta, sort_keys=False, allow_unicode=True).strip()
    kind = "Launch group" if r["model"] == "launch" else "Release train"
    return (f"---\n{front}\n---\n# {r['id']}: {r['title']}\n\n{kind}.\n\n"
            f"## Release notes\n\n{notes.strip() or 'Written when the release is approved.'}\n"
            + (f"\n## Evidence\n\n{evidence.strip()}\n" if evidence.strip() else ""))


def update_initiative(text: str, changes: dict, review_line: str = "") -> str:
    """The initiative file with front-matter fields changed and a line added under '## Reviews'.
    Everything else in it stays as written."""
    m = _FRONT.match((text or "").replace("\r\n", "\n"))
    if not m:
        raise ValueError("The initiative record has no front matter")
    meta = yaml.safe_load(m.group(1)) or {}
    meta.update({k: v for k, v in changes.items() if v not in (None, "")})
    body = m.group(2).rstrip("\n")
    if review_line:
        if re.search(r"^## Reviews\s*$", body, re.M):
            head, _, rest = body.partition("## Reviews")
            nxt = re.search(r"^## ", rest, re.M)
            if nxt:
                body = head + "## Reviews" + rest[:nxt.start()].rstrip("\n") + f"\n- {review_line}\n\n" + rest[nxt.start():]
            else:
                body = head + "## Reviews" + rest.rstrip("\n") + f"\n- {review_line}"
        else:
            body += f"\n\n## Reviews\n\n- {review_line}"
    front = yaml.safe_dump(meta, sort_keys=False, allow_unicode=True).strip()
    return f"---\n{front}\n---\n{body}\n"

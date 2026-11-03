"""After a production release, propose the feature's decisions to the product's architecture
record. The tool opens a pull request in the governance repo; people merge it. Until it merges,
the capability catalog carries the decisions, so other features already see them."""

from __future__ import annotations

from datetime import date

from .record import mark_superseded, parse_adr, render_adr


def _overview_with_change(overview: str, line: str, product_name: str) -> str:
    text = overview.rstrip() or f"# {product_name}: architecture overview\n\nWrite the current design here."
    if "\n## Change log" not in f"\n{text}":
        text += "\n\n## Change log\n"
    return text.rstrip() + f"\n{line}\n"


def propose_record_update(services, state: dict, hld: str, design: dict) -> dict | None:
    """Writes ADR files (accepted), marks superseded ones and logs the change in the overview, on
    one branch per feature; returns the pull request, or None when there is nothing to record."""
    from ..agents.parse import parse_affected_repos
    from ..graph.pipeline import _hld_adrs

    adrs = design.get("adrs") or _hld_adrs(hld)
    if not adrs:
        return None
    cfg = services.knowledge.cfg.settings if getattr(services, "knowledge", None) else {}
    base = (cfg.get("architecture_dir") or "architecture").strip("/")
    gov = services.settings.entry(state["product_id"]).governance
    src = services.governance.source
    sha = src.resolve(gov.org, gov.repo, gov.ref)
    existing = {}
    for path in src.list(gov.org, gov.repo, f"{base}/decisions", sha):
        adr = parse_adr(src.read(gov.org, gov.repo, path, sha) or "", path)
        if adr:
            existing[adr["id"]] = adr
    today = date.today().isoformat()
    repos = [r["name"] for r in parse_affected_repos(hld)]
    contracts = [c for c in design.get("contracts") or [] if c.get("next") and c["next"] != c.get("current")]
    consequences = "\n".join(f"- {c['repo']}: API {c['current']} -> {c['next']}"
                             + (" (breaking; see the HLD's migration plan)" if c.get("breaking") else "")
                             for c in contracts)
    files: dict[str, str] = {}
    for a in adrs:
        if a["id"] in existing:
            continue
        files[f"{base}/decisions/{a['id']}.md"] = render_adr(
            {"id": a["id"], "title": a["title"].split(" (", 1)[0][:120], "status": "accepted", "date": today,
             "repos": repos, "supersedes": [a["supersedes"]] if a.get("supersedes") else [],
             "feature": state["feature_id"]},
            context=f"{state['title']}. {state.get('description', '')}".strip(),
            decision=a["title"], consequences=consequences)
        old = existing.get(a.get("supersedes") or "")
        if old and old["status"] != "superseded":
            files[old["path"]] = mark_superseded(src.read(gov.org, gov.repo, old["path"], sha) or "", a["id"])
    if not files:
        return None
    overview_path = f"{base}/overview.md"
    overview = src.read(gov.org, gov.repo, overview_path, sha, optional=True) or ""
    line = (f"- {today} {state['feature_id']}: {state['title']} ({', '.join(a['id'] for a in adrs)}"
            + (f"; {', '.join(c['repo'].split('/', 1)[-1] + ' ' + c['next'] for c in contracts)}" if contracts else "")
            + ")")
    files[overview_path] = _overview_with_change(overview, line, state.get("product_name") or state["product_id"])

    branch = f"vrixel/{state['feature_id']}-architecture"
    gh = services.github
    for path, content in files.items():
        gh.put_file(repo=gov.full_name, branch=branch, path=path, content=content,
                    message=f"{state['feature_id']}: record {path.rsplit('/', 1)[-1]}")
    body = (f"Released {state['feature_id']} ({state['title']}). This records the decisions G4 approved, so "
            "future features build on them.\n\n"
            + "\n".join(f"- {a['id']}: {a['title']}" + (f" (supersedes {a['supersedes']})" if a.get("supersedes") else "")
                        for a in adrs)
            + ("\n\nAPI changes:\n" + consequences if consequences else "")
            + "\n\nReview the wording, then merge. Until then the capability catalog carries these decisions.")
    pr = gh.open_pr(repo=gov.full_name, branch=branch, title=f"{state['feature_id']}: record architecture decisions",
                    body=body, files=sorted(files), labels=["vrixel", "architecture"])
    return {"url": pr["url"], "files": sorted(files)}

"""Changes to a product's governance repo go in by pull request: write files to a branch, open (or
update) its PR. Delivery's design record, ops' incident follow-ups and the roadmap all use it, so it's
the core's (ADR 0005).
"""

from __future__ import annotations


def governance_pr(services, product_id: str, *, branch: str, files: dict[str, str], title: str, body: str,
                  labels: list[str]) -> dict:
    """Write files to a branch of the product's governance repo and open (or update) its PR."""
    gov = services.settings.entry(product_id).governance
    gh = services.github
    for path, content in files.items():
        gh.put_file(repo=gov.full_name, branch=branch, path=path, content=content, message=f"{title}: {path}")
    pr = gh.open_pr(repo=gov.full_name, branch=branch, title=title, body=body, files=sorted(files), labels=labels)
    return {"url": pr["url"], "number": pr["number"], "files": sorted(files)}

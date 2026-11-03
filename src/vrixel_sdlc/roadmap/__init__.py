"""Product roadmap: goals, initiatives and releases above features.

Git is the record (roadmap/ in each product's governance repo, changed by pull request); the tool
keeps the live state of initiatives and releases it is running (drafting, at their gate, waiting
for features) and computes progress from the features themselves. Trackers get a copy.
"""

from __future__ import annotations

from .record import parse_goals, parse_initiative, parse_release

ROADMAP_DIR = "roadmap"


def read_roadmap(services, product_id: str) -> dict:
    """The product's roadmap record at its governance repo's current head."""
    gov = services.settings.entry(product_id).governance
    src = services.governance.source
    sha = src.resolve(gov.org, gov.repo, gov.ref)
    goals = parse_goals(src.read(gov.org, gov.repo, f"{ROADMAP_DIR}/goals.yaml", sha, optional=True) or "")
    initiatives, releases = [], []
    for path in src.list(gov.org, gov.repo, f"{ROADMAP_DIR}/initiatives", sha):
        i = parse_initiative(src.read(gov.org, gov.repo, path, sha) or "", path) if path.endswith(".md") else None
        if i:
            initiatives.append(i)
    for path in src.list(gov.org, gov.repo, f"{ROADMAP_DIR}/releases", sha):
        r = parse_release(src.read(gov.org, gov.repo, path, sha) or "", path) if path.endswith(".md") else None
        if r:
            releases.append(r)
    return {"repo": gov.full_name, "sha": sha, "goals": goals, "initiatives": initiatives, "releases": releases}


def governance_pr(services, product_id: str, *, branch: str, files: dict[str, str], title: str, body: str,
                  labels: list[str]) -> dict:
    """Write files to a branch of the product's governance repo and open (or update) its PR."""
    gov = services.settings.entry(product_id).governance
    gh = services.github
    for path, content in files.items():
        gh.put_file(repo=gov.full_name, branch=branch, path=path, content=content, message=f"{title}: {path}")
    pr = gh.open_pr(repo=gov.full_name, branch=branch, title=title, body=body, files=sorted(files), labels=labels)
    return {"url": pr["url"], "number": pr["number"], "files": sorted(files)}

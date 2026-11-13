"""Where design knowledge comes from. Each source returns plain data for one section of the pack.
Sources are named in config/knowledge.yaml, so a product line can swap one (say, read ADRs from
Backstage TechDocs or Confluence) without touching the pipeline.

  architecture   the product's architecture record in its governance repo (Git, the system of record)
  contracts      each API repo's OpenAPI document, as merged on its default branch
  capabilities   the capability catalog: what is live, and which designs are approved and in flight
  features       the features the pipeline has run for the product, and where each got to
"""

from __future__ import annotations

from typing import Protocol

from .contracts import parse_openapi
from .record import parse_adr


class KnowledgeSource(Protocol):
    def collect(self, snap, cfg: dict) -> dict: ...


class GovernanceArchitecture:
    """architecture/overview.md and architecture/decisions/*.md, read at the governance repo's
    current head (not the feature's pin): design knowledge must be current, while the constitution
    stays pinned. The SHA read is returned so G4 records exactly what reviewers saw."""

    def __init__(self, services) -> None:
        self.services = services

    def _lane(self, lane, cfg: dict) -> dict:
        src = self.services.governance.source
        gov = self.services.settings.entry(lane.product_id).governance
        sha = src.resolve(gov.org, gov.repo, gov.ref)
        base = cfg.get("architecture_dir", "architecture").strip("/")
        overview = src.read(gov.org, gov.repo, f"{base}/overview.md", sha, optional=True) or ""
        adrs = []
        for path in src.list(gov.org, gov.repo, f"{base}/decisions", sha):
            if path.endswith(".md"):
                adr = parse_adr(src.read(gov.org, gov.repo, path, sha) or "", path)
                if adr:
                    adrs.append({**adr, "lane": lane.product_id})
        return {"repo": gov.full_name, "sha": sha, "overview": overview, "adrs": sorted(adrs, key=lambda a: a["id"])}

    def collect(self, snap, cfg: dict) -> dict:
        own = self._lane(snap.product, cfg)
        deps = {}
        for lane_id, dep in snap.dependencies.items():
            try:
                deps[lane_id] = self._lane(dep.product, cfg)
            except Exception as exc:  # a dependency lane without a record must not block this product
                deps[lane_id] = {"error": f"{type(exc).__name__}: {exc}", "adrs": [], "overview": ""}
        return {**own, "dependencies": deps}


class RepoOpenApi:
    """The OpenAPI document of every API-owning repo (providers and dependency-lane providers),
    read from its default branch: the contract as merged, not as remembered."""

    API_ROLES = ("provider",)

    def __init__(self, services) -> None:
        self.services = services

    def collect(self, snap, cfg: dict) -> dict:
        paths = cfg.get("openapi_paths") or ["api/openapi.yaml"]
        out = {}
        for lane in snap.lanes().values():
            for r in lane.repos:
                if r.role not in self.API_ROLES:
                    continue
                entry: dict = {"repo": r.name, "lane": lane.product_id}
                try:
                    for p in paths:
                        text = self.services.github.get_file(repo=r.name, path=p, ref="")
                        if text:
                            entry.update(parse_openapi(text), path=p)
                            break
                    else:
                        entry["missing"] = True
                except Exception as exc:
                    entry["error"] = f"{type(exc).__name__}: {exc}"
                out[r.name] = entry
        return {"apis": out}


class ProductFeatures:
    """Every feature the pipeline has run for this product: released, in progress, stopped."""

    def __init__(self, services) -> None:
        self.services = services

    def collect(self, snap, cfg: dict) -> dict:
        lookup = getattr(self.services, "features", None)
        return {"features": lookup(snap.product.product_id) if callable(lookup) else []}


class ProductRoadmap:
    """Goals, initiatives and releases: the record in Git plus initiatives the engine is running."""

    def __init__(self, services) -> None:
        self.services = services

    def collect(self, snap, cfg: dict) -> dict:
        from ..roadmap import read_roadmap

        pid = snap.product.product_id
        record = read_roadmap(self.services, pid)
        lookup = getattr(self.services, "initiatives", None)
        running = lookup(pid) if callable(lookup) else []
        known = {i["id"] for i in record["initiatives"]}
        initiatives = record["initiatives"] + [i for i in running if i["id"] not in known and i["status"] == "approved"]
        return {"goals": record["goals"], "initiatives": initiatives, "releases": record["releases"]}


class RecentIncidents:
    """Recent production incidents and their likely causes: designs should not repeat them."""

    def __init__(self, services) -> None:
        self.services = services

    def collect(self, snap, cfg: dict) -> dict:
        lookup = getattr(self.services, "incidents", None)
        return {"incidents": lookup(snap.product.product_id) if callable(lookup) else []}


class CatalogCapabilities:
    def __init__(self, services) -> None:
        self.services = services

    def collect(self, snap, cfg: dict) -> dict:
        caps = self.services.catalog.lookup(snap.product.product_id)
        return {"live": [c for c in caps if c.get("status", "live") == "live"],
                "in_design": [c for c in caps if c.get("status") == "in_design"]}

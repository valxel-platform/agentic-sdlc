"""Design knowledge: what a product has already built and decided, given to every agent and
checked before G4, so new work builds on the existing design instead of reinventing it.

Git is the system of record (decisions and contracts live next to the code and change through
reviewed pull requests); the catalog says what is live and in flight; work trackers and diagram
tools are views, written by the pipeline, never read back as truth.
"""

from __future__ import annotations

import importlib
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .contracts import semver
from .record import ADR_ID

__all__ = ["KnowledgeConfig", "Knowledge", "load_knowledge", "render", "design_review", "semver"]


@dataclass(frozen=True)
class KnowledgeConfig:
    sources: dict = field(default_factory=dict)  # name -> "module:Class"
    settings: dict = field(default_factory=dict)  # architecture_dir, openapi_paths, max_chars ...


def load_knowledge(path: Path) -> KnowledgeConfig | None:
    if not Path(path).exists():
        return None
    raw = yaml.safe_load(Path(path).read_text()) or {}
    return KnowledgeConfig(sources=dict(raw.get("sources") or {}), settings=dict(raw.get("settings") or {}))


class Knowledge:
    def __init__(self, cfg: KnowledgeConfig, services) -> None:
        self.cfg = cfg
        self.sources = {}
        for name, dotted in cfg.sources.items():
            module, _, cls = dotted.partition(":")
            self.sources[name] = getattr(importlib.import_module(module), cls)(services)

    def collect(self, snap) -> dict:
        """Plain data (it travels in checkpointed phase state). A source that fails reports its
        error instead of failing the phase; G4 shows what could not be read."""
        pack: dict = {"errors": {}}
        for name, source in self.sources.items():
            try:
                pack[name] = source.collect(snap, self.cfg.settings)
            except Exception as exc:
                pack[name] = {}
                pack["errors"][name] = f"{type(exc).__name__}: {exc}"
        return pack


# --------------------------------------------------------------------------- prompt


def _cut(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: max(0, limit - 40)].rstrip() + "\n[... truncated]"


def render(pack: dict, max_chars: int = 12000) -> str:
    """The pack as a prompt section: decisions and contracts first (they constrain), then the
    overview and the catalog. Bounded so a large record never crowds out the feature."""
    if not pack:
        return ""
    arch, apis, caps = pack.get("architecture") or {}, (pack.get("contracts") or {}).get("apis") or {}, pack.get("capabilities") or {}
    out = ["# Existing design (build on it; supersede a decision only with a new ADR that says so)"]
    if arch.get("repo"):
        out.append(f"Architecture record: {arch['repo']}@{str(arch.get('sha', ''))[:7]}")
    adrs = arch.get("adrs") or []
    if adrs:
        out.append("\n## Decisions (ADRs)")
        for a in sorted(adrs, key=lambda a: (a["status"] != "accepted", a["id"])):
            tail = f"; superseded by {a['superseded_by']}" if a.get("superseded_by") else ""
            repos = f" [{', '.join(r.split('/', 1)[-1] for r in a['repos'])}]" if a.get("repos") else ""
            out.append(f"- {a['id']} ({a['status']}{tail}): {a['title']}{repos}"
                       + (f"\n  {a['decision']}" if a["status"] == "accepted" and a.get("decision") else ""))
    for lane, dep in (arch.get("dependencies") or {}).items():
        accepted = [a for a in dep.get("adrs") or [] if a["status"] == "accepted"]
        if accepted:
            out.append(f"\n## Decisions of the {lane} lane this product depends on")
            out += [f"- {a['id']}: {a['title']}" for a in accepted]
    if apis:
        out.append("\n## API contracts (as merged on each repo's default branch)")
        for name, api in sorted(apis.items()):
            if api.get("version"):
                ops = ", ".join(api["operations"][:25]) + (" ..." if len(api["operations"]) > 25 else "")
                out.append(f"- {name}: {api.get('title') or 'API'} {api['version']} ({api['path']}): {ops}")
            else:
                out.append(f"- {name}: no OpenAPI document found" if api.get("missing") else
                           f"- {name}: contract unreadable ({api.get('error', 'unknown error')})")
    if caps.get("live") or caps.get("in_design"):
        out.append("\n## Capabilities")
        out += [f"- {c['id']} (live): {c['name']}" for c in caps.get("live", [])]
        for c in caps.get("in_design", []):
            changes = "; ".join(f"{x['repo'].split('/', 1)[-1]} -> {x['next']}" for x in c.get("contracts") or [])
            out.append(f"- {c['id']} (approved design, in flight: {c.get('feature', '')}): {c['name']}"
                       + (f"; changing {changes}" if changes else ""))
    feats = (pack.get("features") or {}).get("features") or []
    if feats:
        out.append("\n## Features of this product (don't redo one; extend it)")
        for f in feats[-40:]:
            where = {"released": "released", "stopped": "stopped"}.get(f["status"], f"in progress, at {f.get('stage') or '?'}")
            out.append(f"- {f['feature_id']} ({where}): {f['title']}")
    if arch.get("overview"):
        out.append("\n## Architecture overview\n" + arch["overview"].strip())
    return _cut("\n".join(out), max_chars) + "\n"


# --------------------------------------------------------------------------- design review


def _contract_lines(hld: str) -> dict[str, str]:
    from ..agents.parse import bullets, section

    out = {}
    for b in bullets(section(hld, "Contracts")):
        name = b.split(":", 1)[0].strip()
        out[name] = b
    return out


_ARROW = re.compile(r"(v?\d+\.\d+(?:\.\d+)?)\s*(?:->|→)\s*(v?\d+\.\d+(?:\.\d+)?)")


def design_review(hld: str, pack: dict, feature_id: str = "") -> dict:
    """How an HLD relates to the existing design, for the evaluator and for G4 reviewers.

    findings  problems the architect must fix before G4 (deterministic)
    builds_on / supersedes / contracts / related / in_flight  what G4 shows
    """
    from ..agents.parse import bullets, parse_affected_repos, section

    arch = pack.get("architecture") or {}
    adrs = {a["id"]: a for a in arch.get("adrs") or []}
    for dep in (arch.get("dependencies") or {}).values():
        adrs.update({a["id"]: a for a in dep.get("adrs") or []})
    apis = (pack.get("contracts") or {}).get("apis") or {}
    affected = {r["name"] for r in parse_affected_repos(hld)}
    findings: list[str] = []

    # 1. The decisions it builds on must exist and be current.
    builds_on = []
    cited = bullets(section(hld, "Builds on"))
    accepted = [a for a in adrs.values() if a["status"] == "accepted"]
    if accepted and not cited:
        findings.append("HLD has no 'Builds on' section: list the existing ADRs this design relies on "
                        "(or '- None: <why>')")
    for b in cited:
        ids = ADR_ID.findall(b)
        if not ids and not b.lower().startswith("none"):
            findings.append(f"'Builds on' entry {b!r} names no ADR")
        for i in ids:
            a = adrs.get(i)
            if not a:
                findings.append(f"Builds on {i}, which is not in the architecture record")
            elif a["status"] == "superseded":
                findings.append(f"Builds on {i}, which was superseded by {a['superseded_by'] or 'a later ADR'}; "
                                "build on the current decision or supersede it explicitly")
            elif a["status"] != "accepted":
                findings.append(f"Builds on {i}, which is {a['status']}, not accepted")
            if a:
                builds_on.append({k: a[k] for k in ("id", "title", "status", "lane")})

    # 2. Overriding a decision is explicit: a new ADR that says "Supersedes ADR-x".
    supersedes = []
    for line in bullets(section(hld, "ADRs")):
        m = re.search(r"[Ss]upersedes\s+(ADR-[A-Z0-9-]+)", line)
        if not m:
            continue
        old = m.group(1).rstrip("-")
        new = (ADR_ID.findall(line) or [""])[0]
        a = adrs.get(old)
        if not a:
            findings.append(f"{new or 'An ADR'} supersedes {old}, which is not in the architecture record")
        elif a["status"] != "accepted":
            findings.append(f"{new or 'An ADR'} supersedes {old}, which is already {a['status']}")
        supersedes.append({"adr": new, "supersedes": old, "title": a["title"] if a else ""})

    # 3. Contract changes start from the version actually merged, and breaking ones need a migration.
    lines = _contract_lines(hld)
    contracts = []
    for repo in sorted(affected & set(apis)):
        api = apis[repo]
        current = api.get("version", "")
        line = lines.get(repo)
        if not current:
            contracts.append({"repo": repo, "current": "", "next": "", "breaking": False,
                              "note": "no OpenAPI document" if api.get("missing") else api.get("error", "")})
            continue
        if not line:
            findings.append(f"HLD changes {repo} but its Contracts section doesn't say what happens to its API "
                            f"(currently {current}); write '{repo}: {current} -> <next>, <change>' or 'unchanged'")
            continue
        if "unchanged" in line.lower():
            contracts.append({"repo": repo, "current": current, "next": current, "breaking": False, "note": line})
            continue
        m = _ARROW.search(line)
        if not m:
            findings.append(f"Contract change for {repo} must state the versions: '{current} -> <next>'")
            continue
        frm, to = m.group(1), m.group(2)
        if semver(frm) != semver(current):
            findings.append(f"HLD assumes {repo} is at {frm}, but its OpenAPI on the default branch is {current}")
        a, b = semver(current), semver(to)
        breaking = bool(a and b and b[0] > a[0])
        if a and b and b <= a:
            findings.append(f"Next version {to} of {repo} is not after the current {current}")
        if breaking and not section(hld, "Migration"):
            findings.append(f"{repo} {current} -> {to} is a breaking change: add a Migration section "
                            "(expand, migrate consumers, then contract) (VX-4)")
        contracts.append({"repo": repo, "current": current, "next": to, "breaking": breaking, "note": line})

    # 4. For reviewers: decisions on the repos this design touches that it doesn't mention.
    mentioned = {x["id"] for x in builds_on} | {s["supersedes"] for s in supersedes}
    related = [{k: a[k] for k in ("id", "title", "status")} for a in accepted
               if set(a["repos"]) & affected and a["id"] not in mentioned]

    # 5. Other approved designs in flight that change the same APIs.
    in_flight = []
    for c in (pack.get("capabilities") or {}).get("in_design", []):
        if c.get("feature") == feature_id:
            continue
        clash = sorted({x["repo"] for x in c.get("contracts") or []} & {x["repo"] for x in contracts})
        if clash:
            in_flight.append({"feature": c.get("feature"), "title": c.get("name"), "repos": clash,
                              "contracts": c.get("contracts")})

    return {"findings": findings, "builds_on": builds_on, "supersedes": supersedes, "contracts": contracts,
            "related": related, "in_flight": in_flight, "record": {"repo": arch.get("repo"), "sha": arch.get("sha")},
            "errors": dict(pack.get("errors") or {})}

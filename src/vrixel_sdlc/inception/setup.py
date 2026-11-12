"""What the product gate's approval creates: the product's governance record and its registry entry,
each as a pull request, so the platform owners review the setup before it takes effect.

    <org>/<prefix>governance     product.yaml, constitution.md, context.md, CODEOWNERS,
                                 architecture/brief.md, architecture/product-architecture.md,
                                 roadmap/goals.yaml, roadmap/initiatives/<PID>-INI-<n>.md
    the tool's config repo       config/registry.yaml gains the product's line

Once both merge and the tool reloads its config, the product is like any other: the Roadmap page
shows its initiatives with their candidate features to start, and each feature creates the repos
the product architecture names when its design passes G4. An existing product gets one pull
request to its own governance repo instead (the architecture, the brief and its roadmap).

Every step is safe to repeat; a failure is recorded and the rest carries on.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone

import yaml

from ..outcomes import stub_from_measure
from ..roadmap.record import render_initiative
from . import Vision, formats

TEAMS = {"g1": "product-owners", "g2": "product-owners", "g3": "design", "g4": "architects",
         "g5": "tech-leads", "security": "security", "g6": "release-managers"}
BRANCH = "vrixel/inception"


def _today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def ids(brief: str) -> dict:
    p = formats.product(brief)
    return {"product_id": p["id"], "org": p["org"], "prefix": p["prefix"], "budget_usd": p["budget_usd"],
            "data_class": formats.data_class(brief)[0] or "general",
            "governance_repo": f"{p['org']}/{p['prefix']}governance"}


def product_yaml(vision: Vision, brief: str) -> str:
    from .. import capacity, environments

    i = ids(brief)
    tier = capacity.tier(brief)[0] or 3
    org, pid = i["org"], i["product_id"]
    approvers = {gate: [f"{org}/{team}"] for gate, team in TEAMS.items()}
    approvers["traceability"] = [f"{org}/product-owners", f"{org}/qa"]
    doc = {
        "product_id": pid, "name": vision.name, "constitution": "constitution.md", "context": "context.md",
        "depends_on": [],
        "runtime": {"openchoreo_project": pid.lower(), "pipeline": ["dev", "staging", "production"],
                    **environments.runtime_entry(pid, tier)},
        # A first-year envelope spread over its features: the per-feature ceiling starts at a tenth of it.
        "budget_usd": max(50, round(i["budget_usd"] / 10)),
        "repos": [],
        "repo_prefix": i["prefix"],  # the first features propose the repos the architecture names
        "approvers": approvers,
        "high_risk_paths": ["src/auth/", "migrations/"],
    }
    head = (f"# {vision.name} governance manifest, read by the Vrixel SDLC tool.\n"
            "# Written from the product brief approved at the product gate; changes are pull requests.\n"
            "# Repos are added here as features create them (the product architecture lists them).\n")
    return head + yaml.safe_dump(doc, sort_keys=False, allow_unicode=True)


def constitution_md(vision: Vision, brief: str) -> str:
    i = ids(brief)
    pid = i["product_id"]
    articles = [f"- **{pid}-1** Every goal in roadmap/goals.yaml is measured as its brief says, and the measure is "
                "reported after each release that moves it. (Policy)"]
    if i["data_class"] in ("phi", "payments"):
        articles.append(f"- **{pid}-2** {i['data_class'].upper() if i['data_class'] == 'phi' else 'Payment'} data is "
                        "read and changed only by the components the product architecture names, and every read or "
                        "change is audited. (Policy, Test)")
    for n, c in enumerate(formats.bullets(brief, "Compliance"), len(articles) + 1):
        if not c.lower().startswith("none"):
            articles.append(f"- **{pid}-{n}** {c} (Policy)")
    return (f"---\nname: {pid.lower()}\nversion: 0.1.0\n---\n# {vision.name} constitution\n\n"
            "Starter draft from the product brief. The product council adds and changes articles by pull request.\n\n"
            + "\n".join(articles) + "\n")


def context_md(vision: Vision, brief: str, architecture: str) -> str:
    from ..agents.parse import section

    users = "\n".join(f"- {u['id']}: {u['who']}" for u in formats.users(brief))
    comps = "\n".join(f"- {c['name']} ({c['role']}): {c['does']}" for c in formats.components(architecture) if c.get("name"))
    return (f"# {vision.name}\n\n{section(brief, 'Problem')}\n\n## Users\n\n{users}\n\n## Components\n\n{comps}\n\n"
            "The product architecture (architecture/product-architecture.md) is the reference for every feature's "
            "design; a design that departs from it proposes an amendment with an ADR.\n")


def goals_yaml(brief: str) -> tuple[str, dict]:
    pid = ids(brief)["product_id"]
    rows, mapping = [], {}
    for n, g in enumerate([g for g in formats.goals(brief) if g.get("id")], 1):
        gid = f"GOAL-{pid}-{n}"
        mapping[g["id"]] = gid
        # Entered by hand until someone wires a query (vrixel_sdlc.outcomes): measurable from day one.
        rows.append({"id": gid, "title": g["goal"], "measure": g["measure"], "period": "",
                     "metric": stub_from_measure(g["measure"])})
    return yaml.safe_dump({"goals": rows}, sort_keys=False, allow_unicode=True), mapping


def initiative_files(brief: str, roadmap: str, approved_by: str) -> dict[str, str]:
    pid = ids(brief)["product_id"]
    _, goal_ids = goals_yaml(brief)
    out = {}
    for n, ini in enumerate([i for i in formats.initiatives(roadmap) if i.get("id")], 1):
        iid = f"{pid}-INI-{n}"
        goals = [goal_ids.get(g, g) for g in ini["goals"]]
        record = {"id": iid, "title": ini["title"], "goal": goals[0] if goals else "", "horizon": ini["horizon"],
                  "status": "approved", "budget_usd": ini["budget_usd"], "lanes": [pid], "release_model": "launch",
                  "owner": "", "scope": "product", "approved_by": approved_by, "approved_at": _today()}
        after = ", ".join(f"{pid}-INI-{a.split('-')[-1]}" for a in ini["after"]) or "none"
        body = (f"## Outcome\n\nMoves {', '.join(goals) or 'the product goals'}. Comes after: {after}.\n\n"
                "## Candidate features\n\n"
                + "\n".join(f"- {f['title']}: {f['does']} (builds {', '.join(f['builds'])})"
                            for f in ini["features"] if f.get("title")) + "\n")
        out[f"roadmap/initiatives/{iid}.md"] = render_initiative(record, body=body)
    return out


def capacity_file(brief: str, architecture: str, plat: dict | None) -> str:
    """runtime/capacity.yaml from the approved documents ('' when the plan has no components)."""
    from .. import capacity

    plat = plat or {}
    p = capacity.plan(brief, architecture, plat.get("capacity"), plat.get("catalog") or {},
                      data_class=formats.data_class(brief)[0] or "general")
    return capacity.capacity_yaml(p, ids(brief)["product_id"]) if p["components"] else ""


def governance_files(vision: Vision, brief: str, architecture: str, roadmap: str, approved_by: str,
                     research: str = "", plat: dict | None = None) -> dict[str, str]:
    i = ids(brief)
    goals, _ = goals_yaml(brief)
    sized = capacity_file(brief, architecture, plat)
    files = {
        **({"runtime/capacity.yaml": sized} if sized else {}),
        "architecture/brief.md": brief,
        "architecture/product-architecture.md": architecture,
        "roadmap/goals.yaml": goals,
        **initiative_files(brief, roadmap, approved_by),
        **({"architecture/research.md": research} if research else {}),
    }
    if not vision.existing:
        from ..design_record import starter_files

        files |= starter_files()  # the design record starts here; releases add screens and components
        files |= {"product.yaml": product_yaml(vision, brief), "constitution.md": constitution_md(vision, brief),
                  "context.md": context_md(vision, brief, architecture),
                  "CODEOWNERS": f"* @{i['org']}/product-owners\n"}
    return files


def registry_line(brief: str) -> str:
    i = ids(brief)
    return (f"  {i['product_id']}: {{ kind: product, org: {i['org']}, repo: {i['prefix']}governance, ref: main, "
            f"data_class: {i['data_class']}, workspace: wrkspc-{i['product_id'].lower()} }}")


def add_to_registry(text: str, line: str) -> str:
    """config/registry.yaml with the product's line at the end of `products:` (unchanged if present)."""
    pid = line.strip().split(":", 1)[0]
    lines = text.rstrip("\n").splitlines()
    if any(ln.strip().startswith(f"{pid}:") for ln in lines):
        return text
    try:
        start = next(n for n, ln in enumerate(lines) if ln.rstrip() == "products:")
    except StopIteration:
        return text.rstrip("\n") + "\n\nproducts:\n" + line + "\n"
    end = start + 1
    while end < len(lines) and (lines[end].startswith(" ") or not lines[end].strip()):
        end += 1
    while end - 1 > start and not lines[end - 1].strip():
        end -= 1
    return "\n".join(lines[:end] + [line] + lines[end:]) + "\n"


def run(services, vision: Vision, *, brief: str, architecture: str, roadmap: str, approved_by: str,
        thread_id: str, config_repo: str | None = None, research: str = "") -> dict:
    """Open the pull requests that record the approved product. Returns what was done, step by step."""
    gh = services.github
    i = ids(brief)
    steps: list[dict] = []
    try:
        from .run import platform

        plat = platform(services)
    except Exception:
        plat = {}
    files = governance_files(vision, brief, architecture, roadmap, approved_by, research, plat)

    def step(name: str, fn) -> dict:
        try:
            out = {"step": name, "ok": True, **(fn() or {})}
        except Exception as exc:  # recorded; the rest still runs
            out = {"step": name, "ok": False, "error": f"{type(exc).__name__}: {exc}"}
        steps.append(out)
        return out

    if vision.existing:
        gov = services.settings.entry(vision.product_id).governance.full_name
    else:
        gov = i["governance_repo"]
        step("governance_repo", lambda: {"repo": gov, "result": gh.create_repo(
            repo=gov, template=None, private=False,
            description=f"{vision.name}: governance (product.yaml, constitution, roadmap, architecture)")})

    def governance_pr() -> dict:
        for path, content in files.items():
            gh.put_file(repo=gov, branch=BRANCH, path=path, content=content, message=f"{thread_id}: {path}")
        pr = gh.open_pr(repo=gov, branch=BRANCH, title=f"{thread_id}: {vision.name}", labels=["vrixel", "inception"],
                        files=sorted(files),
                        body=(f"The product brief, architecture and roadmap approved by the product council "
                              f"({approved_by}).\n\nMerge to record them; "
                              + ("the registry pull request makes the tool read this repo." if not vision.existing
                                 else "the roadmap's initiatives then appear on the Roadmap page.")))
        return {"url": pr["url"], "repo": gov, "files": sorted(files)}

    step("governance_pr", governance_pr)

    if not vision.existing:
        config_repo = config_repo if config_repo is not None else os.environ.get("VRIXEL_CONFIG_REPO", "")
        line = registry_line(brief)

        def registry_pr() -> dict:
            if not config_repo:
                return {"skipped": True, "line": line,
                        "why": "VRIXEL_CONFIG_REPO isn't set: add the line to config/registry.yaml yourself"}
            current = gh.get_file(repo=config_repo, path="config/registry.yaml", ref="") or ""
            gh.put_file(repo=config_repo, branch=f"vrixel/register-{i['product_id'].lower()}",
                        path="config/registry.yaml", content=add_to_registry(current, line),
                        message=f"Register {i['product_id']} ({thread_id})")
            pr = gh.open_pr(repo=config_repo, branch=f"vrixel/register-{i['product_id'].lower()}",
                            title=f"Register {i['product_id']}: {vision.name}", files=["config/registry.yaml"],
                            labels=["vrixel", "inception"],
                            body=(f"Adds {i['product_id']} to the registry, reading {gov}.\n\nMerge after the "
                                  "governance pull request; the tool picks it up when it next loads its config."))
            return {"url": pr["url"], "repo": config_repo, "line": line}

        step("registry_pr", registry_pr)
        from .. import capacity, environments

        tier = capacity.tier(brief)[0] or 3
        zones = int(((plat.get("capacity") or capacity.DEFAULTS)["tiers"].get(tier) or {}).get("zones", 2))
        template = ""
        try:
            p = services.settings.platform
            folder = services.settings.platform_resources_path.rsplit("/", 1)[0]
            template = services.governance.source.read(p.org, p.repo, f"{folder}/openchoreo/clusters.yaml",
                                                       plat.get("sha") or p.ref, optional=True) or ""
        except Exception:
            template = ""
        steps.extend(environments.setup(services, product_id=i["product_id"], tier=tier, approver=approved_by,
                                        template=template, zones=zones, thread_id=thread_id,
                                        awake=(plat.get("capacity") or capacity.DEFAULTS).get("awake")
                                        or environments.AWAKE))
        teams = sorted({f"{i['org']}/{t}" for t in (*TEAMS.values(), "qa")})
        steps.append({"step": "teams", "ok": True, "skipped": True, "teams": teams,
                      "why": "The approver teams product.yaml names; an org admin creates any that are missing"})
    return {"product_id": i["product_id"], "governance_repo": gov, "steps": steps,
            "ok": all(s["ok"] for s in steps)}

"""Generate docs/module-map.html: every module of the tool, its capability and layer, and how they import each other.

    uv run python scripts/module_map.py            # writes docs/module-map.html
    uv run python scripts/module_map.py --check    # fails if a module has no capability

The layers come from config/seams.yaml (valxel_sdlc.seams); the capabilities are CAPS below. A new
module goes into the capability it serves: tests/test_module_map.py fails until it does.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path

from valxel_sdlc import seams

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src" / "valxel_sdlc"
TEMPLATE = Path(__file__).with_name("module_map_template.html")
OUT = ROOT / "docs" / "module-map.html"
SKIP = ("standard_files",)  # managed files the tool copies into product repos, not the tool's own modules

# (layer, key, name, what it does, modules: exact names or "package.*"). An exact name wins over a
# "package.*" match, so ops.incident can sit in ops while the rest of ops.* sits in the core.
CAPS = [
    (
        "root",
        "front",
        "Front doors",
        "The HTTP API and web console, sign-in, live updates, and agents reached over A2A and MCP.",
        ["api", "api.*", "web", "web.*", "cli"],
    ),
    (
        "root",
        "engine",
        "Feature engine",
        "Starts and resumes every thread (feature, initiative, incident), keeps the gate inbox, records "
        "decisions, recovers dropped threads.",
        ["engine", "engine_parts", "engine_parts.*", "run_queue", "checkpoint_archive", "worksync"],
    ),
    (
        "root",
        "wiring",
        "Service wiring",
        "Builds the service container the graphs run with: gateway, stores, runtimes, integrations; strict "
        "mode; the watcher's jobs.",
        ["services", "strict", "watcher_jobs"],
    ),
    (
        "root",
        "selfeval",
        "Self-evaluation",
        "Replays the tool's own agents on real cases: config evals, coder and reviewer evals, injection suites,"
        " drift and health; and rehearses its own releases against a copy of its database.",
        ["evals", "code_evals", "code_eval_runner", "coder_injection", "injection", "drift", "health", "what_changed",
         "rehearsal"],
    ),
    (
        "root",
        "lite",
        "Lite hosting",
        "Runs a product's environments on Docker Compose (one or two VMs, or a laptop) through the OpenChoreo "
        "interface.",
        ["lite", "lite.*"],
    ),
    (
        "intake",
        "intake",
        "Guided intake",
        "The one front door for requests: the intake chat and the files people attach.",
        ["intake", "attachments"],
    ),
    (
        "intake",
        "inception",
        "Product inception",
        "From a vision to a brief, an architecture and a roadmap, then the product gate and the product's setup.",
        [
            "inception",
            "inception.checks",
            "inception.diagrams",
            "inception.graph",
            "inception.rubrics",
            "inception.run",
            "inception.setup",
            "inception.templates",
        ],
    ),
    (
        "delivery",
        "pipeline",
        "Pipeline and gates",
        "The feature pipeline: phase subgraphs, routing, the six human gates and their packets, pilot and "
        "approval confidence.",
        [
            "graph",
            "graph.pipeline",
            "graph.phase",
            "graph.routing",
            "graph.briefs",
            "graph.gates",
            "confidence",
            "fit",
            "pilot",
        ],
    ),
    (
        "delivery",
        "design",
        "Design previews",
        "What people judge before code: sketch at G1, mockups and the clickable prototype at G3, today's "
        "screens, previews.",
        ["sketch", "mockups", "prototype", "current_screens", "graph.prototype_steps", "preview", "screens"],
    ),
    (
        "delivery",
        "build",
        "Build, merge and release",
        "Change-set environments, builds and deploys, the merge train, quality suites, independent review, "
        "rollout to production.",
        [
            "graph.delivery",
            "graph.merge_train",
            "graph.rollout",
            "graph.infra",
            "changeset_env",
            "waits",
            "reconcile",
            "dependencies",
            "independent_review",
            "scaffolding",
            "quality.sonar",
            "quality.sources",
        ],
    ),
    (
        "delivery",
        "repos",
        "Repos and records",
        "New repos on the golden path, standards drift, and the product's own record of every approved document.",
        ["graph.provision", "provisioning", "provision_queue", "repo_standards", "drafts", "records", "traceability"],
    ),
    (
        "roadmap",
        "initiatives",
        "Initiatives and releases",
        "Goals, initiatives and releases above features: their drafts, workflows and views.",
        ["roadmap", "roadmap.graphs", "roadmap.initiative", "roadmap.views"],
    ),
    (
        "roadmap",
        "outcomeloop",
        "Outcome loop",
        "Reading goals, readouts after a release, and the initiative review: is the goal moving, and what now.",
        ["outcomes.read", "outcomes.readouts", "outcomes.review"],
    ),
    (
        "ops",
        "incidents",
        "Incidents and signals",
        "Signals from alerts, errors and tickets, worked into incidents to a postmortem; paging, alarms, change"
        " receipts, lookback.",
        [
            "ops.incident",
            "ops.signals",
            "ops.signal_tokens",
            "ops.alarms",
            "ops.error_tracking",
            "ops.paging",
            "ops.lookback",
            "ops.servicenow",
        ],
    ),
    (
        "ops",
        "costs",
        "Costs and inventory",
        "One FinOps ledger and its collectors, cost after release against G4's plan, and what each environment "
        "really runs.",
        ["finops", "finops.collect", "finops.ledger", "ops.cost", "ops.inventory"],
    ),
    (
        "ops",
        "upkeepsvc",
        "Security upkeep",
        "GitHub security alerts into one findings register, triaged into fixes with deadlines.",
        ["upkeep.alerts", "upkeep.register", "upkeep.service"],
    ),
    (
        "core",
        "agentrt",
        "Agent runtime",
        "Calling agents: prompt layers, sessions and hand-offs, runtimes (Claude Code, Codex, generic), "
        "sandboxes, lessons and budgets.",
        [
            "agent_calls",
            "agent_steps",
            "agents",
            "agents.*",
            "runtime",
            "runtime.*",
            "sessions",
            "session_launcher",
            "session_runner",
            "sandbox",
            "egress_proxy",
            "git_broker",
            "workspace",
            "context",
            "budgets",
            "loops",
            "lessons",
            "diary",
        ],
    ),
    (
        "core",
        "gatewaycap",
        "Model gateway",
        "One gateway for every model call: provider and model per role, data-class rules, pinning, prices and "
        "the usage ledger.",
        ["gateway", "gateway.*", "gateway_sessions", "models", "pinning", "plugins"],
    ),
    (
        "core",
        "evalcap",
        "Checks and quality policy",
        "Deterministic checks, the critic and its rubrics, quality and performance policy, eval cases, "
        "acceptance tests.",
        ["evaluators", "evaluators.*", "quality", "performance", "acceptance", "eval_case"],
    ),
    (
        "core",
        "flow",
        "Workflow rules",
        "What threads share: state, events, gate decisions and separation of duties, profiles, progress, "
        "notifications, duplicates.",
        [
            "state",
            "graph_events",
            "gate_decisions",
            "decisions",
            "separation",
            "thread_kinds",
            "progress",
            "profiles",
            "notify",
            "events",
            "duplicates",
        ],
    ),
    (
        "core",
        "governancecap",
        "Governance and records",
        "Each product's governance and constitution, design knowledge and records, research, repo policy and "
        "managed files.",
        [
            "governance",
            "governance.*",
            "constitution",
            "config",
            "knowledge",
            "knowledge.*",
            "design_record",
            "roadmap.record",
            "inception.formats",
            "research",
            "retrieval",
            "repo_policy",
            "repo_files",
            "error_tracking_policy",
        ],
    ),
    (
        "core",
        "components",
        "Components and resources",
        "What a product is built from: component roles and their template contract, cloud resources, capacity, "
        "environments, datasets.",
        [
            "template_contract",
            "repo_roles",
            "channels",
            "connectors",
            "documents",
            "jobs",
            "pipelines",
            "store",
            "sandboxes",
            "agent_tools",
            "resources",
            "capacity",
            "environments",
            "catalog_info",
            "datasets",
        ],
    ),
    (
        "core",
        "prodrules",
        "Production rules",
        "Gradual rollout, remediation limits, alert rules, the change log, undo, cost plans and records, "
        "outcomes, upkeep policy.",
        [
            "ops.*",
            "undo",
            "finops.records",
            "finops.views",
            "outcomes",
            "outcomes.instrumentation",
            "outcomes.sources",
            "outcomes.store",
            "upkeep",
        ],
    ),
    (
        "core",
        "integ",
        "Integrations",
        "GitHub, OpenChoreo, feature flags, work tracking, packages, MCP servers, and tool connections through OAuth.",
        ["integrations", "integrations.*", "oauth_connections", "connections"],
    ),
    (
        "core",
        "cloudcap",
        "Cloud ports",
        "What the tool needs from a cloud, with AWS, Azure and Google Cloud adapters and in-memory fakes.",
        ["cloud", "cloud.*"],
    ),
    (
        "core",
        "trust",
        "Trust and access",
        "Workspaces and tenancy, secrets and signing, audit trail and anchors, artifacts, telemetry and safe "
        "mode, the tool's own hosting tiers and the releases it proposes for itself.",
        [
            "release_proposals",
            "workspaces",
            "tenancy",
            "billing",
            "secret_ring",
            "secret_scrub",
            "signing",
            "audit_events",
            "audit_anchors",
            "append_only",
            "artifacts",
            "telemetry",
            "metrics",
            "safe_mode",
            "seams",
            "cassettes",
            "cluster",
            "vm",
            "laptop",
            "laptop.*",
        ],
    ),
]


def _path(module: str) -> Path:
    p = SRC / (module.replace(".", "/") + ".py")
    return p if p.exists() else SRC / module.replace(".", "/") / "__init__.py"


def _summary(module: str) -> str:
    try:
        doc = ast.get_docstring(ast.parse(_path(module).read_text())) or ""
    except (OSError, SyntaxError):
        return ""
    doc = " ".join(doc.split())
    return re.split(r"(?<=[.!?])\s", doc, maxsplit=1)[0][:220] if doc else ""


def modules() -> list[str]:
    names = set()
    for p in SRC.rglob("*.py"):
        parts = list(p.relative_to(SRC).with_suffix("").parts)
        if parts[-1] == "__init__":
            parts = parts[:-1]
        if parts and parts[0] not in SKIP and "__pycache__" not in parts:
            names.add(".".join(parts))
    return sorted(names)


def _matches(name: str, patterns: list[str]) -> bool:
    return any(name.startswith(p[:-1]) if p.endswith(".*") else name == p for p in patterns)


def capability_of(name: str, layer: str) -> str:
    """The capability a module belongs to; raises if none, or more than one, claims it."""
    hits = [c for c in CAPS if _matches(name, c[4])]
    hits = [c for c in hits if name in c[4]] or hits
    hits = [c for c in hits if c[0] == layer] or hits
    if len(hits) != 1 or hits[0][0] != layer:
        where = "claimed by " + ", ".join(c[1] for c in hits) if hits else "in no capability"
        raise ValueError(f"{name} ({layer}): {where}")
    return hits[0][1]


def build() -> dict:
    config = seams.load()
    names = modules()
    index = {n: i for i, n in enumerate(names)}
    edges = sorted({(index[a], index[b]) for a, b in seams.imports() if a in index and b in index})
    mods = [
        {
            "n": n,
            "s": seams.seam_of(n, config),
            "d": _summary(n),
            "l": len(_path(n).read_text().splitlines()),
            "pkg": _path(n).name == "__init__.py",
        }
        for n in names
    ]
    cap_of = [capability_of(m["n"], m["s"]) for m in mods]
    links: dict[tuple[str, str], list[list[int]]] = defaultdict(list)
    inside: Counter[str] = Counter()
    for a, b in edges:
        if cap_of[a] == cap_of[b]:
            inside[cap_of[a]] += 1
        else:
            links[(cap_of[a], cap_of[b])].append([a, b])
    caps = [
        {
            "k": c[1],
            "layer": c[0],
            "name": c[2],
            "d": c[3],
            "inside": inside[c[1]],
            "mods": [i for i, k in enumerate(cap_of) if k == c[1]],
        }
        for c in CAPS
    ]
    empty = [c["k"] for c in caps if not c["mods"]]
    if empty:
        raise ValueError(f"capabilities with no modules: {', '.join(empty)}")
    ordered = sorted(links.items(), key=lambda x: -len(x[1]))
    return {
        "m": mods,
        "e": [list(e) for e in edges],
        "caps": caps,
        "cap_of": cap_of,
        "links": [{"a": a, "b": b, "n": len(p), "p": p} for (a, b), p in ordered],
    }


def _commit() -> str:
    """The checked-out commit, read from .git (no subprocess), for the page's footer."""
    git = ROOT / ".git"
    try:
        head = (git / "HEAD").read_text().strip()
        if not head.startswith("ref: "):
            return head[:7]
        ref = head[5:]
        if (git / ref).exists():
            return (git / ref).read_text().strip()[:7]
        for line in (git / "packed-refs").read_text().splitlines():
            if line.endswith(" " + ref):
                return line[:7]
    except OSError:
        pass
    return "unknown"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="only check every module has a capability")
    args = parser.parse_args()
    data = build()
    if args.check:
        print(f"{len(data['m'])} modules in {len(data['caps'])} capabilities")
        return 0
    commit = _commit()
    page = (
        TEMPLATE.read_text()
        .replace("__DATA__", json.dumps(data, separators=(",", ":")).replace("</", "<\\/"))  # safe in <script>
        .replace("__COMMIT__", commit)
        .replace("__DATE__", datetime.now(UTC).strftime("%Y-%m-%d"))
    )
    OUT.write_text(page)
    counts = f"{len(data['m'])} modules, {len(data['e'])} imports, {len(data['caps'])} capabilities"
    print(f"wrote {OUT.relative_to(ROOT)}: {counts}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

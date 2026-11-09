"""Performance testing: budgets, and when a change needs a performance test.

Budgets are the product's (product.yaml `performance:`), starting from common industry targets:
an API answers 95% of requests within 500 ms and 99% within 1 s with under 1% errors; a web page
paints its largest content within 2.5 s with under 200 ms of blocked main thread and a layout
shift under 0.1; an event reaches its store within 5 s. Products tighten them as they learn.

When a test is needed is decided per repository of the change set, by fixed rules first:

  none   only docs, tests or comments changed
  smoke  a hot path, a database migration, a runtime or base-image upgrade, a change to timeouts,
         pools or JVM flags, or a large change: a short load test on the pull request that
         compares the image built from main with the PR's, on the same runner (perf-smoke)
  full   an API contract change, a new or changed event handler, or a design whose Performance
         section says the feature adds traffic: a load test in staging at the product's expected
         peak scaled to staging's size, judged against the budget and the last release (performance)

Agents can only raise a level: the reviewer's performance concerns raise `none` to `smoke`. A
person can carry on past a failed performance suite at the delivery gate, with a reason that is
recorded, like any other failed suite.
"""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass, field

LEVELS = ("none", "smoke", "full")

# Industry-standard starting budgets per kind of component (tighten them after the pilot).
DEFAULT_BUDGETS: dict[str, dict[str, float]] = {
    # HTTP APIs, the BFF and MCP servers: response time percentiles and error rate (SRE practice).
    "api": {"p95_ms": 500, "p99_ms": 1000, "error_rate": 0.01},
    # Kafka event workers: time from event to store, and failed applications.
    "event-worker": {"lag_p95_s": 5, "error_rate": 0.01},
    # Web apps: Core Web Vitals "good" thresholds (LCP 2.5 s, CLS 0.1); TBT 200 ms is Lighthouse's
    # lab stand-in for INP; 200 KB of compressed JavaScript on first load.
    "web": {"lcp_ms": 2500, "tbt_ms": 200, "cls": 0.1, "js_kb": 200},
    # Mobile apps: a cold start within 2 s, and a JavaScript bundle within 3 MB.
    "mobile": {"cold_start_ms": 2000, "bundle_kb": 3000},
    # Agents: model turns and seconds per run (from the recorded evals).
    "agent": {"turns_max": 8, "run_p95_s": 60},
}

KIND_OF_ROLE = {"provider": "api", "bff": "api", "mcp-server": "api", "consumer": "web", "mobile": "mobile",
                "event-worker": "event-worker", "agent": "agent"}

DEFAULTS = {
    "smoke_regression_pct": 20,  # PR: how much worse than main the PR's image may be (shared runners are noisy)
    "staging_regression_pct": 10,  # staging: how much worse than the last release
    # Staging is smaller than production: its load test runs at this share of the expected peak,
    # and the budgets are judged at that load.
    "staging_load_factor": 0.1,
    "peak_rps": 50,  # expected peak requests per second per API, until the product says otherwise
    "large_change_files": 15,  # runtime files changed in one repository that make it a large change
}

# Where hot code usually lives in the templates; products add theirs (`hot_paths`).
DEFAULT_HOT_PATHS = (
    "**/*Controller.java", "**/*Handler.java", "**/*Store.java", "**/*Repository.java", "**/*Service.java",
    "**/events/**", "**/security/**",
    "src/*/server.py", "src/*/store.py", "src/*/graph.py", "src/*/tools.py", "src/*/api.py",
    "src/api/**", "src/app/**", "src/screens/**", "src/outbox/**",
)
MIGRATIONS = ("**/db/migration/**", "**/migrations/**", "**/*.sql")
RUNTIME = ("Dockerfile", "gradle/libs.versions.toml", "build.gradle.kts", "pyproject.toml", "package.json",
           "nginx.conf", "app.config.ts")
TUNING = ("**/application.yaml", "**/application.yml", "**/settings.py", "workload.yaml", "**/*Config.java")
CONTRACTS = ("**/openapi.yaml", "**/openapi.yml", "**/openapi.json", "api/**", "pacts/**")
HANDLERS = ("**/*Handler.java", "**/handlers/**")
NOT_RUNTIME = ("**/test/**", "**/tests/**", "**/*.test.*", "**/*Test.java", "**/test_*.py", "**/*.md", "docs/**",
               ".github/**", ".vrixel/**", "**/*.spec.ts", "e2e/**", "evals/**", "LICENSE", ".editorconfig")


@dataclass
class Decision:
    repo: str
    role: str
    level: str = "none"
    reasons: list[str] = field(default_factory=list)
    soak: bool = False  # data volume grows: the nightly soak test matters for this release

    def raise_to(self, level: str, reason: str) -> None:
        if LEVELS.index(level) > LEVELS.index(self.level):
            self.level = level
        if reason not in self.reasons:
            self.reasons.append(reason)

    def as_dict(self) -> dict:
        return {"repo": self.repo, "role": self.role, "level": self.level, "reasons": list(self.reasons),
                "soak": self.soak}


def _any(path: str, globs) -> bool:
    p = path.lstrip("./")
    return any(fnmatch.fnmatch(p, g) or fnmatch.fnmatch("/" + p, "*/" + g.lstrip("*/")) for g in globs)


def settings(product_perf: dict | None) -> dict:
    """The product's performance settings over the defaults (budgets merged per kind)."""
    raw = dict(product_perf or {})
    budgets = {k: dict(v) for k, v in DEFAULT_BUDGETS.items()}
    for kind, values in (raw.get("budgets") or {}).items():
        budgets.setdefault(kind, {}).update(values or {})
    out = {**DEFAULTS, **{k: v for k, v in raw.items() if k in DEFAULTS}, "budgets": budgets}
    out["hot_paths"] = tuple(DEFAULT_HOT_PATHS) + tuple(raw.get("hot_paths") or ())
    return out


def check(raw: dict | None) -> list[str]:
    """Problems with a product's `performance:` block."""
    problems: list[str] = []
    if raw is None:
        return problems
    if not isinstance(raw, dict):
        return ["performance must be a mapping"]
    known = set(DEFAULTS) | {"budgets", "hot_paths"}
    for k in sorted(set(raw) - known):
        problems.append(f"performance.{k} is not a setting (use {sorted(known)})")
    for kind, values in (raw.get("budgets") or {}).items():
        if kind not in DEFAULT_BUDGETS:
            problems.append(f"performance.budgets.{kind} is not a kind of component ({sorted(DEFAULT_BUDGETS)})")
            continue
        for metric, v in (values or {}).items():
            if metric not in DEFAULT_BUDGETS[kind]:
                problems.append(f"performance.budgets.{kind}.{metric} is not a metric ({sorted(DEFAULT_BUDGETS[kind])})")
            elif not isinstance(v, (int, float)) or v <= 0:
                problems.append(f"performance.budgets.{kind}.{metric} must be a positive number")
    f = raw.get("staging_load_factor")
    if f is not None and not (isinstance(f, (int, float)) and 0 < f <= 1):
        problems.append("performance.staging_load_factor must be between 0 and 1")
    return problems


# --------------------------------------------------------------------------- the design's section

_LINE = re.compile(r"^\s*[-*]\s*(new traffic|data growth|expected load|journeys)\s*:\s*(.+)$", re.I | re.M)


def design_signals(hld: str) -> dict:
    """What the HLD's `## Performance` section says: whether the feature adds traffic or data
    volume, the expected load and the journeys to load-test."""
    m = re.search(r"^##\s+Performance\s*$(.*?)(?=^##\s|\Z)", hld or "", re.M | re.S)
    if not m:
        return {}
    found = {k.lower(): v.strip() for k, v in _LINE.findall(m.group(1))}
    yes = lambda v: bool(re.match(r"\s*yes\b", v or "", re.I))  # noqa: E731
    return {"new_traffic": yes(found.get("new traffic")), "data_growth": yes(found.get("data growth")),
            "expected_load": found.get("expected load", ""), "journeys": found.get("journeys", "")}


# --------------------------------------------------------------------------- the rules


def decide(repo: str, role: str, files: list[str], *, hld: str = "", perf: dict | None = None,
           concerns: list[str] | None = None) -> Decision:
    """The level a repository's change needs. Fixed rules on the changed files and the design;
    the reviewer's `concerns` can only raise it."""
    s = perf if perf and "budgets" in perf else settings(perf)
    d = Decision(repo=repo, role=role)
    kind = KIND_OF_ROLE.get(role)
    if kind is None:  # libraries, the acceptance repo: their users' tests measure them
        return d
    runtime = [f for f in files if not _any(f, NOT_RUNTIME)]
    if not runtime:
        return d
    if any(_any(f, CONTRACTS) for f in runtime):
        d.raise_to("full", "the API contract changes")
    if role == "event-worker" and any(_any(f, HANDLERS) for f in runtime):
        d.raise_to("full", "an event handler is new or changes")
    if any(_any(f, s["hot_paths"]) for f in runtime):
        d.raise_to("smoke", "a hot path changes")
    if any(_any(f, MIGRATIONS) for f in runtime):
        d.raise_to("smoke", "a database migration")
    if any(_any(f, RUNTIME) for f in runtime):
        d.raise_to("smoke", "the runtime, its dependencies or its image change")
    if any(_any(f, TUNING) for f in runtime):
        d.raise_to("smoke", "timeouts, pools or runtime settings change")
    if len(runtime) >= int(s["large_change_files"]):
        d.raise_to("smoke", f"a large change ({len(runtime)} runtime files)")
    sig = design_signals(hld)
    if sig.get("new_traffic") and kind in ("api", "web", "event-worker") and d.level != "none":
        d.raise_to("full", "the design says the feature adds traffic")
    if sig.get("data_growth") and d.level != "none":
        d.soak = True
        d.reasons.append("the design says data volume grows: watch the nightly soak test")
    for c in concerns or []:
        d.raise_to("smoke", f"the reviewer: {c}")
    return d


def label(level: str) -> str | None:
    """The pull request label that runs the template's perf-smoke job."""
    return {"smoke": "vrixel:perf-smoke", "full": "vrixel:perf-full"}.get(level)


def summary(decisions: dict[str, dict]) -> str:
    """One line per repository for the gates: the level and why."""
    rows = [d for d in decisions.values() if d["level"] != "none"]
    if not rows:
        return "No performance test needed: no runtime path, contract or design change that affects performance."
    return "\n".join(f"- {d['repo']}: {d['level']} ({'; '.join(d['reasons'])})" for d in sorted(rows, key=lambda d: d["repo"]))

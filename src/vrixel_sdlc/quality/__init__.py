"""Tests and security scans as policy, independent of the tools that run them.

- A *suite* (unit, sast, e2e, ...) says what must be verified, at which stage, and how strictly.
- A *source* reads one kind of result (GitHub check runs, SARIF code-scanning alerts, dependency
  review, secret scanning, OpenChoreo build steps) and turns it into the same shape:
  SuiteResult with counts, coverage and Findings.
- The *gate* compares results with the policy: failed or missing tests send work back; findings
  at or above `block_on` go to security approvers, who can have agents fix them, accept the
  risk for a limited time (a waiver), or stop the feature.

Policy is config: platform defaults and per-data-class floors in config/quality.yaml, product
tightening in <product>-governance/product.yaml (`quality:`). Changing tools is a config edit; a new kind
of source is one class named in config/quality.yaml.
"""

from __future__ import annotations

import fnmatch
import importlib
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Protocol

import yaml

SEVERITIES = ("info", "low", "medium", "high", "critical")
STAGES = ("pr", "build", "dev", "staging", "production")
# The change set's own environment runs the dev-stage suites (and acceptance) before anything merges.
POLICY_STAGE = {"change_set": "dev"}
WAIT_STAGE = {"ci": "pr", "build": "build", "change_set": "change_set", "dev": "dev", "staging": "staging", "production": "production"}


def sev_rank(sev: str | None) -> int:
    s = (sev or "").lower()
    alias = {"error": "high", "warning": "medium", "note": "low", "none": "info", "moderate": "medium"}
    s = alias.get(s, s)
    return SEVERITIES.index(s) if s in SEVERITIES else 2  # unknown counts as medium


def norm_sev(sev: str | None) -> str:
    return SEVERITIES[sev_rank(sev)]


@dataclass
class Finding:
    suite: str
    tool: str
    rule: str
    severity: str
    title: str
    repo: str = ""
    url: str = ""
    fingerprint: str = ""  # stable id, used for waivers

    def __post_init__(self) -> None:
        self.severity = norm_sev(self.severity)
        if not self.fingerprint:
            self.fingerprint = f"{self.tool}:{self.repo}:{self.rule}:{self.title}"[:240]


@dataclass
class SuiteResult:
    suite: str
    kind: str  # test | security
    stage: str
    state: str  # passed | failed | pending | missing | not_needed
    repo: str = ""
    passed: int | None = None
    failed: int | None = None
    skipped: int | None = None
    coverage: float | None = None
    url: str = ""
    message: str = ""
    findings: list[Finding] = field(default_factory=list)
    details: dict = field(default_factory=dict)  # e.g. acceptance results per criterion


class QualitySource(Protocol):
    def collect(self, suite: str, spec: dict, targets: list[dict], stage: str) -> list[SuiteResult]: ...


# --------------------------------------------------------------------------- policy


class QualityConfigError(ValueError):
    pass


@dataclass(frozen=True)
class QualityConfig:
    sources: dict[str, str]
    suites: dict[str, dict]
    floors: dict[str, dict[str, dict]]
    waiver_max_days: int = 90


def load_quality(path: Path) -> QualityConfig:
    if not path.exists():
        return QualityConfig(sources={}, suites={}, floors={})
    raw = yaml.safe_load(path.read_text()) or {}
    return QualityConfig(sources=dict(raw.get("sources") or {}), suites=dict(raw.get("suites") or {}),
                         floors={k: dict(v or {}) for k, v in (raw.get("floors") or {}).items()},
                         waiver_max_days=int((raw.get("waivers") or {}).get("max_days", 90)))


_KEYS = {"kind", "stage", "source", "match", "required", "block_on", "min_coverage", "min_repos", "budget_s",
         "applies_to", "level", "min_pass_rate"}
PERF_LEVELS = ("none", "smoke", "full")


def _stricter(a: dict, b: dict) -> dict:
    """Combine two specs keeping the stricter value of each limit."""
    out = {**a, **{k: v for k, v in b.items() if k not in ("required", "block_on", "min_coverage", "min_pass_rate")}}
    if "required" in b:
        out["required"] = bool(a.get("required")) or bool(b["required"])
    if "block_on" in b:
        out["block_on"] = b["block_on"] if sev_rank(b["block_on"]) < sev_rank(a.get("block_on", "critical")) \
            else a.get("block_on", b["block_on"])
    if "min_coverage" in b:
        out["min_coverage"] = max(float(a.get("min_coverage") or 0), float(b["min_coverage"]))
    if "min_pass_rate" in b:
        out["min_pass_rate"] = max(float(a.get("min_pass_rate") or 0), float(b["min_pass_rate"]))
    return out


def effective_policy(cfg: QualityConfig, data_class: str, overrides: dict | None,
                     problems: list[str] | None = None) -> dict[str, dict]:
    """Platform suites, tightened by the data-class floor, then by the product. A product may
    tighten, add suites, or change `source`/`match` (its tools), never loosen below the floor."""
    problems = problems if problems is not None else []
    floors = cfg.floors.get(data_class, {})
    policy = {}
    for name, spec in cfg.suites.items():
        policy[name] = _stricter(dict(spec), floors.get(name, {}))
    for name, raw in (overrides or {}).items():
        if not isinstance(raw, dict):
            problems.append(f"quality.{name} must be a mapping")
            continue
        unknown = set(raw) - _KEYS
        if unknown:
            problems.append(f"quality.{name} has unknown keys {sorted(unknown)}")
        base = policy.get(name)
        if base is None:  # a new suite
            missing = {"kind", "stage", "source"} - set(raw)
            if missing:
                problems.append(f"quality.{name} is a new suite and needs {sorted(missing)}")
                continue
            base = {"required": False, "match": {}}
        floor = base
        if "required" in raw and not raw["required"] and floor.get("required"):
            problems.append(f"quality.{name} is required by the platform for {data_class} data")
        if "block_on" in raw and sev_rank(raw["block_on"]) > sev_rank(floor.get("block_on", "critical")):
            problems.append(f"quality.{name}.block_on {raw['block_on']} is looser than the platform's {floor.get('block_on')}")
        if "min_coverage" in raw and float(raw["min_coverage"]) < float(floor.get("min_coverage") or 0):
            problems.append(f"quality.{name}.min_coverage {raw['min_coverage']} is below the platform's {float(floor.get('min_coverage')):g}%")
        if "min_pass_rate" in raw:
            rate = raw["min_pass_rate"]
            if not isinstance(rate, (int, float)) or not 0 < float(rate) <= 1:
                problems.append(f"quality.{name}.min_pass_rate must be a share between 0 and 1, e.g. 0.95")
            elif float(rate) < float(floor.get("min_pass_rate") or 0):
                problems.append(f"quality.{name}.min_pass_rate {rate} is below the platform's {float(floor['min_pass_rate']):g}")
        merged = {**base, **{k: v for k, v in raw.items() if k in _KEYS}}
        if "required" in raw:
            merged["required"] = bool(raw["required"]) or bool(floor.get("required"))
        policy[name] = merged
    for name, spec in policy.items():
        if spec.get("stage") not in STAGES:
            problems.append(f"quality.{name}.stage must be one of {list(STAGES)}")
        if spec.get("kind") not in ("test", "security"):
            problems.append(f"quality.{name}.kind must be test or security")
        if spec.get("source") not in cfg.sources and spec.get("source") != "stub":
            problems.append(f"quality.{name}.source {spec.get('source')!r} is not registered in config/quality.yaml")
        if spec.get("block_on") is not None and str(spec["block_on"]).lower() not in SEVERITIES:
            problems.append(f"quality.{name}.block_on must be one of {list(SEVERITIES)}")
    return policy


# --------------------------------------------------------------------------- sources


def load_sources(cfg: QualityConfig, services) -> dict[str, QualitySource]:
    out = {}
    for name, dotted in cfg.sources.items():
        module, _, cls = dotted.partition(":")
        out[name] = getattr(importlib.import_module(module), cls)(services)
    return out


def match_any(name: str, patterns: list[str]) -> bool:
    low = (name or "").lower()
    return any(fnmatch.fnmatch(low, p.lower()) for p in patterns)


# --------------------------------------------------------------------------- gate


def active_waivers(waivers: list[dict], at: datetime | None = None) -> set[str]:
    at = at or datetime.now(timezone.utc)
    return {w["fingerprint"] for w in waivers or []
            if datetime.fromisoformat(w["expires"]) > at}


def waiver(finding: dict, *, approver: str, reason: str, days: int, max_days: int) -> dict:
    days = max(1, min(int(days or max_days), max_days))
    expires = datetime.now(timezone.utc) + timedelta(days=days)
    return {"fingerprint": finding["fingerprint"], "suite": finding["suite"], "rule": finding["rule"],
            "severity": finding["severity"], "repo": finding.get("repo", ""), "title": finding["title"],
            "approver": approver, "reason": reason, "expires": expires.isoformat()}


def evaluate(stage: str, policy: dict[str, dict], results: list[SuiteResult], waivers: list[dict]) -> dict:
    """status: pending (keep waiting), failed (tests), blocked (security), passed."""
    waived = active_waivers(waivers)
    suites, test_failures, blocking, pending = [], [], [], []
    for name, spec in sorted(policy.items()):
        if spec.get("stage") != POLICY_STAGE.get(stage, stage):
            continue
        mine = [r for r in results if r.suite == name]
        if not mine and spec.get("applies_to") in ("acceptance", "performance"):
            continue  # only some changes need these; Quality.check reports them when they do
        if not mine:
            mine = [SuiteResult(suite=name, kind=spec["kind"], stage=stage, state="missing")]
        for r in mine:
            if r.state == "not_needed":  # e.g. no acceptance run for a single-repo change
                continue
            row = asdict(r)
            reasons = []
            if r.state == "pending":
                pending.append(name)
            elif r.state == "missing":
                if spec.get("required"):
                    reasons.append(f"{name} did not report{' for ' + r.repo if r.repo else ''}")
            elif spec["kind"] == "test":
                if r.state == "failed":
                    reasons.append(f"{name} tests failed{' in ' + r.repo if r.repo else ''}"
                                   + (f": {r.message}" if r.message else ""))
                ran = (r.passed or 0) + (r.failed or 0)
                if spec.get("min_pass_rate") and ran and (r.passed or 0) / ran < float(spec["min_pass_rate"]):
                    # Agents' evals against a real model: a case may miss by chance, a share may not.
                    reasons.append(f"{name} passed {(r.passed or 0) / ran:.0%} of its cases, below "
                                   f"{float(spec['min_pass_rate']):.0%}" + (f" in {r.repo}" if r.repo else ""))
                if spec.get("min_coverage") and r.coverage is not None and r.coverage < float(spec["min_coverage"]):
                    reasons.append(f"{name} coverage {r.coverage:.1f}% is below {float(spec['min_coverage']):.0f}%"
                                   + (f" in {r.repo}" if r.repo else ""))
            else:
                threshold = sev_rank(spec.get("block_on", "high"))
                findings = list(r.findings)
                if r.state == "failed" and not findings:  # a red scan job with no detail
                    findings = [Finding(suite=name, tool=name, rule=f"{name}-failed", severity=spec.get("block_on", "high"),
                                        title=r.message or f"{name} failed", repo=r.repo, url=r.url)]
                row["findings"] = [asdict(f) for f in findings]
                for f in findings:
                    if sev_rank(f.severity) >= threshold:
                        if f.fingerprint in waived:
                            row.setdefault("waived", []).append(f.fingerprint)
                        else:
                            blocking.append(asdict(f))
            if reasons:
                if spec["kind"] == "test" or r.state == "missing":
                    test_failures += reasons
                row["reasons"] = reasons
            row["required"] = bool(spec.get("required"))
            suites.append(row)
    if pending:
        status = "pending"
    elif test_failures:
        status = "failed"
    elif blocking:
        status = "blocked"
    else:
        status = "passed"
    return {"stage": stage, "status": status, "suites": suites, "test_failures": test_failures,
            "blocking": blocking, "pending": sorted(set(pending))}


class Quality:
    """What the waits call: collect a stage's suites from their sources and evaluate them."""

    def __init__(self, cfg: QualityConfig, sources: dict[str, QualitySource]) -> None:
        self.cfg = cfg
        self.sources = sources

    def check(self, stage: str, policy: dict[str, dict], targets: list[dict], waivers: list[dict]) -> dict:
        results: list[SuiteResult] = []
        for name, spec in policy.items():
            if spec.get("stage") != POLICY_STAGE.get(stage, stage):
                continue
            source = self.sources.get(spec["source"]) or self.sources.get("stub")
            if source is None:
                continue
            # Acceptance suites read the product's acceptance repo; every other suite reads the
            # components. A change set with no acceptance run doesn't need one. Performance suites
            # read only the components whose change needs that level of test.
            on_acceptance = spec.get("applies_to") == "acceptance"
            mine = [t for t in targets if (t.get("kind") == "acceptance") == on_acceptance]
            if spec.get("applies_to") == "performance":
                need = PERF_LEVELS.index(spec.get("level") or "smoke")
                mine = [t for t in mine if PERF_LEVELS.index(t.get("perf") or "none") >= need]
            if not mine:
                if on_acceptance or spec.get("applies_to") == "performance":
                    results.append(SuiteResult(suite=name, kind=spec["kind"], stage=stage, state="not_needed"))
                continue
            try:
                results += source.collect(name, spec, mine, stage)
            except Exception as exc:  # a flaky API is not a red suite: keep waiting
                results.append(SuiteResult(suite=name, kind=spec["kind"], stage=stage, state="pending",
                                           message=f"{type(exc).__name__}: {exc}"))
        return evaluate(stage, policy, results, waivers)

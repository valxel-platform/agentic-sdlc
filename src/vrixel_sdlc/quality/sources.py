"""Quality sources: each reads one kind of result and returns SuiteResults. Targets carry what a
stage knows: `repo` and `sha` (and `number`, `base` for pull requests), or an OpenChoreo `run_id`.

Every source takes the services container, so it can use whichever GitHub or OpenChoreo adapter
is live. To add a tool that publishes somewhere new, write one class with `collect` and register
it in config/quality.yaml.
"""

from __future__ import annotations

import re

from . import Finding, SuiteResult, match_any

_COUNTS = {
    "passed": re.compile(r"(\d+)\s+(?:passed|passing)", re.I),
    "failed": re.compile(r"(\d+)\s+(?:failed|failing)", re.I),
    "skipped": re.compile(r"(\d+)\s+(?:skipped|pending)", re.I),
}
_COVERAGE = re.compile(r"coverage[^0-9]{0,20}(\d+(?:\.\d+)?)\s*%", re.I)


def _summary(text: str) -> dict:
    out = {k: int(m.group(1)) for k, rx in _COUNTS.items() if (m := rx.search(text or ""))}
    if m := _COVERAGE.search(text or ""):
        out["coverage"] = float(m.group(1))
    return out


class GitHubChecks:
    """Check runs and commit statuses whose names match `match.checks`. Counts and coverage come
    from the check's summary text when the reporter writes them ("42 passed", "Coverage: 81%")."""

    def __init__(self, services) -> None:
        self.services = services

    def collect(self, suite, spec, targets, stage):
        patterns = (spec.get("match") or {}).get("checks") or [suite]
        out = []
        for t in targets:
            res = self.services.github.checks(repo=t["repo"], sha=t["sha"])
            mine = [c for c in res.get("checks", []) if match_any(c.get("name", ""), patterns)]
            if not mine:
                out.append(SuiteResult(suite=suite, kind=spec["kind"], stage=stage, state="missing", repo=t["repo"]))
                continue
            states = {c["state"] for c in mine}
            state = "pending" if "pending" in states else "failed" if "failure" in states else "passed"
            info = _summary(" ".join(c.get("summary", "") for c in mine))
            failing = [c["name"] for c in mine if c["state"] == "failure"]
            out.append(SuiteResult(suite=suite, kind=spec["kind"], stage=stage, state=state, repo=t["repo"],
                                   url=mine[0].get("url", ""), message=", ".join(failing),
                                   **{k: info.get(k) for k in ("passed", "failed", "skipped", "coverage")}))
        return out


class GitHubCodeScanning:
    """Open code-scanning alerts (any SARIF-producing tool) for the pull request, or for the
    branch at later stages. `match.tools` limits tools; `match.categories` limits SARIF categories."""

    def __init__(self, services) -> None:
        self.services = services

    def collect(self, suite, spec, targets, stage):
        m = spec.get("match") or {}
        tools, cats = m.get("tools") or [], m.get("categories") or []
        out = []
        for t in targets:
            alerts = self.services.github.code_scanning_alerts(repo=t["repo"], pr=t.get("number"), ref=t.get("ref"))
            if alerts is None:  # code scanning not enabled on the repo
                out.append(SuiteResult(suite=suite, kind="security", stage=stage, state="missing", repo=t["repo"]))
                continue
            findings = []
            for a in alerts:
                tool = (a.get("tool") or {}).get("name", "")
                cat = (a.get("most_recent_instance") or {}).get("category", "")
                if tools and not match_any(tool, tools):
                    continue
                if cats and not any(c.lower() in (cat or "").lower() for c in cats):
                    continue
                rule = a.get("rule") or {}
                findings.append(Finding(
                    suite=suite, tool=tool, rule=str(rule.get("id", "")),
                    severity=rule.get("security_severity_level") or rule.get("severity") or "medium",
                    title=rule.get("description") or rule.get("name") or str(rule.get("id", "")),
                    repo=t["repo"], url=a.get("html_url", ""), fingerprint=f"{t['repo']}:code-scanning:{a.get('number')}"))
            out.append(SuiteResult(suite=suite, kind="security", stage=stage, state="passed", repo=t["repo"],
                                   findings=findings))
        return out


class GitHubDependencyReview:
    """Vulnerable dependencies the change adds (dependency review between base and head)."""

    def __init__(self, services) -> None:
        self.services = services

    def collect(self, suite, spec, targets, stage):
        out = []
        for t in targets:
            diff = self.services.github.dependency_diff(repo=t["repo"], base=t.get("base") or "main", head=t["sha"])
            if diff is None:
                out.append(SuiteResult(suite=suite, kind="security", stage=stage, state="missing", repo=t["repo"]))
                continue
            findings = [
                Finding(suite=suite, tool="dependency-review", rule=v.get("advisory_ghsa_id", ""),
                        severity=v.get("severity", "medium"),
                        title=f"{d.get('name')}@{d.get('version')}: {v.get('advisory_summary', '')}",
                        repo=t["repo"], url=v.get("advisory_url", ""),
                        fingerprint=f"{t['repo']}:{v.get('advisory_ghsa_id')}:{d.get('name')}")
                for d in diff if d.get("change_type") == "added" for v in d.get("vulnerabilities") or []]
            out.append(SuiteResult(suite=suite, kind="security", stage=stage, state="passed", repo=t["repo"],
                                   findings=findings))
        return out


class GitHubSecretScanning:
    """Open secret-scanning alerts in the repo. Any exposed secret is a finding, wherever it is."""

    def __init__(self, services) -> None:
        self.services = services

    def collect(self, suite, spec, targets, stage):
        out = []
        for repo in sorted({t["repo"] for t in targets}):
            alerts = self.services.github.secret_alerts(repo=repo)
            if alerts is None:
                out.append(SuiteResult(suite=suite, kind="security", stage=stage, state="missing", repo=repo))
                continue
            findings = [Finding(suite=suite, tool="secret-scanning", rule=a.get("secret_type", ""),
                                severity="critical", title=f"Exposed {a.get('secret_type_display_name') or 'secret'}",
                                repo=repo, url=a.get("html_url", ""), fingerprint=f"{repo}:secret:{a.get('number')}")
                        for a in alerts]
            out.append(SuiteResult(suite=suite, kind="security", stage=stage, state="passed", repo=repo,
                                   findings=findings))
        return out


class OpenChoreoWorkflowTasks:
    """Steps of the OpenChoreo build whose names match `match.tasks`, e.g. an image scan. A failed
    step is a finding at the suite's `block_on` severity (the step decides what fails it)."""

    def __init__(self, services) -> None:
        self.services = services

    def collect(self, suite, spec, targets, stage):
        patterns = (spec.get("match") or {}).get("tasks") or [suite]
        out = []
        for t in targets:
            if not t.get("run_id"):
                continue
            res = self.services.openchoreo.run_status(run_id=t["run_id"], component=t["component"],
                                                      project=t.get("project"), namespace=t.get("namespace"))
            tasks = [x for x in res.get("tasks") or [] if match_any(x.get("name", ""), patterns)]
            if not tasks:
                out.append(SuiteResult(suite=suite, kind=spec["kind"], stage=stage, state="missing", repo=t["repo"]))
                continue
            phases = {str(x.get("phase", "")).lower() for x in tasks}
            if phases & {"pending", "running"}:
                state = "pending"
            elif phases & {"failed", "error"}:
                state = "failed"
            else:
                state = "passed"
            msg = "; ".join(f"{x['name']}: {x.get('message', '')}" for x in tasks
                            if str(x.get("phase", "")).lower() in ("failed", "error"))
            out.append(SuiteResult(suite=suite, kind=spec["kind"], stage=stage, state=state, repo=t["repo"],
                                   message=msg))
        return out


class AcceptanceRuns:
    """The product acceptance run for a feature and stage: the check run
    `acceptance / <feature> / <stage>` on the acceptance repo's commit. Its summary lists each
    criterion ("AC-3: passed") and the duration; a criterion the change set needs that has no
    passing test fails the suite. Running over the product's budget is reported, not blocking."""

    def __init__(self, services) -> None:
        self.services = services

    def collect(self, suite, spec, targets, stage):
        from ..acceptance import check_name, parse_report

        out = []
        for t in targets:
            name = check_name(t["feature_id"], stage)
            res = self.services.github.checks(repo=t["repo"], sha=t["sha"])
            mine = [c for c in res.get("checks", []) if c.get("name", "").lower() == name.lower()]
            if not mine:
                out.append(SuiteResult(suite=suite, kind="test", stage=stage, state="pending", repo=t["repo"],
                                       message=f"waiting for {name}"))
                continue
            c = mine[-1]
            if c["state"] == "pending":
                out.append(SuiteResult(suite=suite, kind="test", stage=stage, state="pending", repo=t["repo"], url=c.get("url", "")))
                continue
            out.append(acceptance_result(suite, stage, t, c.get("summary", ""), c["state"], c.get("url", ""), spec))
        return out


def acceptance_result(suite: str, stage: str, target: dict, summary: str, state: str, url: str, spec: dict) -> SuiteResult:
    from ..acceptance import parse_report

    report = parse_report(summary)
    counts = _summary(summary)
    acs = report["acs"]
    wanted = list(target.get("acs") or [])
    failing = [ac for ac in wanted if acs.get(ac) == "failed"]
    missing = [ac for ac in wanted if ac not in acs or acs[ac] == "skipped"]
    problems = ([f"failing: {', '.join(failing)}"] if failing else []) + \
               ([f"no passing test: {', '.join(missing)}"] if missing else [])
    if state == "failure" and not problems:
        problems.append("the run failed")
    budget = spec.get("budget_s")
    over = bool(budget and report["duration_s"] and report["duration_s"] > float(budget))
    details = {"criteria": {ac: acs.get(ac, "missing") for ac in wanted} or acs,
               "duration_s": report["duration_s"], "budget_s": budget, "over_budget": over,
               "tags": list(target.get("tags") or []), "projects": list(target.get("projects") or [])}
    msg = "; ".join(problems)
    if over:
        msg = "; ".join(filter(None, [msg, f"took {report['duration_s']:.0f}s, over the {float(budget):.0f}s budget"]))
    return SuiteResult(suite=suite, kind="test", stage=stage, state="failed" if problems else "passed",
                       repo=target["repo"], url=url, message=msg, details=details,
                       **{k: counts.get(k) for k in ("passed", "failed", "skipped")})


class StubQuality:
    """Offline: every suite passes with plausible numbers, unless a test injects results through
    `results[(suite, repo)]` (a SuiteResult or a callable returning one)."""

    def __init__(self, services=None) -> None:
        self.results: dict[tuple[str, str], object] = {}

    def collect(self, suite, spec, targets, stage):
        out = []
        for repo in sorted({t["repo"] for t in targets}):
            hit = self.results.get((suite, repo)) or self.results.get((suite, "*"))
            if callable(hit):
                hit = hit(suite, repo)
            if hit is not None:
                out.append(hit)
            elif spec.get("applies_to") == "acceptance":
                t = next(t for t in targets if t["repo"] == repo)
                lines = "\n".join(f"- {ac}: passed" for ac in t.get("acs") or [])
                out.append(acceptance_result(suite, stage, t, f"{len(t.get('acs') or [])} passed, 0 failed\n{lines}\n"
                                             "Duration: 42s", "success", "", spec))
            elif spec["kind"] == "test":
                out.append(SuiteResult(suite=suite, kind="test", stage=stage, state="passed", repo=repo,
                                       passed=24, failed=0, skipped=0,
                                       coverage=max(85.0, float(spec["min_coverage"])) if spec.get("min_coverage") else None))
            else:
                out.append(SuiteResult(suite=suite, kind="security", stage=stage, state="passed", repo=repo))
        return out

"""The dependencies a change adds, checked before it merges (suite `new-dependencies`).

Dependency review (suite `dependencies`) blocks known vulnerabilities. It says nothing about a
package a model invented and someone then registered, a typo of a popular name, a package
published last week, or a license the product can't ship. For each package the pull request adds
(GitHub's dependency graph diff between the base and the head):

    not in its registry                  critical   (a name nobody publishes: a hallucination)
    first published under min_age_days   high       (new names are where squatting happens)
    license denied, or not allowed       high       (config/dependencies.yaml)
    license unknown                      medium
    OpenSSF Scorecard under min_score    medium     (when its source repository has one)

Dependency manifests are high-risk paths (MANIFESTS), so a change to them also waits for a person
at G5, for every product, not only regulated ones.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

import yaml

from .quality import Finding, SuiteResult

MANIFESTS = ("package.json", "pyproject.toml", "requirements.txt", "requirements-dev.txt", "build.gradle.kts",
             "build.gradle", "settings.gradle.kts", "gradle/libs.versions.toml", "pom.xml", "go.mod")
DEFAULTS = {
    "min_age_days": 30,
    "min_scorecard": 3.0,
    "allowed_licenses": ["MIT", "Apache-2.0", "BSD-2-Clause", "BSD-3-Clause", "ISC", "0BSD", "MPL-2.0",
                         "Unlicense", "CC0-1.0", "Python-2.0", "PSF-2.0", "BlueOak-1.0.0", "EPL-2.0", "Zlib"],
    "denied_licenses": ["GPL-2.0", "GPL-3.0", "AGPL-3.0", "LGPL-2.1", "LGPL-3.0", "SSPL-1.0", "BUSL-1.1",
                        "CC-BY-NC-4.0"],
}


def is_manifest(path: str) -> bool:
    return path.rsplit("/", 1)[-1] in {m.rsplit("/", 1)[-1] for m in MANIFESTS} or path.endswith("libs.versions.toml")


def load_policy(path: str | None = None) -> dict:
    import os

    p = Path(path or Path(os.environ.get("VALXEL_ROOT", ".")) / "config/dependencies.yaml")
    try:
        raw = yaml.safe_load(p.read_text()) or {}
    except OSError:
        raw = {}
    return {**DEFAULTS, **{k: v for k, v in raw.items() if k in DEFAULTS}}


# ------------------------------------------------------------------------------ registries

def _get_json(url: str, timeout: float = 10.0):
    req = Request(url, headers={"Accept": "application/json", "User-Agent": "valxel-sdlc"})
    try:
        with urlopen(req, timeout=timeout) as resp:  # noqa: S310 - fixed https registries
            return json.loads(resp.read().decode("utf-8"))
    except HTTPError as exc:
        if exc.code == 404:
            return None
        raise


def first_published(ecosystem: str, name: str, fetch=_get_json) -> tuple[bool, datetime | None]:
    """(exists, when it was first published). Unknown ecosystems: (True, None)."""
    eco = (ecosystem or "").lower()
    if eco == "npm":
        doc = fetch(f"https://registry.npmjs.org/{quote(name, safe='@')}")
        if doc is None:
            return False, None
        created = (doc.get("time") or {}).get("created")
        return True, _when(created)
    if eco in ("pip", "pypi"):
        doc = fetch(f"https://pypi.org/pypi/{quote(name)}/json")
        if doc is None:
            return False, None
        uploads = [f.get("upload_time_iso_8601") for files in (doc.get("releases") or {}).values() for f in files]
        dates = [d for d in (_when(u) for u in uploads if u) if d]
        return True, min(dates) if dates else None
    if eco == "maven":
        group, _, artifact = name.partition(":")
        doc = fetch("https://search.maven.org/solrsearch/select?q="
                    + quote(f'g:"{group}" AND a:"{artifact}"') + "&core=gav&rows=200&wt=json")
        if doc is None:
            return True, None
        docs = (doc.get("response") or {}).get("docs") or []
        if not docs:
            return False, None
        oldest = min(int(d.get("timestamp", 0)) for d in docs)
        return True, datetime.fromtimestamp(oldest / 1000, tz=timezone.utc) if oldest else None
    return True, None


def scorecard(source_url: str, fetch=_get_json) -> float | None:
    if not source_url or "github.com/" not in source_url:
        return None
    repo = source_url.split("github.com/", 1)[1].strip("/").removesuffix(".git")
    try:
        doc = fetch(f"https://api.securityscorecards.dev/projects/github.com/{repo}")
    except Exception:
        return None
    return float(doc["score"]) if doc and doc.get("score") is not None else None


def _when(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


# ------------------------------------------------------------------------------ the check

def _license_ok(license_expr: str, policy: dict) -> tuple[str, str]:
    """('ok' | 'denied' | 'unknown' | 'not_allowed', why)."""
    if not license_expr or license_expr.upper() in ("NOASSERTION", "OTHER", "UNKNOWN"):
        return "unknown", "no license GitHub could identify"
    ids = [p.strip("() ") for p in license_expr.replace(" AND ", " OR ").replace(" WITH ", " OR ").split(" OR ")]
    if any(any(i.startswith(d) for d in policy["denied_licenses"]) for i in ids) and \
            not any(i in policy["allowed_licenses"] for i in ids):
        return "denied", f"license {license_expr} isn't allowed in the product"
    if any(i in policy["allowed_licenses"] for i in ids):
        return "ok", ""
    return "not_allowed", f"license {license_expr} isn't on the allowed list (config/dependencies.yaml)"


def check(added: list[dict], policy: dict, *, fetch=_get_json, now: float | None = None) -> list[dict]:
    """Findings for the packages a change adds: [{name, version, severity, rule, why}]."""
    out = []
    now_dt = datetime.fromtimestamp(now if now is not None else time.time(), tz=timezone.utc)
    for d in added:
        name, version, eco = d.get("name", ""), d.get("version", ""), d.get("ecosystem", "")
        try:
            exists, first = first_published(eco, name, fetch)
        except Exception:
            exists, first = True, None
        if not exists:
            out.append({"name": name, "version": version, "severity": "critical", "rule": "not-in-registry",
                        "why": f"{name} isn't published in {eco or 'its registry'}: the name may be invented"})
            continue
        if first is not None:
            days = (now_dt - first).days
            if days < int(policy["min_age_days"]):
                out.append({"name": name, "version": version, "severity": "high", "rule": "too-new",
                            "why": f"{name} was first published {days} days ago (under {policy['min_age_days']}): "
                                   "new names are where squatting happens"})
        state, why = _license_ok(d.get("license") or "", policy)
        if state in ("denied", "not_allowed"):
            out.append({"name": name, "version": version, "severity": "high", "rule": f"license-{state}", "why": why})
        elif state == "unknown":
            out.append({"name": name, "version": version, "severity": "medium", "rule": "license-unknown",
                        "why": f"{name}: {why}"})
        score = scorecard(d.get("source_repository_url") or "", fetch)
        if score is not None and score < float(policy["min_scorecard"]):
            out.append({"name": name, "version": version, "severity": "medium", "rule": "scorecard",
                        "why": f"{name}: OpenSSF Scorecard {score:g} (under {policy['min_scorecard']:g})"})
    return out


class NewDependencies:
    """Quality source: the packages each pull request adds, checked as above."""

    def __init__(self, services, fetch=None) -> None:
        self.services = services
        self.fetch = fetch or _get_json

    def collect(self, suite, spec, targets, stage):
        policy = load_policy()
        out = []
        for t in targets:
            diff = self.services.github.dependency_diff(repo=t["repo"], base=t.get("base") or "main", head=t["sha"])
            if diff is None:
                out.append(SuiteResult(suite=suite, kind="security", stage=stage, state="missing", repo=t["repo"]))
                continue
            added = [d for d in diff if d.get("change_type") == "added"]
            findings = [Finding(suite=suite, tool="new-dependencies", rule=f["rule"], severity=f["severity"],
                                title=f"{f['name']}@{f['version']}: {f['why']}", repo=t["repo"], url="",
                                fingerprint=f"{t['repo']}:{f['rule']}:{f['name']}")
                        for f in check(added, policy, fetch=self.fetch)]
            out.append(SuiteResult(suite=suite, kind="security", stage=stage, state="passed", repo=t["repo"],
                                   findings=findings,
                                   details={"added": [f"{d.get('ecosystem')}:{d.get('name')}@{d.get('version')}"
                                                      for d in added]}))
        return out

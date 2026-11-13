"""Packages a change adds: real, not brand new, licensed for the product, from a maintained source;
and a change to a dependency manifest waits for a person at G5."""

from __future__ import annotations

from datetime import datetime, timezone

from valxel_sdlc import dependencies as deps
from valxel_sdlc.graph.phase import risk_tier

NOW = datetime(2026, 11, 12, tzinfo=timezone.utc).timestamp()
REGISTRY = {
    "https://registry.npmjs.org/left-pad": {"time": {"created": "2016-03-14T00:00:00Z"}},
    "https://registry.npmjs.org/reactt-dom-utils": {"time": {"created": "2026-11-05T00:00:00Z"}},
    "https://registry.npmjs.org/@acme%2Fui": {"time": {"created": "2024-01-01T00:00:00Z"}},
    "https://pypi.org/pypi/requests/json": {"releases": {"2.0": [{"upload_time_iso_8601": "2013-09-24T00:00:00Z"}]}},
    "https://api.securityscorecards.dev/projects/github.com/acme/ui": {"score": 2.1},
}


def fetch(url):
    return REGISTRY.get(url)


def add(name, eco="npm", license="MIT", src=""):
    return {"change_type": "added", "name": name, "version": "1.0.0", "ecosystem": eco, "license": license,
            "source_repository_url": src}


def test_each_added_package_is_checked():
    policy = deps.load_policy()
    found = deps.check([add("left-pad"), add("requests", eco="pip", license="Apache-2.0"),
                        add("lodash-hallucinated"), add("reactt-dom-utils"),
                        add("@acme/ui", license="GPL-3.0-only", src="https://github.com/acme/ui"),
                        add("left-pad", license="NOASSERTION")], policy, fetch=fetch, now=NOW)
    by = {(f["name"], f["rule"]): f for f in found}
    assert ("left-pad", "too-new") not in by and ("requests", "too-new") not in by
    assert by[("lodash-hallucinated", "not-in-registry")]["severity"] == "critical"
    assert by[("reactt-dom-utils", "too-new")]["severity"] == "high" and "7 days ago" in by[("reactt-dom-utils", "too-new")]["why"]
    assert by[("@acme/ui", "license-denied")]["severity"] == "high"
    assert by[("@acme/ui", "scorecard")]["severity"] == "medium"
    assert by[("left-pad", "license-unknown")]["severity"] == "medium"
    # A dual license with an allowed choice is fine.
    assert not deps.check([add("left-pad", license="MIT OR GPL-3.0")], policy, fetch=fetch, now=NOW)


def test_the_suite_reports_findings_against_block_on():
    class GH:
        def dependency_diff(self, *, repo, base, head):
            return [add("lodash-hallucinated"), {"change_type": "removed", "name": "old", "ecosystem": "npm"}]

    class S:
        github = GH()

    [r] = deps.NewDependencies(S, fetch=fetch).collect("new-dependencies", {"kind": "security"},
                                                       [{"repo": "acme/web", "sha": "abc"}], "pr")
    assert r.state == "passed" and [f.severity for f in r.findings] == ["critical"]
    assert r.details["added"] == ["npm:lodash-hallucinated@1.0.0"]


def test_a_dependency_manifest_makes_the_change_high_risk():
    class Lane:
        data_class, high_risk_paths = "general", ("src/auth/",)

    lane = Lane()
    assert risk_tier({}, lane, [{"repo": "acme/web", "files": ["src/app.ts"]}]) == "low"
    assert risk_tier({}, lane, [{"repo": "acme/web", "files": ["package.json"]}]) == "high"
    assert risk_tier({}, lane, [{"repo": "acme/api", "files": ["gradle/libs.versions.toml"]}]) == "high"
    assert deps.is_manifest("services/api/pyproject.toml") and not deps.is_manifest("src/package.ts")

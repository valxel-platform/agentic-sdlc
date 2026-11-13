"""Package registry for shared-services releases (libraries, SDKs, charts). Shared lanes own no
runtime: a merged change publishes a release candidate, G6 marks it stable, and each consuming
product adopts the version in its own runtime through its own change sets.

Releases are driven by Git tags, so every registry and language works the same way:

  merge     the tool tags the merge commit  v1.5.0-rc.1   the repo's release workflow builds and
                                                          publishes 1.5.0-rc.1 to GitHub Packages
  G6        the tool tags the same commit   v1.5.0        the workflow publishes 1.5.0 from it

The tool never builds or uploads a package, and needs no registry credentials: the repo's own
workflow publishes with its GITHUB_TOKEN. The tool reads GitHub Packages only to check that a
release candidate was published before G6 marks it stable. The next version comes from the repo's
latest stable tag: a minor bump by default, patch for a hotfix, major for a breaking contract.
"""

from __future__ import annotations

import re
import threading
from typing import Protocol
from urllib.parse import quote

PACKAGE_TYPES = ("maven", "npm", "nuget", "rubygems", "container", "docker")
BUMPS = ("major", "minor", "patch")
_STABLE = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")
_RC = re.compile(r"^v(\d+\.\d+\.\d+)-rc\.(\d+)$")


class PackageRegistry(Protocol):
    def publish(self, *, repo: str, version: str | None, channel: str, actor: str, sha: str = "",
                bump: str = "minor") -> dict: ...
    def status(self, *, repo: str, version: str, package: dict | None) -> str: ...  # published | missing | unknown


def next_version(tags: list[str], bump: str = "minor") -> str:
    """The version after the highest stable tag (v0.0.0 when there is none)."""
    stable = [tuple(int(x) for x in m.groups()) for t in tags if (m := _STABLE.match(t))]
    major, minor, patch = max(stable, default=(0, 0, 0))
    if bump == "major":
        return f"{major + 1}.0.0"
    if bump == "patch":
        return f"{major}.{minor}.{patch + 1}"
    return f"{major}.{minor + 1}.0"


def next_rc(tags: list[str], version: str) -> int:
    return 1 + max((int(m.group(2)) for t in tags if (m := _RC.match(t)) and m.group(1) == version), default=0)


def _record(repo, version, channel, actor, sha, tag) -> dict:
    return {"repo": repo, "version": version, "channel": channel, "actor": actor, "sha": sha, "tag": tag,
            "package_version": tag[1:]}


class GitHubPackages:
    """Tags the release commits on GitHub; checks GitHub Packages for what the workflows published."""

    def __init__(self, github) -> None:
        self.github = github  # integrations.github.GitHubRest

    def _tags(self, repo: str) -> list[str]:
        refs = self.github._call("GET", repo, "/git/matching-refs/tags/v") or []
        return [r["ref"].removeprefix("refs/tags/") for r in refs]

    def _tag(self, repo: str, tag: str, sha: str) -> None:
        have = self.github._call("GET", repo, f"/git/ref/tags/{quote(tag, safe='')}", ok404=True)
        if have:
            if have["object"]["sha"] != sha:
                raise ValueError(f"{repo}: tag {tag} already points at {have['object']['sha'][:7]}, not {sha[:7]}")
            return  # a retry: already tagged
        self.github._call("POST", repo, "/git/refs", {"ref": f"refs/tags/{tag}", "sha": sha})

    def publish(self, *, repo, version, channel, actor, sha="", bump="minor") -> dict:
        if channel not in ("rc", "stable"):
            raise ValueError(f"Unknown channel {channel!r}")
        if not sha:
            raise ValueError(f"{repo}: a release needs the merge commit")
        tags = self._tags(repo)
        version = version or next_version(tags, bump)
        tag = f"v{version}-rc.{next_rc(tags, version)}" if channel == "rc" else f"v{version}"
        self._tag(repo, tag, sha)
        return _record(repo, version, channel, actor, sha, tag)

    def status(self, *, repo, version, package=None) -> str:
        """Whether the workflow published `version` (e.g. 1.5.0-rc.1) to GitHub Packages."""
        if not package or package.get("type") not in PACKAGE_TYPES or not package.get("name"):
            return "unknown"  # the repo declares no package: nothing to check
        org = repo.split("/", 1)[0]
        try:
            versions = self.github._abs(
                "GET", org, f"/orgs/{quote(org)}/packages/{package['type']}/{quote(package['name'], safe='')}"
                            "/versions?per_page=100", ok404=True)
        except Exception:
            return "unknown"
        if versions is None:
            return "missing"
        names = {v.get("name") for v in versions}
        names |= {t for v in versions for t in ((v.get("metadata") or {}).get("container") or {}).get("tags") or []}
        return "published" if version in names else "missing"


class StubPackageRegistry:
    """In memory: tags per repo; every release candidate counts as published."""

    def __init__(self) -> None:
        self.releases: list[dict] = []
        self.tags: dict[str, dict[str, str]] = {}
        self.missing: set[tuple[str, str]] = set()  # tests: (repo, version) a workflow failed to publish
        self._lock = threading.Lock()

    def publish(self, *, repo, version, channel, actor, sha="", bump="minor") -> dict:
        if channel not in ("rc", "stable"):
            raise ValueError(f"Unknown channel {channel!r}")
        with self._lock:
            tags = self.tags.setdefault(repo, {})
            version = version or next_version(list(tags), bump)
            tag = f"v{version}-rc.{next_rc(list(tags), version)}" if channel == "rc" else f"v{version}"
            tags.setdefault(tag, sha)
            rec = _record(repo, version, channel, actor, sha, tag)
            self.releases.append(rec)
        return dict(rec)

    def status(self, *, repo, version, package=None) -> str:
        return "missing" if (repo, version) in self.missing else "published"

    def channel(self, repo: str, version: str) -> str | None:
        with self._lock:
            hits = [r["channel"] for r in self.releases if r["repo"] == repo and r["version"] == version]
        return hits[-1] if hits else None

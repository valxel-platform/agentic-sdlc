"""Package registry for shared-services releases (libraries, SDKs, charts). Shared lanes own no
runtime: a merged change publishes a release candidate, G6 marks it stable, and each consuming
product adopts the version in its own runtime through its own change sets."""

from __future__ import annotations

import threading
from typing import Protocol


class PackageRegistry(Protocol):
    def publish(self, *, repo: str, version: str, channel: str, actor: str) -> dict: ...


class StubPackageRegistry:
    def __init__(self) -> None:
        self.releases: list[dict] = []
        self._lock = threading.Lock()

    def publish(self, *, repo, version, channel, actor) -> dict:
        if channel not in ("rc", "stable"):
            raise ValueError(f"Unknown channel {channel!r}")
        rec = {"repo": repo, "version": version, "channel": channel, "actor": actor}
        with self._lock:
            self.releases.append(rec)
        return dict(rec)

    def channel(self, repo: str, version: str) -> str | None:
        with self._lock:
            hits = [r["channel"] for r in self.releases if r["repo"] == repo and r["version"] == version]
        return hits[-1] if hits else None

"""GitHub interface. Repos are "<org>/<repo>": each product's code lives in its own org.
The stub keeps PRs in memory; a real adapter uses the same GitHub App as the governance
loader, installed per product org, with tokens scoped to the change set's repos."""

from __future__ import annotations

import itertools
import threading
from typing import Protocol


class GitHub(Protocol):
    def open_pr(self, *, repo: str, branch: str, title: str, body: str, files: list[str], labels: list[str]) -> dict: ...
    def merge(self, *, repo: str, number: int, actor: str) -> dict: ...


class StubGitHub:
    def __init__(self) -> None:
        self.prs: dict[str, dict] = {}
        self._seq = itertools.count(100)
        self._lock = threading.Lock()

    def open_pr(self, *, repo, branch, title, body, files, labels) -> dict:
        with self._lock:
            number = next(self._seq)
            pr = {
                "repo": repo,
                "number": number,
                "branch": branch,
                "title": title,
                "body": body,
                "files": list(files),
                "labels": list(labels),
                "state": "open",
                "url": f"https://github.com/{repo}/pull/{number}",
            }
            self.prs[f"{repo}#{number}"] = pr
            return dict(pr)

    def merge(self, *, repo, number, actor) -> dict:
        with self._lock:
            pr = self.prs[f"{repo}#{number}"]
            pr["state"] = "merged"
            pr["merged_by"] = actor
            return dict(pr)

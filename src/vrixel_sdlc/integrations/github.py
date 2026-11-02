"""GitHub interface for delivery. Repos are "<org>/<repo>": each product's code lives in its own org.

`GitHubRest` uses the same GitHub App as the governance loader, installed in every product
org, with installation tokens narrowed to what delivery needs (contents, pull requests and
commit statuses: write; checks: read). Every call is safe to repeat, because graph nodes and
the watcher can retry: a PR is found by its head branch before one is created, a merged PR is
reported as merged, and a file is written only when its content changed.

`StubGitHub` keeps everything in memory. Its CI result defaults to success; tests set
`ci[(repo, sha)]` to "pending" or "failure" to exercise the waits.
"""

from __future__ import annotations

import base64
import hashlib
import itertools
import json
import threading
from typing import Protocol
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, build_opener

DELIVERY_PERMISSIONS = {"contents": "write", "pull_requests": "write", "statuses": "write",
                        "checks": "read", "metadata": "read", "security_events": "read",
                        "secret_scanning_alerts": "read", "vulnerability_alerts": "read"}


class GitHubError(RuntimeError):
    pass


class GitHub(Protocol):
    def open_pr(self, *, repo: str, branch: str, title: str, body: str, files: list[str], labels: list[str]) -> dict: ...
    def merge(self, *, repo: str, number: int, actor: str) -> dict: ...
    def put_file(self, *, repo: str, branch: str, path: str, content: str, message: str) -> dict: ...
    def checks(self, *, repo: str, sha: str) -> dict: ...  # {"state": pending|success|failure, "checks": [...]}
    def set_status(self, *, repo: str, sha: str, context: str, state: str, description: str,
                   url: str = "") -> None: ...
    # Security results; None means the feature is not enabled on the repo.
    def code_scanning_alerts(self, *, repo: str, pr: int | None = None, ref: str | None = None) -> list | None: ...
    def dependency_diff(self, *, repo: str, base: str, head: str) -> list | None: ...
    def secret_alerts(self, *, repo: str) -> list | None: ...


class StubGitHub:
    def __init__(self) -> None:
        self.prs: dict[str, dict] = {}
        self.files: dict[tuple[str, str, str], str] = {}  # (repo, branch, path) -> content
        self.ci: dict[tuple[str, str], str] = {}  # (repo, sha) -> pending | success | failure
        self.ci_default = "success"
        self.statuses: list[dict] = []
        self.code_alerts: dict[str, list] = {}  # repo -> code-scanning alerts
        self.dep_diffs: dict[str, list] = {}  # repo -> dependency-review diff
        self.secrets: dict[str, list] = {}  # repo -> secret-scanning alerts
        self._seq = itertools.count(100)
        self._lock = threading.Lock()

    def open_pr(self, *, repo, branch, title, body, files, labels) -> dict:
        with self._lock:
            for pr in self.prs.values():  # one open PR per head branch, as on GitHub
                if pr["repo"] == repo and pr["branch"] == branch and pr["state"] == "open":
                    pr.update(title=title, body=body)
                    return dict(pr)
            number = next(self._seq)
            sha = hashlib.sha1(f"{repo}#{number}:{body}".encode()).hexdigest()
            pr = {"repo": repo, "number": number, "branch": branch, "base": "main", "title": title, "body": body,
                  "files": list(files), "labels": list(labels), "state": "open", "head_sha": sha,
                  "url": f"https://github.com/{repo}/pull/{number}"}
            self.prs[f"{repo}#{number}"] = pr
            return dict(pr)

    def merge(self, *, repo, number, actor) -> dict:
        with self._lock:
            pr = self.prs[f"{repo}#{number}"]
            if pr["state"] != "merged":
                pr.update(state="merged", merged_by=actor,
                          merge_sha=hashlib.sha1(f"merge:{repo}#{number}".encode()).hexdigest())
            return dict(pr)

    def put_file(self, *, repo, branch, path, content, message) -> dict:
        with self._lock:
            self.files[(repo, branch, path)] = content
        return {"repo": repo, "branch": branch, "path": path, "message": message,
                "sha": hashlib.sha1(content.encode()).hexdigest()}

    def checks(self, *, repo, sha) -> dict:
        state = self.ci.get((repo, sha), self.ci_default)
        return {"state": state, "checks": [{"name": "ci", "state": state,
                                            "url": f"https://github.com/{repo}/commit/{sha}/checks"}]}

    def set_status(self, *, repo, sha, context, state, description, url="") -> None:
        with self._lock:
            self.statuses.append({"repo": repo, "sha": sha, "context": context, "state": state,
                                  "description": description, "url": url})

    def code_scanning_alerts(self, *, repo, pr=None, ref=None):
        return list(self.code_alerts.get(repo, []))

    def dependency_diff(self, *, repo, base, head):
        return list(self.dep_diffs.get(repo, []))

    def secret_alerts(self, *, repo):
        return list(self.secrets.get(repo, []))


class GitHubRest:
    """GitHub REST API with per-org installation tokens (see GitHubAppTokenProvider)."""

    def __init__(self, tokens, *, api: str = "https://api.github.com", opener=None, timeout: float = 20.0,
                 merge_method: str = "squash") -> None:
        self.tokens = tokens
        self.api = api.rstrip("/")
        self.opener = opener or build_opener()
        self.timeout = timeout
        self.merge_method = merge_method

    # -- plumbing
    def _call(self, method: str, repo: str, path: str, body: dict | None = None, *, ok404: bool = False):
        org = repo.split("/", 1)[0]
        data = json.dumps(body).encode() if body is not None else None
        req = Request(f"{self.api}/repos/{repo}{path}", data=data, method=method, headers={
            "Authorization": f"Bearer {self.tokens.token(org)}", "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "vrixel-agentic-sdlc",
            **({"Content-Type": "application/json"} if data else {}),
        })
        try:
            with self.opener.open(req, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8")
                return json.loads(raw) if raw else {}
        except HTTPError as exc:
            if ok404 and exc.code == 404:
                return None
            detail = ""
            try:
                detail = json.loads(exc.read().decode("utf-8")).get("message", "")
            except Exception:
                pass
            raise GitHubError(f"GitHub {method} {repo}{path}: {exc.code} {detail}".strip()) from exc

    def _default_branch(self, repo: str) -> str:
        return self._call("GET", repo, "")["default_branch"]

    def ensure_branch(self, repo: str, branch: str) -> str:
        """The branch's head SHA, creating the branch from the default branch when missing."""
        ref = self._call("GET", repo, f"/git/ref/heads/{quote(branch, safe='')}", ok404=True)
        if ref:
            return ref["object"]["sha"]
        base = self._call("GET", repo, f"/git/ref/heads/{quote(self._default_branch(repo), safe='')}")
        self._call("POST", repo, "/git/refs", {"ref": f"refs/heads/{branch}", "sha": base["object"]["sha"]})
        return base["object"]["sha"]

    @staticmethod
    def _pr(repo: str, pr: dict) -> dict:
        merged = bool(pr.get("merged") or pr.get("merged_at"))
        return {"repo": repo, "number": pr["number"], "branch": pr["head"]["ref"],
                "base": (pr.get("base") or {}).get("ref", ""), "title": pr["title"],
                "state": "merged" if merged else pr["state"], "head_sha": pr["head"]["sha"],
                "merge_sha": pr.get("merge_commit_sha") if merged else None, "url": pr["html_url"],
                "labels": [lbl["name"] for lbl in pr.get("labels", [])]}

    # -- interface
    def put_file(self, *, repo, branch, path, content, message) -> dict:
        self.ensure_branch(repo, branch)
        qpath = quote(path)
        current = self._call("GET", repo, f"/contents/{qpath}?ref={quote(branch, safe='')}", ok404=True)
        if current and isinstance(current, dict):
            existing = base64.b64decode(current.get("content", "")).decode("utf-8", "replace")
            if existing == content:
                return {"repo": repo, "branch": branch, "path": path, "sha": current["sha"], "unchanged": True}
        body = {"message": message, "branch": branch,
                "content": base64.b64encode(content.encode("utf-8")).decode()}
        if current:
            body["sha"] = current["sha"]
        res = self._call("PUT", repo, f"/contents/{qpath}", body)
        return {"repo": repo, "branch": branch, "path": path, "sha": res["commit"]["sha"]}

    def open_pr(self, *, repo, branch, title, body, files, labels) -> dict:
        org = repo.split("/", 1)[0]
        found = self._call("GET", repo, f"/pulls?state=all&head={quote(org)}:{quote(branch, safe='')}")
        live = [p for p in found or [] if p["state"] == "open" or p.get("merged_at")]
        if live:
            pr = live[0]
            if pr["state"] == "open" and (pr["title"] != title or (pr.get("body") or "") != body):
                pr = self._call("PATCH", repo, f"/pulls/{pr['number']}", {"title": title, "body": body})
        else:
            pr = self._call("POST", repo, "/pulls", {"title": title, "head": branch, "body": body,
                                                     "base": self._default_branch(repo)})
        if labels and not set(labels) <= {lbl["name"] for lbl in pr.get("labels", [])}:
            self._call("POST", repo, f"/issues/{pr['number']}/labels", {"labels": labels})
            pr = {**pr, "labels": [{"name": n} for n in labels]}
        return self._pr(repo, pr)

    def merge(self, *, repo, number, actor) -> dict:
        pr = self._call("GET", repo, f"/pulls/{number}")
        if not pr.get("merged"):
            # Branch protection still applies: required checks and reviews must pass, or GitHub refuses.
            self._call("PUT", repo, f"/pulls/{number}/merge", {
                "merge_method": self.merge_method, "sha": pr["head"]["sha"],
                "commit_title": f"{pr['title']} (#{number})",
                "commit_message": f"Merged by the Vrixel merge train. Approved by {actor}."})
            pr = self._call("GET", repo, f"/pulls/{number}")
        return {**self._pr(repo, pr), "merged_by": actor}

    def checks(self, *, repo, sha) -> dict:
        """Check runs (GitHub Actions and apps) plus legacy commit statuses, rolled up."""
        runs = self._call("GET", repo, f"/commits/{sha}/check-runs?per_page=100") or {}
        combined = self._call("GET", repo, f"/commits/{sha}/status") or {}
        out = []
        for r in runs.get("check_runs", []):
            if r["status"] != "completed":
                state = "pending"
            elif r.get("conclusion") in ("success", "neutral", "skipped"):
                state = "success"
            else:
                state = "failure"
            output = r.get("output") or {}
            out.append({"name": r["name"], "state": state, "url": r.get("html_url", ""),
                        "summary": " ".join(filter(None, [output.get("title"), output.get("summary")]))})
        for st in combined.get("statuses", []):
            if st["context"].startswith("openchoreo/"):
                continue  # our own reports, not CI
            state = {"success": "success", "pending": "pending"}.get(st["state"], "failure")
            out.append({"name": st["context"], "state": state, "url": st.get("target_url") or "",
                        "summary": st.get("description") or ""})
        if any(c["state"] == "failure" for c in out):
            state = "failure"
        elif any(c["state"] == "pending" for c in out):
            state = "pending"
        else:
            state = "success"  # no CI configured counts as passing; branch protection is the backstop
        return {"state": state, "checks": out}

    def set_status(self, *, repo, sha, context, state, description, url="") -> None:
        body = {"state": state, "context": context, "description": description[:140]}
        if url:
            body["target_url"] = url
        self._call("POST", repo, f"/statuses/{sha}", body)

    def _maybe(self, repo: str, path: str) -> list | None:
        """GitHub answers 404 or 403 when a security feature is off for the repo."""
        try:
            res = self._call("GET", repo, path, ok404=True)
        except GitHubError as exc:
            if " 403" in str(exc):
                return None
            raise
        return res if isinstance(res, list) or res is None else res.get("items", [])

    def code_scanning_alerts(self, *, repo, pr=None, ref=None):
        q = "state=open&per_page=100" + (f"&pr={pr}" if pr else f"&ref={quote(ref, safe='')}" if ref else "")
        return self._maybe(repo, f"/code-scanning/alerts?{q}")

    def dependency_diff(self, *, repo, base, head):
        return self._maybe(repo, f"/dependency-graph/compare/{quote(base, safe='')}...{quote(head, safe='')}")

    def secret_alerts(self, *, repo):
        return self._maybe(repo, "/secret-scanning/alerts?state=open&per_page=100")

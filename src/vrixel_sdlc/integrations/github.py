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


# Creating repos and applying their settings, used only by the provision step.
PROVISIONING_PERMISSIONS = {"administration": "write", "contents": "write", "pull_requests": "write",
                            "security_events": "write", "secret_scanning_alerts": "write",
                            "vulnerability_alerts": "write", "repository_custom_properties": "write",
                            "organization_custom_properties": "admin", "members": "read", "metadata": "read"}


class GitHubError(RuntimeError):
    def __init__(self, message: str, status: int = 0) -> None:
        super().__init__(message)
        self.status = status


def ruleset_body(spec: dict, *, target: str = "branch") -> dict:
    """A repository ruleset as GitHub's API takes it, from the standards' ruleset or tag_ruleset."""
    rules: list[dict] = []
    if target == "tag":
        rules = [{"type": "deletion"}, {"type": "non_fast_forward"}, {"type": "update"}]
        include = [f"refs/tags/{spec.get('pattern', 'v*')}"]
    else:
        include = ["~DEFAULT_BRANCH"]
        if spec.get("block_deletion", True):
            rules.append({"type": "deletion"})
        if spec.get("block_force_push", True):
            rules.append({"type": "non_fast_forward"})
        if spec.get("linear_history"):
            rules.append({"type": "required_linear_history"})
        rules.append({"type": "pull_request", "parameters": {
            "required_approving_review_count": int(spec.get("required_approvals", 0)),
            "dismiss_stale_reviews_on_push": bool(spec.get("dismiss_stale_reviews", True)),
            "require_code_owner_review": bool(spec.get("code_owner_review", True)),
            "require_last_push_approval": bool(spec.get("require_last_push_approval", False)),
            "required_review_thread_resolution": bool(spec.get("require_conversation_resolution", True))}})
        checks = [{"context": c} for c in spec.get("required_checks") or []]
        if spec.get("gates_check"):
            # Only the platform's App may set the gates status, when its id is known.
            gates = {"context": spec["gates_check"]}
            if spec.get("bypass_app_id"):
                gates["integration_id"] = int(spec["bypass_app_id"])
            checks.append(gates)
        if checks:
            rules.append({"type": "required_status_checks", "parameters": {
                "strict_required_status_checks_policy": bool(spec.get("up_to_date_before_merge", False)),
                "required_status_checks": checks}})
        if spec.get("code_scanning_merge_protection"):
            rules.append({"type": "code_scanning", "parameters": {"code_scanning_tools": [{
                "tool": "CodeQL", "alerts_threshold": "errors",
                "security_alerts_threshold": f"{spec['code_scanning_merge_protection']}_or_higher"}]}})
    bypass = []
    # The merge train merges after the pipeline's own gates (G5 for high-risk changes).
    if spec.get("bypass_app_id"):
        bypass.append({"actor_id": int(spec["bypass_app_id"]), "actor_type": "Integration", "bypass_mode": "pull_request"})
    if spec.get("admin_bypass", "none") != "none" and target == "branch":
        bypass.append({"actor_id": 5, "actor_type": "RepositoryRole", "bypass_mode": spec["admin_bypass"]})
    return {"name": spec.get("name", "vrixel-default-branch" if target == "branch" else "vrixel-release-tags"),
            "target": target, "enforcement": "active",
            "conditions": {"ref_name": {"include": include, "exclude": []}}, "rules": rules,
            "bypass_actors": bypass}


def repo_settings_body(merge: dict, default_branch: str, description: str | None = None,
                       features: dict | None = None, is_template: bool | None = None) -> dict:
    body = {
        "allow_squash_merge": merge.get("squash", True), "allow_merge_commit": merge.get("merge_commit", False),
        "allow_rebase_merge": merge.get("rebase", False),
        "delete_branch_on_merge": merge.get("delete_branch_on_merge", True),
        "allow_auto_merge": merge.get("allow_auto_merge", False),
        "allow_update_branch": merge.get("allow_update_branch", False),
        "squash_merge_commit_title": merge.get("squash_title", "PR_TITLE"),
        "squash_merge_commit_message": merge.get("squash_message", "PR_BODY"),
        "default_branch": default_branch,
    }
    body.update({k: bool(v) for k, v in (features or {}).items()})
    if is_template is not None:
        body["is_template"] = bool(is_template)
    if description is not None:
        body["description"] = description
    return body


class GitHub(Protocol):
    def open_pr(self, *, repo: str, branch: str, title: str, body: str, files: list[str], labels: list[str]) -> dict: ...
    # sha: the head the gates approved; GitHub refuses the merge if the branch has moved since.
    def merge(self, *, repo: str, number: int, actor: str, sha: str = "") -> dict: ...
    def put_file(self, *, repo: str, branch: str, path: str, content: str, message: str) -> dict: ...
    def checks(self, *, repo: str, sha: str) -> dict: ...  # {"state": pending|success|failure, "checks": [...]}
    def set_status(self, *, repo: str, sha: str, context: str, state: str, description: str,
                   url: str = "") -> None: ...
    # Security results; None means the feature is not enabled on the repo.
    def code_scanning_alerts(self, *, repo: str, pr: int | None = None, ref: str | None = None) -> list | None: ...
    def dependency_diff(self, *, repo: str, base: str, head: str) -> list | None: ...
    def secret_alerts(self, *, repo: str) -> list | None: ...
    # Provisioning new repositories (see vrixel_sdlc.provisioning); each call is idempotent.
    def get_file(self, *, repo: str, path: str, ref: str) -> str | None: ...  # ref '' = default branch
    def create_repo(self, *, repo: str, template: str | None, private: bool, description: str) -> str: ...
    def update_repo(self, *, repo: str, merge: dict, default_branch: str, description: str) -> None: ...
    def enable_security(self, *, repo: str, **features: bool) -> None: ...
    def grant_team(self, *, org: str, team: str, repo: str, permission: str) -> None: ...
    def set_topics(self, *, repo: str, topics: list[str]) -> None: ...
    def set_properties(self, *, repo: str, properties: dict[str, str]) -> None: ...
    def upsert_ruleset(self, *, repo: str, branch: str, spec: dict) -> str: ...
    def dispatch(self, *, repo: str, event_type: str, payload: dict) -> None: ...
    # For the cost ledger: Actions runs with their jobs (started_at, completed_at, labels).
    def workflow_runs(self, *, repo: str, branch: str | None = None, head_sha: str | None = None,
                      event: str | None = None) -> list[dict]: ...


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
        self.repos: dict[str, dict] = {}  # provisioned repos and the settings applied to them
        self.fail_steps: set[str] = set()  # tests: provisioning calls that should fail
        self.ci_fail_once: set[str] = set()  # tests: repos whose first PR commit fails CI
        self.dispatches: list[dict] = []
        # Performance tests the tool asks for: the template's job reports this result.
        self.perf_contexts = ("perf-smoke / report", "performance / report")
        self.perf_result = "success"
        # Store apps' release workflows (vrixel_sdlc.store): the stub answers a dispatch with the
        # statuses the workflow would post; set to "pending" or "failure" to test waiting and failing.
        self.release_result = "success"
        # Actions runs for the cost ledger: (repo, branch | sha | event) -> runs. Unset, a PR's branch
        # has one five-minute Linux run.
        self.actions: dict[tuple[str, str], list[dict]] = {}
        self._seq = itertools.count(100)
        self._lock = threading.Lock()

    def open_pr(self, *, repo, branch, title, body, files, labels) -> dict:
        with self._lock:
            for pr in self.prs.values():  # one open PR per head branch, as on GitHub
                if pr["repo"] == repo and pr["branch"] == branch and pr["state"] == "open":
                    # The agent pushed again (a retry): the head moves, and CI runs on the new commit.
                    pr["pushes"] = pr.get("pushes", 0) + 1
                    pr["labels"] = list(dict.fromkeys(list(pr.get("labels") or []) + list(labels)))  # labels add up, as on GitHub
                    pr.update(title=title, body=body, head_sha=hashlib.sha1(
                        f"{repo}#{pr['number']}:{body}:{pr['pushes']}".encode()).hexdigest())
                    return {k: v for k, v in pr.items() if k != "pushes"}
            number = next(self._seq)
            sha = hashlib.sha1(f"{repo}#{number}:{body}".encode()).hexdigest()
            pr = {"repo": repo, "number": number, "branch": branch, "base": "main", "title": title, "body": body,
                  "files": list(files), "labels": list(labels), "state": "open", "head_sha": sha,
                  "url": f"https://github.com/{repo}/pull/{number}"}
            self.prs[f"{repo}#{number}"] = pr
            if repo in self.ci_fail_once:  # tests: this repo's first commit fails CI
                self.ci[(repo, sha)] = "failure"
            return dict(pr)

    def merge(self, *, repo, number, actor, sha="") -> dict:
        with self._lock:
            pr = self.prs[f"{repo}#{number}"]
            if pr["state"] != "merged" and sha and sha != pr["head_sha"]:
                raise GitHubError(f"{repo}#{number}: head branch was modified since {sha[:7]}", 409)
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
        out = [{"name": "ci", "state": state, "url": f"https://github.com/{repo}/commit/{sha}/checks"}]
        latest: dict[str, dict] = {}
        for st in self.statuses:  # commit statuses too, as on GitHub (the latest per context)
            if st["repo"] == repo and st["sha"] == sha and not st["context"].startswith(("openchoreo/", "vrixel/")):
                latest[st["context"]] = {"name": st["context"], "state": st["state"], "url": st.get("url", ""),
                                         "summary": st.get("description", "")}
        out += latest.values()
        # Rolled up like GitHub's: a failing or pending status counts, not only the CI run.
        if any(c["state"] == "failure" for c in out):
            state = "failure"
        elif any(c["state"] == "pending" for c in out):
            state = "pending"
        return {"state": state, "checks": out}

    def set_status(self, *, repo, sha, context, state, description, url="") -> None:
        with self._lock:
            self.statuses.append({"repo": repo, "sha": sha, "context": context, "state": state,
                                  "description": description, "url": url})
            if state == "pending" and context in self.perf_contexts:
                # The template's performance job answers the status the tool opened (perf_result).
                self.statuses.append({"repo": repo, "sha": sha, "context": context, "state": self.perf_result,
                                      "description": "p95 120 ms (budget 500, main 115, +4%), errors 0.0%", "url": ""})

    def code_scanning_alerts(self, *, repo, pr=None, ref=None):
        return list(self.code_alerts.get(repo, []))

    def dependency_diff(self, *, repo, base, head):
        return list(self.dep_diffs.get(repo, []))

    def secret_alerts(self, *, repo):
        return list(self.secrets.get(repo, []))

    def workflow_runs(self, *, repo, branch=None, head_sha=None, event=None) -> list[dict]:
        key = (repo, branch or head_sha or event or "")
        if key in self.actions:
            return [dict(r) for r in self.actions[key]]
        if not branch:
            return []
        at = "2026-11-02T15:00:00+00:00"
        return [{"id": sum(map(ord, f"{repo}{branch}")), "name": "ci", "display_title": branch,
                 "event": "pull_request", "status": "completed", "created_at": at,
                 "updated_at": "2026-11-02T15:05:00+00:00",
                 "jobs": [{"started_at": at, "completed_at": "2026-11-02T15:04:30+00:00", "labels": ["ubuntu-latest"]}]}]

    def dispatch(self, *, repo, event_type, payload) -> None:
        with self._lock:
            self.dispatches.append({"repo": repo, "event_type": event_type, "payload": dict(payload)})
        sha, result = payload.get("sha", ""), self.release_result
        if event_type == "vrixel-release" and result != "pending":
            stage = payload.get("stage", "")
            suite = {"dev": "integration / report", "staging": "e2e / report"}.get(stage)
            if suite:
                self.set_status(repo=repo, sha=sha, context=suite, state=result, description="3 passed, 0 failed")
            self.set_status(repo=repo, sha=sha, context=f"release / {stage}", state=result, description="stub release")
        if event_type == "vrixel-rollout" and payload.get("context") and result != "pending":
            self.set_status(repo=repo, sha=sha, context=payload["context"], state=result,
                            description="crash-free 99.8% of 1200 users (floor 99.00%)")

    def _repo(self, repo: str, call: str) -> dict:
        if call in self.fail_steps:
            raise GitHubError(f"GitHub {call} {repo}: 403 Resource not accessible by integration")
        return self.repos.setdefault(repo, {})

    def get_file(self, *, repo, path, ref):
        return self.files.get((repo, ref or "main", path))

    def create_repo(self, *, repo, template, private, description) -> str:
        r = self._repo(repo, "create_repo")
        created = "created" not in r
        r.setdefault("created", {"template": template, "private": private, "description": description})
        if created and template:  # a repo made from a template starts with its files
            with self._lock:
                for (src, branch, path), content in list(self.files.items()):
                    if src == template and branch == "main":
                        self.files.setdefault((repo, "main", path), content)
        return "created" if created else "exists"

    def update_repo(self, *, repo, merge, default_branch, description=None, features=None, is_template=None) -> None:
        r = self._repo(repo, "update_repo")
        r["settings"] = {"merge": dict(merge), "default_branch": default_branch}
        r.setdefault("state", {}).update(repo_settings_body(merge, default_branch, description, features, is_template))

    def enable_security(self, *, repo, **features) -> None:
        r = self._repo(repo, "enable_security")
        avail = (r.get("state") or {}).get("available") or {}
        if features.get("code_scanning_default_setup") and avail.get("code_scanning") is False:
            raise GitHubError(f"GitHub PATCH {repo}/code-scanning/default-setup: 403 Advanced Security must be enabled", 403)
        merged = {**(r.get("security") or {}), **{k: v for k, v in features.items() if v}}
        r["security"] = merged
        r.setdefault("state", {})["security"] = merged

    def grant_team(self, *, org, team, repo, permission) -> None:
        self._repo(repo, "grant_team").setdefault("teams", {})[team] = permission

    def set_topics(self, *, repo, topics) -> None:
        self._repo(repo, "set_topics")["topics"] = list(topics)

    def set_properties(self, *, repo, properties) -> None:
        self._repo(repo, "set_properties")["properties"] = dict(properties)

    def upsert_ruleset(self, *, repo, branch, spec, target: str = "branch") -> str:
        r = self._repo(repo, "upsert_ruleset")
        body = ruleset_body(spec, target=target)
        if target == "branch":
            r["ruleset"] = {"branch": branch, **spec}
        r.setdefault("state", {}).setdefault("rulesets", {})[body["name"]] = body
        return body["name"]

    def repo_state(self, *, repo) -> dict | None:
        """Tests describe a repo's current state in repos[repo]["state"] (see repo_standards)."""
        r = self.repos.get(repo)
        if r is None:
            return None
        st = r.setdefault("state", {})
        sec = dict(st.get("security") or r.get("security") or {})
        return {
            "settings": {k: v for k, v in st.items() if k not in (
                "rulesets", "security", "available", "topics", "properties", "teams", "files", "visibility",
                "archived", "description")},
            "visibility": st.get("visibility", "private"), "archived": st.get("archived", False),
            # GitHub turns the dependency graph on for new repos unless the org says otherwise.
            "security": {k: bool(sec.get(k, k == "dependency_graph")) for k in (
                "dependency_graph", "dependabot_alerts", "dependabot_security_updates", "secret_scanning",
                "push_protection", "code_scanning_default_setup", "private_vulnerability_reporting")},
            "available": {"rulesets": True, **dict(st.get("available") or {"code_scanning": True, "secret_scanning": True})},
            "rulesets": dict(st.get("rulesets") or {}),
            "topics": list(r.get("topics") or st.get("topics") or []),
            "properties": dict(r.get("properties") or st.get("properties") or {}),
            "teams": dict(r.get("teams") or st.get("teams") or {}),
            "files": {".github/CODEOWNERS": self.files.get((repo, "main", ".github/CODEOWNERS"))
                      or self.files.get((repo, "main", "CODEOWNERS")),
                      **{p: self.files.get((repo, "main", p)) for p in (".github/dependabot.yml", "SECURITY.md")}},
            "org": repo.split("/", 1)[0],
        }

    def ensure_org_properties(self, *, org, names) -> None:
        self.org_properties = {**getattr(self, "org_properties", {}), org: sorted(set(names))}


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
            "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "vrixel-sdlc",
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
            raise GitHubError(f"GitHub {method} {repo}{path}: {exc.code} {detail}".strip(), exc.code) from exc

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

    def merge(self, *, repo, number, actor, sha="") -> dict:
        pr = self._call("GET", repo, f"/pulls/{number}")
        if not pr.get("merged"):
            # Branch protection still applies: required checks must pass, or GitHub refuses. With the
            # approved sha, GitHub also refuses if anything was pushed after the gates approved.
            self._call("PUT", repo, f"/pulls/{number}/merge", {
                "merge_method": self.merge_method, "sha": sha or pr["head"]["sha"],
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
            if st["context"].startswith(("openchoreo/", "vrixel/")):
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

    def workflow_runs(self, *, repo, branch=None, head_sha=None, event=None) -> list[dict]:
        """Actions runs (first 100) with their jobs, for the cost ledger."""
        from urllib.parse import urlencode

        q = urlencode({k: v for k, v in (("branch", branch), ("head_sha", head_sha), ("event", event),
                                         ("per_page", 100)) if v})
        runs = (self._call("GET", repo, f"/actions/runs?{q}", ok404=True) or {}).get("workflow_runs") or []
        out = []
        for r in runs:
            jobs = (self._call("GET", repo, f"/actions/runs/{r['id']}/jobs?per_page=100", ok404=True) or {}).get("jobs") or []
            out.append({"id": r["id"], "name": r.get("name", ""), "display_title": r.get("display_title", ""),
                        "event": r.get("event", ""), "status": r.get("status", ""), "created_at": r.get("created_at"),
                        "updated_at": r.get("updated_at"),
                        "jobs": [{"started_at": j.get("started_at"), "completed_at": j.get("completed_at"),
                                  "labels": j.get("labels") or []} for j in jobs]})
        return out

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

    # -- provisioning
    def _abs(self, method: str, org: str, path: str, body: dict | None = None, *, ok404: bool = False):
        data = json.dumps(body).encode() if body is not None else None
        req = Request(f"{self.api}{path}", data=data, method=method, headers={
            "Authorization": f"Bearer {self.tokens.token(org)}", "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "vrixel-sdlc",
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
            raise GitHubError(f"GitHub {method} {path}: {exc.code} {detail}".strip(), exc.code) from exc

    def get_file(self, *, repo, path, ref):
        query = f"?ref={quote(ref, safe='')}" if ref else ""
        res = self._call("GET", repo, f"/contents/{quote(path)}{query}", ok404=True)
        if not res or not isinstance(res, dict) or "content" not in res:
            return None
        return base64.b64decode(res["content"]).decode("utf-8")

    def create_repo(self, *, repo, template, private, description) -> str:
        org, name = repo.split("/", 1)
        if self._abs("GET", org, f"/repos/{repo}", ok404=True):
            return "exists"
        if template:
            self._abs("POST", org, f"/repos/{template}/generate", {
                "owner": org, "name": name, "description": description, "private": private,
                "include_all_branches": False})
        else:
            self._abs("POST", org, f"/orgs/{quote(org)}/repos", {
                "name": name, "description": description, "private": private, "auto_init": True})
        return "created"

    def update_repo(self, *, repo, merge, default_branch, description=None, features=None, is_template=None) -> None:
        self._call("PATCH", repo, "", repo_settings_body(merge, default_branch, description, features, is_template))

    def enable_security(self, *, repo, dependency_graph=True, dependabot_alerts=True,
                        dependabot_security_updates=True, secret_scanning=True, push_protection=True,
                        code_scanning_default_setup=True, **extra) -> None:
        """Turn on the features asked for. Features passed False are left as they are (a feature
        the plan lacks, e.g. without Advanced Security, is not switched off by the reconciler)."""
        if dependabot_alerts or dependency_graph:
            self._call("PUT", repo, "/vulnerability-alerts")  # also turns on the dependency graph
        if dependabot_security_updates:
            self._call("PUT", repo, "/automated-security-fixes")
        if extra.get("private_vulnerability_reporting"):
            self._call("PUT", repo, "/private-vulnerability-reporting")
        sa = {k: {"status": "enabled"} for k, on in (
            ("secret_scanning", secret_scanning), ("secret_scanning_push_protection", push_protection)) if on}
        if sa:
            self._call("PATCH", repo, "", {"security_and_analysis": sa})
        if code_scanning_default_setup:
            self._call("PATCH", repo, "/code-scanning/default-setup", {"state": "configured"})

    def grant_team(self, *, org, team, repo, permission) -> None:
        self._abs("PUT", org, f"/orgs/{quote(org)}/teams/{quote(team)}/repos/{repo}", {"permission": permission})

    def set_topics(self, *, repo, topics) -> None:
        self._call("PUT", repo, "/topics", {"names": topics})

    def set_properties(self, *, repo, properties) -> None:
        if properties:
            self._call("PATCH", repo, "/properties/values", {
                "properties": [{"property_name": k, "value": v} for k, v in properties.items()]})

    def upsert_ruleset(self, *, repo, branch, spec, target: str = "branch") -> str:
        body = ruleset_body(spec, target=target)
        existing = next((r for r in self._call("GET", repo, "/rulesets") or [] if r.get("name") == body["name"]), None)
        if existing:
            self._call("PUT", repo, f"/rulesets/{existing['id']}", body)
        else:
            self._call("POST", repo, "/rulesets", body)
        return body["name"]

    # -- reading what a repo has now, for the standards reconciler (vrixel_sdlc.repo_standards)
    def repo_state(self, *, repo) -> dict | None:
        """The repo's settings, security features, rulesets, topics, properties, team access and
        standard files, normalized for comparison with the standards. None if it doesn't exist."""
        info = self._call("GET", repo, "", ok404=True)
        if info is None:
            return None
        org = repo.split("/", 1)[0]
        sa = info.get("security_and_analysis") or {}

        def status(*keys):
            for k in keys:
                if (sa.get(k) or {}).get("status"):
                    return sa[k]["status"] == "enabled"
            return None

        public = info.get("visibility") == "public" or info.get("private") is False
        ghas_code = public or bool(status("code_security", "advanced_security"))
        ghas_secrets = public or bool(status("secret_protection", "advanced_security"))
        alerts = self._call("GET", repo, "/vulnerability-alerts", ok404=True) is not None
        # The dependency graph has no setting in the API; its SBOM exists only while it's on.
        graph = self._call("GET", repo, "/dependency-graph/sbom", ok404=True) is not None
        # Read from its own endpoint: security_and_analysis is empty for private repos on GitHub Free.
        fixes = bool((self._call("GET", repo, "/automated-security-fixes", ok404=True) or {}).get("enabled"))
        try:
            cs = (self._call("GET", repo, "/code-scanning/default-setup", ok404=True) or {}).get("state") == "configured"
        except GitHubError as exc:
            if exc.status not in (403, 422):
                raise
            cs, ghas_code = False, False
        rulesets, has_rulesets = {}, True
        try:
            listed = self._call("GET", repo, "/rulesets") or []
        except GitHubError as exc:
            # Private repos on GitHub Free have no rulesets (or branch protection): "Upgrade to
            # GitHub Pro or make this repository public".
            if exc.status != 403:
                raise
            listed, has_rulesets = [], False
        for r in listed:
            if r.get("source_type", "Repository") == "Repository":
                rulesets[r["name"]] = self._call("GET", repo, f"/rulesets/{r['id']}")
        teams = {t["slug"]: t.get("permission", "pull") for t in self._call("GET", repo, "/teams") or []}
        props = {p["property_name"]: p.get("value") for p in
                 (self._call("GET", repo, "/properties/values", ok404=True) or [])}
        files = {}
        for path in (".github/dependabot.yml", "SECURITY.md"):
            files[path] = self.get_file(repo=repo, path=path, ref="")
        # GitHub reads CODEOWNERS from .github/, the root or docs/.
        files[".github/CODEOWNERS"] = next((c for c in (self.get_file(repo=repo, path=p, ref="") for p in (
            ".github/CODEOWNERS", "CODEOWNERS", "docs/CODEOWNERS")) if c is not None), None)
        try:
            pvr = bool((self._call("GET", repo, "/private-vulnerability-reporting", ok404=True) or {}).get("enabled"))
        except GitHubError:
            pvr = False
        return {
            "settings": {k: info.get(k) for k in (
                "allow_squash_merge", "allow_merge_commit", "allow_rebase_merge", "delete_branch_on_merge",
                "allow_auto_merge", "allow_update_branch", "squash_merge_commit_title",
                "squash_merge_commit_message", "default_branch", "has_wiki", "has_projects", "has_discussions",
                "is_template")},
            "visibility": info.get("visibility") or ("private" if info.get("private") else "public"),
            "archived": bool(info.get("archived")),
            "security": {"dependabot_alerts": alerts, "dependency_graph": graph,
                         "dependabot_security_updates": fixes,
                         "private_vulnerability_reporting": pvr,
                         "secret_scanning": bool(status("secret_scanning")),
                         "push_protection": bool(status("secret_scanning_push_protection")),
                         "code_scanning_default_setup": cs},
            "available": {"code_scanning": ghas_code, "secret_scanning": ghas_secrets, "rulesets": has_rulesets},
            "rulesets": rulesets, "topics": list((self._call("GET", repo, "/topics") or {}).get("names") or []),
            "properties": props, "teams": teams, "files": files, "org": org,
        }

    def ensure_org_properties(self, *, org, names) -> None:
        """Define the organization's custom properties the standards set (string values)."""
        have = {p["property_name"] for p in self._abs("GET", org, f"/orgs/{quote(org)}/properties/schema") or []}
        missing = [n for n in names if n not in have]
        if missing:
            self._abs("PATCH", org, f"/orgs/{quote(org)}/properties/schema", {"properties": [
                {"property_name": n, "value_type": "string",
                 "description": "Set by the Vrixel SDLC repo standards"} for n in missing]})

    def dispatch(self, *, repo, event_type, payload) -> None:
        """repository_dispatch: starts the repo's post-deploy workflow (templates ship one)."""
        self._call("POST", repo, "/dispatches", {"event_type": event_type, "client_payload": payload})

"""The tool proposes its own releases, and its shared services' (platform-foundation's upgrade path).

A deployment pins each image by digest in one file (platform-foundation:
gitops/clusters/tools/releases.yaml). When a source repository's publish workflow has pushed and
signed a new image from main, it posts a commit status, `valxel/image`, whose description is the
digest. That status reaches the tool's GitHub webhook, and the tool:

1. reads the pins on the deployment repository's default branch,
2. moves the pin whose image is that repository's, at the tag that tracks main, to the new digest,
   keeping any other update still waiting in the open release PR,
3. resets the release branch to the default branch and commits the file there, and
4. opens the release PR, or updates the open one.

That is all it does. It proposes; it doesn't approve, merge or deploy. The deployment repository's
own checks verify the digest's signature and rehearse the release against a copy of the tool's
database, a person merges, and Flux rolls it out. If the tool can't propose (it's down, or a release
broke this), anyone can edit the pins by hand: nothing downstream depends on the tool.

Off unless VALXEL_RELEASE_REPO names the deployment repository.
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .cluster import Cluster
    from .integrations.github import GitHub

CONTEXT = "valxel/image"
DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
# `  sdlc_tool_image: ghcr.io/valxel-platform/agentic-sdlc:main@sha256:...  # anything`
PIN = re.compile(r"^(?P<lead>  (?P<key>[a-z_]+): )ghcr\.io/(?P<repo>[a-z0-9._-]+/[a-z0-9._-]+):(?P<tag>[\w.-]+)"
                 r"@(?P<digest>sha256:[0-9a-f]{64})(?P<rest>.*)$")
LOCK = "release-proposal"


def pins(text: str) -> dict[str, dict]:
    """The pinned images in a releases file: key -> {repo, tag, digest}."""
    out = {}
    for line in (text or "").splitlines():
        m = PIN.match(line)
        if m:
            out[m["key"]] = {"repo": m["repo"], "tag": m["tag"], "digest": m["digest"]}
    return out


def pin(text: str, repo: str, tag: str, digest: str) -> tuple[str, list[str]]:
    """The file with each pin of repo at tag moved to digest, and the keys that changed."""
    changed, lines = [], []
    for line in text.splitlines(keepends=True):
        m = PIN.match(line.rstrip("\n"))
        if m and m["repo"] == repo.lower() and m["tag"] == tag and m["digest"] != digest:
            changed.append(m["key"])
            end = "\n" if line.endswith("\n") else ""
            lines.append(f"{m['lead']}ghcr.io/{m['repo']}:{m['tag']}@{digest}{m['rest']}{end}")
        else:
            lines.append(line)
    return "".join(lines), changed


def carry(main: str, proposed: str | None) -> str:
    """main, with the digests the open release PR already proposes for other images."""
    if not proposed:
        return main
    waiting = pins(proposed)
    out = main
    for key, was in pins(main).items():
        now = waiting.get(key)
        if now and now["repo"] == was["repo"] and now["tag"] == was["tag"] and now["digest"] != was["digest"]:
            out, _ = pin(out, was["repo"], was["tag"], now["digest"])
    return out


def _short(digest: str) -> str:
    return digest.split(":", 1)[-1][:12]


@dataclass
class Proposer:
    github: GitHub
    cluster: Cluster
    repo: str  # the deployment repository, owner/name
    path: str = "gitops/clusters/tools/releases.yaml"
    branch: str = "release/valxel-images"
    wait_s: float = 120.0

    @staticmethod
    def from_env(github, cluster, env=None) -> Proposer | None:
        env = env if env is not None else os.environ
        repo = env.get("VALXEL_RELEASE_REPO", "").strip()
        if not repo:
            return None
        return Proposer(github, cluster, repo,
                        path=env.get("VALXEL_RELEASE_FILE", "") or "gitops/clusters/tools/releases.yaml",
                        branch=env.get("VALXEL_RELEASE_BRANCH", "") or "release/valxel-images")

    def on_status(self, event: dict) -> dict | None:
        """A `valxel/image` status from a publish workflow; None for any other status."""
        if event.get("context") != CONTEXT:
            return None
        if event.get("state") != "success":
            return {"ignored": "not a published image"}
        digest = str(event.get("description") or "").strip()
        source = str((event.get("repository") or {}).get("full_name") or "").lower()
        branches = {b.get("name") for b in event.get("branches") or [] if isinstance(b, dict)}
        if not DIGEST.match(digest):
            return {"ignored": "the description isn't a digest"}
        if "main" not in branches:
            return {"ignored": "not built from main"}
        return self.propose(source, "main", digest, commit=str(event.get("sha") or ""),
                            run=str(event.get("target_url") or ""))

    def _lease(self):
        deadline = time.monotonic() + self.wait_s
        while True:
            lease = self.cluster.lock(LOCK)
            if lease is not None or time.monotonic() > deadline:
                return lease
            time.sleep(2)

    def propose(self, source: str, tag: str, digest: str, *, commit: str = "", run: str = "") -> dict:
        lease = self._lease()  # one proposal at a time, across replicas: each builds on the last
        if lease is None:
            raise TimeoutError(f"another release proposal held {LOCK} for {self.wait_s:.0f}s")
        try:
            main = self.github.get_file(repo=self.repo, path=self.path, ref="")
            if main is None:
                return {"ignored": f"{self.repo} has no {self.path}"}
            if not any(p["repo"] == source and p["tag"] == tag for p in pins(main).values()):
                return {"ignored": f"{source}:{tag} isn't pinned in {self.path}"}
            open_pr = next((p for p in self.github.open_pull_requests(repo=self.repo)
                            if p.get("branch") == self.branch), None)
            # An open release PR's other updates carry over; a merged or closed one's don't.
            waiting = self.github.get_file(repo=self.repo, path=self.path, ref=self.branch) if open_pr else None
            proposed, changed = pin(carry(main, waiting), source, tag, digest)
            if proposed == main:
                return {"ignored": f"{source}:{tag} is already pinned at {_short(digest)}"}
            if open_pr is not None and proposed == waiting:
                return {"unchanged": open_pr.get("number")}
            self.github.reset_branch(repo=self.repo, branch=self.branch, sha=self.github.head_sha(repo=self.repo))
            what = f"{source}@{commit[:7]}" if commit else source
            self.github.put_file(repo=self.repo, branch=self.branch, path=self.path, content=proposed,
                                 message=f"Release {what}: {', '.join(changed) or 'pins'} at {_short(digest)}")
            pr = self.github.open_pr(repo=self.repo, branch=self.branch, title=self._title(main, proposed),
                                     body=self._body(main, proposed, source, commit, run), files=[self.path],
                                     labels=[], reopen_merged=False)
            return {"pr": pr.get("url"), "number": pr.get("number"), "changed": changed}
        finally:
            lease.release()

    @staticmethod
    def _moves(main: str, proposed: str) -> list[tuple[str, dict, dict]]:
        before, after = pins(main), pins(proposed)
        return [(k, before[k], after[k]) for k in after if k in before and after[k] != before[k]]  # file order

    def _title(self, main: str, proposed: str) -> str:
        names = [p["repo"].split("/", 1)[1] for _, _, p in self._moves(main, proposed)]
        return f"Release {', '.join(names)}"

    def _body(self, main: str, proposed: str, source: str, commit: str, run: str) -> str:
        rows = [f"| `{k}` | `{b['repo']}` | `{_short(b['digest'])}` | `{_short(a['digest'])}` |"
                for k, b, a in self._moves(main, proposed)]
        latest = f"https://github.com/{source}/commit/{commit}" if commit else ""
        return "\n".join([
            "The images below were built from `main`, signed, and published. This moves their pins.",
            "",
            "| Pin | Repository | Running | Proposed |",
            "| --- | --- | --- | --- |",
            *rows,
            "",
            f"Latest: [{source}@{commit[:7]}]({latest})" + (f", [publish run]({run})" if run else "") if commit else "",
            "",
            "Before merging: the signatures check verifies each digest was signed by its repository's "
            "publish workflow, and the rehearsal check runs the running release, this one, then the "
            "running one again against a copy of the tool's database. Merging deploys it; reverting "
            "this PR rolls it back.",
            "",
            "_Opened by the Valxel SDLC tool, which only proposes its releases: it doesn't approve or "
            "merge them._",
        ])

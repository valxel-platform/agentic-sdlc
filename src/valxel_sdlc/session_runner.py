"""Agent sessions in sandboxes of their own (VALXEL_SESSION_EXECUTOR=kubernetes).

A coder session runs shell commands and installs packages in a checkout of a product's repository.
Run as a child of the tool's process, it shared the tool's container, user and file system with the
GitHub App's key, the database URL and every other workspace's work. Here each session runs in a
fresh sandbox instead, the pattern template-code-sandbox uses for product agents:

    tool ──SandboxClaim (warm pool agent-session)──▶ a ready sandbox
         ──POST /run: the session spec──▶ runner (gVisor, egress to registries, GitHub and the model)
         ◀── the outcome ── the sandbox is deleted

**What crosses** (`spec_for`): the prompt and the session's options, the feature's documents the
session reads, the repository and branch with a token for that one repository (contents:write, an
hour at most), the model credentials and telemetry settings the session already had, and, to resume,
the earlier session's transcript. Nothing else of the tool's: no App key, database, webhook secret
or other workspace's files reach the sandbox.

**What comes back** (`run_spec`'s outcome): the reply, token usage, the session id, the skills it
loaded and, when asked, the transcript to resume from later. The coder pushes its branch from the
sandbox, as it did from the tool's checkout. The tool writes the ledger, as before.

The platform owns the sandbox's isolation (platform-foundation, gitops/apps/agent-sessions): the
`agent-session` SandboxTemplate runs the tool's own image as `valxel session-runner`, under gVisor,
with no service account token, a read-only root, CPU, memory and a deadline, and network only to the
package registries, GitHub, the model provider and the telemetry collector. A sandbox runs one
session; nothing from one session is seen by the next.

`local` runs the same steps in the tool's process: for development and tests. Strict mode refuses it
in live mode.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Protocol

log = logging.getLogger(__name__)

VERSION = 1
# Agent Sandbox 1.0 serves its extensions at v1beta1 only.
GROUP, API_VERSION, PLURAL = "extensions.agents.x-k8s.io", "v1beta1", "sandboxclaims"
# Set by the runner itself, never taken from the spec.
_OWN = ("cwd", "settings", "env", "plugins", "resume")


class Unavailable(RuntimeError):
    """No sandbox in time, or it couldn't be reached: the session didn't run."""


# --------------------------------------------------------------------------- the tool's side

def files_of(home: str, folder: str = "feature") -> dict[str, str]:
    """The feature's documents the session reads (graph.phase.feature_folder), relative to its home."""
    base = Path(home or "") / folder
    if not home or not base.is_dir():
        return {}
    return {f"{folder}/{p.name}": p.read_text(encoding="utf-8") for p in sorted(base.glob("*.md"))}


def spec_for(task, options: dict, *, transcript: str = "", fallback_prompt: str = "", fallback_mode: str = "",
             plugins_on: bool = True) -> dict:
    """What a sandbox needs to run this session, and nothing else of the tool's."""
    box = task.sandbox or {}
    env = {k: v for k, v in (options.get("env") or {}).items() if v and k not in ("HOME", "TMPDIR")}
    return {
        "version": VERSION, "runtime": "claude_code", "id": uuid.uuid4().hex[:16],
        "prompt": task.layers.tail,
        "options": {k: v for k, v in options.items() if k not in _OWN},
        "env": env, "plugins": bool(plugins_on),
        "checkout": box.get("checkout") or None,
        "read_checkouts": list(box.get("read_checkouts") or []),
        "files": files_of(box.get("home", "")),
        "resume": {"session_id": task.resume, "transcript": transcript} if task.resume and transcript else None,
        "fallback": {"prompt": fallback_prompt or task.layers.tail, "session_mode": fallback_mode or "fresh"},
        "keep_transcript": bool(task.keep_transcript),
        "timeout_s": int(os.environ.get("VALXEL_SESSION_TIMEOUT_S", "3600")),
    }


# --------------------------------------------------------------------------- the sandbox's side

_REPO = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9_][A-Za-z0-9_.-]*")
_SHA = re.compile(r"[0-9a-f]{40}")
_SESSION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,63}")


def _repo(name: str) -> str:
    """`owner/name` as GitHub spells them, nothing that could walk out of the work area."""
    if not isinstance(name, str) or not _REPO.fullmatch(name) or ".." in name:
        raise ValueError(f"not a repository name: {name!r}")
    return name


def _inside(base: Path, rel: str) -> Path:
    """`rel` under `base`, refusing anything that resolves outside it."""
    root = os.path.realpath(base)
    full = os.path.normpath(os.path.join(root, rel))
    if not full.startswith(root + os.sep):
        raise ValueError(f"a path outside {base}: {rel!r}")
    return Path(full)


def run_spec(spec: dict, root: Path | None = None, *, query=None) -> dict:
    """Run one session from its spec, in `root` (the sandbox's work area): the outcome, with the
    transcript when asked. `query` stands in for the SDK in tests."""
    from . import sessions
    from .runtime.claude_code import plugin_dirs_from
    from .sandbox import NO_SCRIPTS, claude_settings, clone

    if spec.get("version") != VERSION or spec.get("runtime") != "claude_code":
        raise ValueError("unknown session spec")
    root = Path(root or tempfile.mkdtemp(prefix="valxel-run-"))
    home = root / "home"
    (home / "tmp").mkdir(parents=True, exist_ok=True)
    for rel, text in (spec.get("files") or {}).items():
        dest = _inside(home, rel)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(text, encoding="utf-8")
    co = spec.get("checkout") or {}
    if co:
        cwd = clone(_repo(co["repo"]), co["branch"], co["token"], root)
    else:
        cwd = str(root / "work")
        Path(cwd).mkdir(parents=True, exist_ok=True)
        for rc in spec.get("read_checkouts") or []:  # the code an architect reads: as it was, read-only
            read_clone(rc["repo"], rc["sha"], rc["token"], root)
    options = {**(spec.get("options") or {}), "cwd": cwd, "settings": json.dumps(claude_settings(str(home))),
               "env": {**(spec.get("env") or {}), "HOME": str(home), "TMPDIR": str(home / "tmp"), **NO_SCRIPTS},
               "plugins": [{"type": "local", "path": p} for p in
                           (plugin_dirs_from(os.environ.get("VALXEL_SKILLS_DIR")) if spec.get("plugins") else [])]}
    ask = query or _sdk_query
    resumed, mode = False, spec["fallback"]["session_mode"]
    out = None
    r = spec.get("resume") or {}
    if r:
        if not _SESSION_ID.fullmatch(str(r.get("session_id", ""))):
            raise ValueError("not a session id")
        dest = sessions.claude_project_dir(str(home), cwd) / f"{r['session_id']}.jsonl"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(r["transcript"], encoding="utf-8")
        try:
            out = ask(spec["prompt"], {**options, "resume": r["session_id"]})
            resumed, mode = True, "resumed"
        except Exception as exc:  # gone, or Claude Code can't use it: start fresh, from the handoff
            log.warning("couldn't resume session %s (%s); starting fresh", r["session_id"], exc)
    if out is None:
        out = ask(spec["fallback"]["prompt"] if r else spec["prompt"], options)
    transcript = ""
    if spec.get("keep_transcript") and out.get("session_id"):
        found = sessions.find_transcript(str(home), out["session_id"])
        transcript = found.read_text(encoding="utf-8") if found else ""
    return {**out, "resumed": resumed, "session_mode": mode, "transcript": transcript}


def read_clone(repo: str, sha: str, token: str, root: Path) -> str:
    """A read-only checkout of `repo` at `sha` under root/work, its token not kept in the clone."""
    import stat
    import subprocess

    from .sandbox import _askpass

    if not _SHA.fullmatch(str(sha)):
        raise ValueError(f"not a commit sha: {sha!r}")
    dest = _inside(root / "work", _repo(repo).split("/", 1)[1])
    dest.mkdir(parents=True, exist_ok=True)
    folder = root / ".git-read"
    folder.mkdir(exist_ok=True)
    env = {"PATH": os.environ.get("PATH", ""), "GIT_ASKPASS": str(_askpass(folder, token)),
           "GIT_TERMINAL_PROMPT": "0", "HOME": str(root / "home")}
    for args in (["init", "--quiet"], ["remote", "add", "origin", f"https://github.com/{repo}.git"],
                 ["fetch", "--quiet", "--depth", "1", "origin", sha], ["checkout", "--quiet", sha]):
        subprocess.run(["git", "-C", str(dest), *args], check=True, env=env, capture_output=True, timeout=600)
    shutil.rmtree(folder, ignore_errors=True)  # the token goes with it
    for base, _dirs, names in os.walk(dest):
        for n in names:
            p = Path(base, n)
            try:
                p.chmod(p.stat().st_mode & ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))
            except OSError:
                pass
    return str(dest)


def _sdk_query(prompt: str, options: dict) -> dict:
    from claude_agent_sdk import ClaudeAgentOptions

    from .runtime.claude_code import run_query

    return asyncio.run(run_query(prompt, ClaudeAgentOptions(**options)))


def serve(port: int = 8081) -> None:
    """The runner in a session's sandbox: one session, then the sandbox is deleted."""
    import uvicorn

    uvicorn.run(make_app(), host="0.0.0.0", port=port, log_level="info")  # noqa: S104 - the pod's own port


def make_app(work: Path = Path("/work"), query=None):
    from fastapi import FastAPI, HTTPException

    app = FastAPI(title="Valxel session runner")
    state = {"used": False}

    @app.get("/healthz")
    def healthz() -> dict:
        return {"ok": True, "used": state["used"]}

    @app.post("/run")
    def run(spec: dict) -> dict:
        if state["used"]:  # one session per sandbox
            raise HTTPException(409, "this sandbox has run its session")
        state["used"] = True
        try:
            return {"ok": True, **run_spec(spec, work, query=query)}
        except Exception as exc:  # the tool reports it as the session's error; details stay in this log
            log.exception("session failed")
            return _failed(exc)

    return app


def _failed(exc: Exception) -> dict:
    """What the tool hears about a failed session: its kind, and whether trying again could work."""
    from .gateway import is_transient

    return {"ok": False, "error": type(exc).__name__, "transient": is_transient(str(exc))}


# --------------------------------------------------------------------------- executors

class Executor(Protocol):
    def run(self, spec: dict) -> dict: ...


class LocalExecutor:
    """The session in this process, in a folder of its own: development and tests only."""

    def __init__(self, query=None) -> None:
        self.query = query

    def run(self, spec: dict) -> dict:
        root = Path(tempfile.mkdtemp(prefix="valxel-run-"))
        try:
            return {"ok": True, **run_spec(spec, root, query=self.query)}
        finally:
            shutil.rmtree(root, ignore_errors=True)


class KubernetesExecutor:
    """Each session claims a fresh sandbox from the warm pool, runs there, and the claim is deleted.
    The claim also deletes itself at its shutdown time, should the tool not get to it."""

    def __init__(self, *, namespace: str, pool: str, port: int = 8081, ready_s: float = 120.0, api: Any = None,
                 http: Any = None, sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic, now: Callable[[], float] = time.time) -> None:
        self.namespace, self.pool, self.port, self.ready_s = namespace, pool, port, ready_s
        self.api = api or _custom_objects()
        self.http = http
        self.sleep, self.clock, self.now = sleep, clock, now

    def run(self, spec: dict) -> dict:
        import httpx
        from datetime import datetime, timezone

        name = f"session-{spec.get('id') or uuid.uuid4().hex[:16]}"
        ends = datetime.fromtimestamp(self.now() + spec.get("timeout_s", 3600) + 900, timezone.utc)
        body = {"apiVersion": f"{GROUP}/{API_VERSION}", "kind": "SandboxClaim",
                "metadata": {"name": name, "labels": {"valxel.io/agent-session": "true"}},
                "spec": {"warmPoolRef": {"name": self.pool},
                         "lifecycle": {"shutdownPolicy": "Delete",
                                       "shutdownTime": ends.strftime("%Y-%m-%dT%H:%M:%SZ")}}}
        self.api.create_namespaced_custom_object(GROUP, API_VERSION, self.namespace, PLURAL, body)
        http = self.http or httpx.Client(follow_redirects=False, trust_env=False)
        try:
            host = self._ready(name)
            resp = self._post(http, f"http://{host}:{self.port}/run", spec)
            if resp.status_code != 200:
                raise Unavailable(f"the session's sandbox answered {resp.status_code}")
            return resp.json()
        except httpx.HTTPError as exc:
            raise Unavailable(f"the session's sandbox couldn't be reached ({type(exc).__name__})") from exc
        finally:
            try:
                self.api.delete_namespaced_custom_object(GROUP, API_VERSION, self.namespace, PLURAL, name)
            except Exception:  # noqa: BLE001 - the claim's TTL removes it anyway
                log.warning("session sandbox claim %s not deleted", name)

    def _post(self, http, url: str, spec: dict):
        """A new sandbox's name may not resolve yet, nor its runner listen: retry connecting for a
        little while. Once connected, the session runs once: nothing is sent twice."""
        import httpx

        deadline = self.clock() + min(self.ready_s, 30.0)
        while True:
            try:
                return http.post(url, json=spec, timeout=spec.get("timeout_s", 3600) + 60)
            except httpx.ConnectError:
                if self.clock() >= deadline:
                    raise
                self.sleep(1.0)

    def _ready(self, name: str) -> str:
        deadline = self.clock() + self.ready_s
        while self.clock() < deadline:
            claim = self.api.get_namespaced_custom_object(GROUP, API_VERSION, self.namespace, PLURAL, name)
            status = claim.get("status") or {}
            ready = any(c.get("type") == "Ready" and c.get("status") == "True" for c in status.get("conditions") or [])
            sb = status.get("sandbox") or {}
            # The pod's own address: a new sandbox's Service name may not resolve yet.
            host = next(iter(sb.get("podIPs") or []), "") or sb.get("serviceFQDN") or ""
            if ready and host:
                return f"[{host}]" if ":" in host else host
            self.sleep(0.5)
        raise Unavailable("no session sandbox was ready in time")


class InClusterApi:
    """The three calls the executor makes, against the cluster's API with this pod's service
    account (no kubernetes client library: httpx is already in the image)."""

    SA = Path("/var/run/secrets/kubernetes.io/serviceaccount")

    def __init__(self, http: Any = None) -> None:
        import httpx

        host, port = os.environ["KUBERNETES_SERVICE_HOST"], os.environ.get("KUBERNETES_SERVICE_PORT", "443")
        self.base = f"https://{host}:{port}"
        self.http = http or httpx.Client(verify=str(self.SA / "ca.crt"), timeout=30, trust_env=False)

    def _url(self, group, version, ns, plural, name="") -> str:
        return f"{self.base}/apis/{group}/{version}/namespaces/{ns}/{plural}" + (f"/{name}" if name else "")

    def _call(self, method: str, url: str, body: dict | None = None) -> dict:
        token = (self.SA / "token").read_text().strip()  # rotated by the kubelet: read each time
        resp = self.http.request(method, url, json=body, headers={"Authorization": f"Bearer {token}"})
        if resp.status_code >= 400:
            raise Unavailable(f"the cluster refused {method} {url.rsplit('/', 2)[-2:]}: {resp.status_code}")
        return resp.json() if resp.content else {}

    def create_namespaced_custom_object(self, group, version, ns, plural, body):
        return self._call("POST", self._url(group, version, ns, plural), body)

    def get_namespaced_custom_object(self, group, version, ns, plural, name):
        return self._call("GET", self._url(group, version, ns, plural, name))

    def delete_namespaced_custom_object(self, group, version, ns, plural, name):
        return self._call("DELETE", self._url(group, version, ns, plural, name))


def _custom_objects() -> Any:
    return InClusterApi()


_executor: Executor | None = None


def executor() -> Executor:
    """The executor VALXEL_SESSION_EXECUTOR names: kubernetes (sandboxes) or local (this process)."""
    global _executor
    if _executor is None:
        kind = os.environ.get("VALXEL_SESSION_EXECUTOR", "local").lower()
        if kind == "kubernetes":
            _executor = KubernetesExecutor(namespace=os.environ.get("VALXEL_SESSION_NAMESPACE", "valxel-sessions"),
                                           pool=os.environ.get("VALXEL_SESSION_POOL", "agent-session"),
                                           port=int(os.environ.get("VALXEL_SESSION_RUNNER_PORT", "8081")),
                                           ready_s=float(os.environ.get("VALXEL_SESSION_READY_S", "120")))
        elif kind == "local":
            _executor = LocalExecutor()
        else:
            raise RuntimeError(f"VALXEL_SESSION_EXECUTOR is kubernetes or local, not {kind!r}")
    return _executor

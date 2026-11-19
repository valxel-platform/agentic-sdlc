"""The laptop tier (ADR 0007): products built and run on this machine's Docker.

With `VALXEL_DELIVERY=local` the tool opens real pull requests on GitHub, but where the cluster
tier asks OpenChoreo to build and deploy, it builds each component's image here (`LocalBuilds`:
`docker build` of the repo at the commit) and runs every environment as a Compose project on the
same Docker, through the lite runtime (`ComposeRuntime`). Gates, agents, the change log and incidents
don't change: they talk to the same interface.

Local builds aren't scanned or signed, and their images live only in this Docker
(`valxel-local/<component>:<commit>`): fine on a developer's machine, refused by the VM and cluster
tiers (strict mode). Docker is the one `DOCKER_HOST` names (`VALXEL_LOCAL_DOCKER`), by default this
machine's.
"""

from __future__ import annotations

import hashlib
import itertools
import logging
import os
import re
import shutil
import subprocess
import tempfile
import threading
from datetime import UTC, datetime
from pathlib import Path

import yaml

from ..integrations.openchoreo import BUILD_STEPS
from .builds import WORKLOAD_FILE
from .docker import DockerCli
from .wiring import RuntimeRouter, lite_runtime

log = logging.getLogger(__name__)
_REPO = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_REF = re.compile(r"^[A-Za-z0-9_./-]{1,200}$")


def _now() -> str:
    return datetime.now(UTC).isoformat()


class LocalBuilds:
    """`docker build` of a component's repo at a ref, on this machine. `tokens` gives a GitHub token
    per org (the delivery App's, or VALXEL_GITHUB_TOKEN); `repo_of` maps a component to its repo."""

    def __init__(self, tokens, *, host: str = "", repo_of=None, runner=subprocess.run,
                 workdir: str | Path | None = None, background: bool = True) -> None:
        self.tokens = tokens
        self.host = host
        self.repo_of = repo_of or (lambda component: component)
        self.run = runner
        self.workdir = Path(workdir or tempfile.gettempdir()) / "valxel-builds"
        self.background = background
        self._runs: dict[str, dict] = {}
        self._seq = itertools.count(1)
        self._lock = threading.Lock()

    def start(self, *, component, ref, kind, repo_url, project=None, namespace=None) -> dict:
        repo = self.repo_of(component)
        if not _REPO.match(repo or ""):
            raise ValueError(f"{component}: no GitHub repo to build from ({repo!r})")
        if not _REF.match(ref or "main") or ".." in (ref or ""):
            raise ValueError(f"not a git ref: {ref!r}")
        with self._lock:
            run_id = f"local-build-{next(self._seq)}"
            run = {"run_id": run_id, "component": component, "repo": repo, "ref": ref or "main",
                   "status": "running", "message": "", "started_at": _now(), "finished_at": ""}
            self._runs[run_id] = run
        if self.background:
            threading.Thread(target=self._build, args=(run,), name=run_id, daemon=True).start()
        else:
            self._build(run)
        return {"run_id": run_id, "url": ""}

    def status(self, *, run_id, component, ref="", project=None, namespace=None) -> dict:
        with self._lock:
            run = dict(self._runs.get(run_id) or {})
        if not run:
            return {"status": "failed", "message": f"{run_id}: no such build (the tool restarted?)"}
        out = {"status": run["status"], "message": run["message"], "tasks": list(BUILD_STEPS),
               "started_at": run["started_at"], "finished_at": run["finished_at"]}
        if run["status"] == "succeeded":
            out |= {"image": run["image"], "workload": run["workload"]}
        return out

    # ------------------------------------------------------------------ the build itself

    def _git(self, args: list[str], cwd: Path, env: dict) -> str:
        res = self.run(["git", *args], cwd=str(cwd), env=env, capture_output=True, text=True, timeout=600)
        if res.returncode != 0:
            raise RuntimeError(f"git {args[0]}: {(res.stderr or res.stdout).strip()[-300:]}")
        return res.stdout or ""

    def _docker(self, args: list[str], cwd: Path) -> str:
        env = dict(os.environ) | ({"DOCKER_HOST": self.host} if self.host else {})
        res = self.run(["docker", *args], cwd=str(cwd), env=env, capture_output=True, text=True, timeout=1800)
        if res.returncode != 0:
            raise RuntimeError(f"docker {args[0]}: {(res.stderr or res.stdout).strip()[-500:]}")
        return res.stdout or ""

    def _build(self, run: dict) -> None:
        from ..sandbox import _askpass

        folder = Path(tempfile.mkdtemp(prefix="build-", dir=self._ensure_workdir()))
        try:
            org = run["repo"].split("/", 1)[0]
            secret = folder / ".git-auth"
            secret.mkdir()
            env = {"PATH": os.environ.get("PATH", ""), "HOME": str(folder), "GIT_TERMINAL_PROMPT": "0",
                   "GIT_ASKPASS": str(_askpass(secret, self.tokens.token(org)))}
            src = folder / "src"
            src.mkdir()
            for args in (["init", "--quiet"], ["remote", "add", "origin", f"https://github.com/{run['repo']}.git"],
                         ["fetch", "--quiet", "--depth", "1", "origin", run["ref"]], ["checkout", "--quiet", "FETCH_HEAD"]):
                self._git(args, src, env)
            shutil.rmtree(secret, ignore_errors=True)  # the token goes before anything is built
            sha = self._git(["rev-parse", "HEAD"], src, env).strip()
            wl = src / WORKLOAD_FILE
            if not wl.is_file():
                raise RuntimeError(f"no {WORKLOAD_FILE} at {run['ref']}")
            workload = yaml.safe_load(wl.read_text()) or {}
            if not isinstance(workload, dict):
                raise RuntimeError(f"{WORKLOAD_FILE} isn't a mapping")
            name = run["component"].rsplit("/", 1)[-1].lower()
            image = f"valxel-local/{name}:{sha[:12]}"
            self._docker(["build", "--pull", "-t", image, "--label", f"valxel.repo={run['repo']}",
                          "--label", f"valxel.commit={sha}", "."], src)
            with self._lock:
                run.update(status="succeeded", image=image, workload=workload, commit=sha, finished_at=_now())
        except Exception as exc:  # noqa: BLE001 - the build's failure is its status, read by the pipeline
            log.warning("local build %s failed: %s", run["run_id"], exc)
            with self._lock:
                run.update(status="failed", message=str(exc)[:500], finished_at=_now())
        finally:
            shutil.rmtree(folder, ignore_errors=True)

    def _ensure_workdir(self) -> Path:
        self.workdir.mkdir(parents=True, exist_ok=True)
        return self.workdir


LOCAL_PORTS = range(20000, 30000)


def local_port(project: str, service: str, endpoint: str) -> int:
    """A stable port on 127.0.0.1 for one external endpoint: the same after every deploy and
    restart, so a bookmark keeps working. Derived from the names; a clash moves to the next free one."""
    h = int(hashlib.sha256(f"{project}/{service}/{endpoint}".encode()).hexdigest(), 16)
    return LOCAL_PORTS.start + h % len(LOCAL_PORTS)


def publish_on_loopback(spec: dict, *, project: str) -> dict:
    """Publish each service's external endpoints (its `dev.valxel.port.<name>` labels) on
    127.0.0.1 only, never on the network. Returns {"<service>/<endpoint>": "http://127.0.0.1:<port>"}."""
    urls, used = {}, set()
    for name, svc in sorted((spec.get("services") or {}).items()):
        ports = []
        for label, value in sorted((svc.get("labels") or {}).items()):
            if not label.startswith("dev.valxel.port."):
                continue
            ep = label.removeprefix("dev.valxel.port.")
            port = local_port(project, name, ep)
            while port in used:
                port = LOCAL_PORTS.start + (port + 1 - LOCAL_PORTS.start) % len(LOCAL_PORTS)
            used.add(port)
            ports.append(f"127.0.0.1:{port}:{int(value)}")
            svc.setdefault("labels", {})[f"dev.valxel.url.{ep}"] = f"http://127.0.0.1:{port}"
            urls[f"{name}/{ep}"] = f"http://127.0.0.1:{port}"
        if ports:
            svc["ports"] = ports
            svc.pop("deploy", None)  # one replica: a published port can't be shared
    return urls


class LocalRouter(RuntimeRouter):
    """Every product deploys through a Compose runtime on this machine's Docker, whatever its
    runtime mode: on a laptop there is no OpenChoreo. One shared `LocalBuilds` builds them all."""

    def __init__(self, base, services, *, tokens, host: str = "", docker_for=None, builds=None) -> None:
        super().__init__(base, services, docker_for=docker_for or (lambda h: DockerCli(h)))
        self.host = host
        self._repos: dict[str, str] = {}  # component -> repo, from every product's runtime.components
        self.builds = builds or LocalBuilds(tokens, host=host)
        self.builds.repo_of = lambda c: self._repos.get(c, c)

    def runtime_for(self, project: str | None):
        if not project:
            return None
        pid, rt = self._product(str(project).lower())
        if not pid:
            return None
        with self._lock:
            known = self._runtimes.get(pid)
            if known is None:
                runtime = lite_runtime(self.services, pid, {"non_production": self.host}, runtime=rt,
                                       docker_for=self.docker_for)
                self._repos.update({comp: repo for repo, comp in (rt.get("components") or {}).items()})
                runtime.builds = self.builds
                runtime.publish_local = True
                known = ((), runtime)
                self._runtimes[pid] = known
            return known[1]

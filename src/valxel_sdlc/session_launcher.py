"""Agent sessions in containers of their own, on one machine (VALXEL_SESSION_EXECUTOR=docker, T-68).

The laptop and single-VM tiers (ADR 0007) have no cluster to claim sandboxes from. Each session gets a
container instead, the same image and runner the cluster's sandboxes use (`valxel session-runner`):

    worker ──POST /sessions (the session spec, bearer token)──▶ launcher ──Docker API──▶ a new container
                                                                         ──POST /run──▶ runner in it
           ◀──────────────── the outcome ────────────────────── launcher ── the container is removed

The **launcher** (`valxel session-launcher`) is the only process holding the Docker socket. The worker
reaches it with a token from a file; it starts only its configured image, never one the spec names,
and the spec never reaches Docker: it goes to the runner inside the container, as in the cluster.

Each container runs one session and is removed after it: gVisor (`runsc`) when Docker has it, every
capability dropped, no new privileges, a read-only root with tmpfs for its work, memory, CPU and
process limits, and a deadline. Its network (`VALXEL_SESSION_NETWORK`) is internal: it reaches the
LLM gateway and the egress proxy (valxel_sdlc.egress_proxy), which lets it out only to GitHub and the
package registries. Nothing else of the tool's (database, API, worker, keys) is on that network.
A container left behind by a crash is removed by the launcher's sweep.
"""

from __future__ import annotations

import hmac
import logging
import os
import threading
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

LABEL = "valxel.io/agent-session"
RUNNER_PORT = 8081


class Unavailable(RuntimeError):
    """No container in time, or it couldn't be reached: the session didn't run."""


# --------------------------------------------------------------------------- the Docker side

class Docker:
    """The few Docker Engine API calls the launcher makes, over the socket (httpx, no Docker SDK)."""

    def __init__(self, socket: str = "/var/run/docker.sock", http: Any = None) -> None:
        import httpx

        self.http = http or httpx.Client(transport=httpx.HTTPTransport(uds=socket), base_url="http://docker",
                                         timeout=60, trust_env=False)

    def _ok(self, resp, what: str) -> dict:
        if resp.status_code >= 400:
            raise Unavailable(f"Docker refused {what}: {resp.status_code} {resp.text[:200]}")
        return resp.json() if resp.content else {}

    def runtimes(self) -> set[str]:
        return set(self._ok(self.http.get("/info"), "info").get("Runtimes") or {})

    def create(self, name: str, body: dict) -> str:
        return self._ok(self.http.post("/containers/create", params={"name": name}, json=body), "create")["Id"]

    def start(self, cid: str) -> None:
        self._ok(self.http.post(f"/containers/{cid}/start"), "start")

    def inspect(self, cid: str) -> dict:
        return self._ok(self.http.get(f"/containers/{cid}/json"), "inspect")

    def remove(self, cid: str) -> None:
        resp = self.http.delete(f"/containers/{cid}", params={"force": "true", "v": "true"})
        if resp.status_code not in (204, 404):
            raise Unavailable(f"Docker refused remove: {resp.status_code}")

    def sessions(self) -> list[dict]:
        import json

        filters = json.dumps({"label": [f"{LABEL}=true"]})
        return self._ok(self.http.get("/containers/json", params={"all": "true", "filters": filters}), "list")


def _size(value: str) -> int:
    """'4g', '512m' -> bytes."""
    v = value.strip().lower()
    mult = {"k": 1 << 10, "m": 1 << 20, "g": 1 << 30}.get(v[-1:], 1)
    return int(float(v[:-1] if v[-1:] in "kmg" else v) * mult)


class Settings:
    def __init__(self, env=None) -> None:
        e = env if env is not None else os.environ
        self.image = e.get("VALXEL_SESSION_IMAGE", "")
        self.network = e.get("VALXEL_SESSION_NETWORK", "valxel-sessions")
        self.runtime = e.get("VALXEL_SESSION_RUNTIME", "auto").lower()  # auto, runsc, runc
        self.memory = _size(e.get("VALXEL_SESSION_MEMORY", "4g"))
        self.cpus = float(e.get("VALXEL_SESSION_CPUS", "2"))
        self.pids = int(e.get("VALXEL_SESSION_PIDS", "1024"))
        self.work = e.get("VALXEL_SESSION_WORK_SIZE", "8g")
        self.max = int(e.get("VALXEL_SESSION_MAX", "4"))
        self.wait_s = float(e.get("VALXEL_SESSION_READY_S", "120"))
        self.proxy = e.get("VALXEL_SESSION_PROXY", "http://egress-proxy:3128")
        self.no_proxy = e.get("VALXEL_SESSION_NO_PROXY", "llm-gateway,egress-proxy,localhost,127.0.0.1")
        token_file = e.get("VALXEL_SESSION_LAUNCHER_TOKEN_FILE", "")
        self.token = Path(token_file).read_text(encoding="utf-8").strip() if token_file else \
            e.get("VALXEL_SESSION_LAUNCHER_TOKEN", "")


def named_proxy(proxy: str, session_id: str) -> str:
    """http://egress-proxy:3128 -> http://<session>@egress-proxy:3128"""
    scheme, sep, rest = proxy.partition("://")
    return f"{scheme}{sep}{session_id}@{rest}" if sep and "@" not in rest and session_id else proxy


def container_body(s: Settings, runtime: str, session_id: str) -> dict:
    """The container a session runs in: locked down as the cluster's sandbox template is."""
    host: dict = {
        "ReadonlyRootfs": True,
        # Owned by the session's user: the root file system is read-only (found on a real Docker).
        "Tmpfs": {"/work": f"rw,exec,nosuid,size={s.work},uid=10001,gid=10001,mode=0700",
                  "/tmp": "rw,nosuid,size=1g,mode=1777"},
        "CapDrop": ["ALL"], "SecurityOpt": ["no-new-privileges"],
        "PidsLimit": s.pids, "Memory": s.memory, "MemorySwap": s.memory, "NanoCpus": int(s.cpus * 1e9),
        "NetworkMode": s.network, "AutoRemove": False, "Init": True,
    }
    if runtime != "runc":
        host["Runtime"] = runtime
    named = named_proxy(s.proxy, session_id)  # the proxy keeps this session's refusals (egress_proxy)
    proxy = [f"{k}={named}" for k in ("HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy")] if s.proxy else []
    return {
        "Image": s.image, "Cmd": ["valxel", "session-runner", "--port", str(RUNNER_PORT)],
        "User": "10001:10001", "WorkingDir": "/work",
        "Env": [*proxy, f"NO_PROXY={s.no_proxy}", f"no_proxy={s.no_proxy}", "HOME=/work/home",
                "TMPDIR=/tmp", f"VALXEL_SESSION_RUNNER_PORT={RUNNER_PORT}"],
        "Labels": {LABEL: "true", "valxel.io/session": session_id, "valxel.io/started": str(int(time.time()))},
        "HostConfig": host,
    }


class Launcher:
    """Starts a container per session, runs the session through it, removes it."""

    def __init__(self, settings: Settings | None = None, *, docker: Docker | None = None, http: Any = None,
                 sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic) -> None:
        self.s = settings or Settings()
        if not self.s.image:
            raise RuntimeError("VALXEL_SESSION_IMAGE is not set: the image sessions run in (the tool's, by digest)")
        self.docker = docker or Docker()
        self.http = http
        self.sleep, self.clock = sleep, clock
        self.slots = threading.BoundedSemaphore(self.s.max)
        self._runtime = ""

    def runtime(self) -> str:
        """gVisor when Docker has it (auto), or what VALXEL_SESSION_RUNTIME names."""
        if not self._runtime:
            if self.s.runtime == "auto":
                self._runtime = "runsc" if "runsc" in self.docker.runtimes() else "runc"
            else:
                if self.s.runtime == "runsc" and "runsc" not in self.docker.runtimes():
                    raise Unavailable("VALXEL_SESSION_RUNTIME is runsc but Docker has no runsc runtime (gVisor)")
                self._runtime = self.s.runtime
        return self._runtime

    def run(self, spec: dict) -> dict:
        """The session's outcome (its steps, if streamed, are dropped: see stream())."""
        from .agent_steps import read

        return read(self.stream({**spec, "stream": True}), None) if spec.get("stream") else self._run(spec)

    def _refusals(self, http, sid: str) -> list[dict]:
        """What the egress proxy refused this session, once it's done. Best effort: the proxy logs them too."""
        if not self.s.proxy or not self.s.token:
            return []
        try:
            r = http.get(f"{self.s.proxy.rstrip('/')}/refusals/{sid}", timeout=5,
                         headers={"Authorization": f"Bearer {self.s.token}"})
            got = r.json() if r.status_code == 200 else []
            return [x for x in got if isinstance(x, dict)][:200]
        except Exception:  # noqa: BLE001 - the proxy's log still has them
            log.warning("egress refusals for session %s not collected", sid)
            return []

    def stream(self, spec: dict):
        """The container's streamed /run, line by line: its steps, then the outcome. The container is
        removed when the stream ends, however it ends."""
        import httpx

        from .agent_steps import line

        if not self.slots.acquire(timeout=min(self.s.wait_s, 600)):
            yield line({"outcome": {"ok": False, "error": "Unavailable", "transient": True}})
            return
        sid = str(spec.get("id") or uuid.uuid4().hex[:16])
        cid = ""
        try:
            cid = self.docker.create(f"valxel-session-{sid}", container_body(self.s, self.runtime(), sid))
            self.docker.start(cid)
            host = self._address(cid)
            http = self.http or httpx.Client(follow_redirects=False, trust_env=False)
            self._healthy(http, host)
            req = http.build_request("POST", f"http://{host}:{RUNNER_PORT}/run", json={**spec, "stream": True},
                                     timeout=float(spec.get("timeout_s", 3600)) + 60)
            resp = http.send(req, stream=True)
            try:
                if resp.status_code != 200:
                    raise Unavailable(f"the session's container answered {resp.status_code}")
                for raw in resp.iter_lines():
                    if raw.strip():
                        yield (raw + "\n").encode()
            finally:
                resp.close()
            for r in self._refusals(http, sid):  # for the audit trail (agent_steps: actions)
                yield line({"step": {"action": {"kind": "egress.refused", "tool": "proxy", "target": r.get("host", ""),
                                                "port": r.get("port")}}})
        except (Unavailable, httpx.HTTPError) as exc:
            yield line({"outcome": {"ok": False, "error": type(exc).__name__, "transient": True}})
        finally:
            if cid:
                try:
                    self.docker.remove(cid)
                except Exception:  # noqa: BLE001 - the sweep removes it
                    log.warning("session container %s not removed", cid[:12])
            self.slots.release()

    def _run(self, spec: dict) -> dict:
        import httpx

        wait = min(self.s.wait_s, 600)
        if not self.slots.acquire(timeout=wait):
            raise Unavailable(f"all {self.s.max} session slots were busy")
        sid = str(spec.get("id") or uuid.uuid4().hex[:16])
        cid = ""
        try:
            cid = self.docker.create(f"valxel-session-{sid}", container_body(self.s, self.runtime(), sid))
            self.docker.start(cid)
            host = self._address(cid)
            http = self.http or httpx.Client(follow_redirects=False, trust_env=False)
            self._healthy(http, host)
            resp = http.post(f"http://{host}:{RUNNER_PORT}/run", json=spec, timeout=float(spec.get("timeout_s", 3600)) + 60)
            if resp.status_code != 200:
                raise Unavailable(f"the session's container answered {resp.status_code}")
            return resp.json()
        except httpx.HTTPError as exc:
            raise Unavailable(f"the session's container couldn't be reached ({type(exc).__name__})") from exc
        finally:
            if cid:
                try:
                    self.docker.remove(cid)
                except Exception:  # noqa: BLE001 - the sweep removes it
                    log.warning("session container %s not removed", cid[:12])
            self.slots.release()

    def _address(self, cid: str) -> str:
        nets = (self.docker.inspect(cid).get("NetworkSettings") or {}).get("Networks") or {}
        ip = (nets.get(self.s.network) or {}).get("IPAddress") or ""
        if not ip:
            raise Unavailable(f"the session's container has no address on {self.s.network}")
        return ip

    def _healthy(self, http, host: str) -> None:
        import httpx

        deadline = self.clock() + self.s.wait_s
        while True:
            try:
                if http.get(f"http://{host}:{RUNNER_PORT}/healthz", timeout=5).status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            if self.clock() >= deadline:
                raise Unavailable("the session's container wasn't ready in time")
            self.sleep(0.5)

    def sweep(self, older_than_s: float = 6 * 3600, now: float | None = None) -> list[str]:
        """Remove session containers left behind (a launcher restart, a crash), older than the longest
        session could be."""
        now = now if now is not None else time.time()
        gone = []
        for c in self.docker.sessions():
            started = float((c.get("Labels") or {}).get("valxel.io/started") or 0)
            if now - started > older_than_s:
                try:
                    self.docker.remove(c["Id"])
                    gone.append(c["Id"])
                except Exception:  # noqa: BLE001 - tried again on the next sweep
                    log.warning("leftover session container %s not removed", c["Id"][:12])
        return gone


def make_app(launcher: Launcher):
    from fastapi import FastAPI, Header, HTTPException

    app = FastAPI(title="Valxel session launcher")

    @app.get("/healthz")
    def healthz() -> dict:
        return {"ok": True, "runtime": launcher.runtime()}

    @app.post("/sessions")
    def sessions(spec: dict, authorization: str = Header(default="")):
        want = launcher.s.token
        got = authorization.removeprefix("Bearer ").strip()
        if not want or not hmac.compare_digest(got.encode(), want.encode()):
            raise HTTPException(401, "a valid launcher token is required")
        if spec.get("stream"):  # the session's steps as they come, then its outcome
            from fastapi.responses import StreamingResponse

            return StreamingResponse(launcher.stream(spec), media_type="application/x-ndjson")
        try:
            return launcher.run(spec)
        except Unavailable as exc:
            raise HTTPException(503, str(exc)) from exc

    return app


def serve(port: int = 8083) -> None:
    """`valxel session-launcher`: the only process with the Docker socket; sweeps every 10 minutes."""
    import uvicorn

    launcher = Launcher()
    if not launcher.s.token:
        raise RuntimeError("VALXEL_SESSION_LAUNCHER_TOKEN_FILE is not set: the worker's token to start sessions")

    def sweeping() -> None:
        while True:
            try:
                launcher.sweep()
            except Exception:
                log.warning("session sweep failed", exc_info=True)
            time.sleep(600)

    threading.Thread(target=sweeping, daemon=True, name="session-sweep").start()
    log.info("session launcher: image %s, runtime %s, network %s", launcher.s.image, launcher.runtime(),
             launcher.s.network)
    uvicorn.run(make_app(launcher), host="0.0.0.0", port=port, log_level="info")


# --------------------------------------------------------------------------- the tool's side

class DockerExecutor:
    """The worker's side: hands the session to the launcher and waits for the outcome."""

    def __init__(self, *, url: str, token: str, http: Any = None) -> None:
        self.url, self.token, self.http = url.rstrip("/"), token, http

    def run(self, spec: dict, on_step=None) -> dict:
        import httpx

        from .agent_steps import read
        from .session_runner import Unavailable as SessionUnavailable

        http = self.http or httpx.Client(follow_redirects=False, trust_env=False)
        body = {**spec, "stream": True} if on_step else spec
        try:
            req = http.build_request("POST", f"{self.url}/sessions", json=body,
                                     headers={"Authorization": f"Bearer {self.token}"},
                                     timeout=float(spec.get("timeout_s", 3600)) + 300)
            resp = http.send(req, stream=bool(on_step))
            try:
                if resp.status_code != 200:
                    resp.read()
                    raise SessionUnavailable(f"the session launcher answered {resp.status_code}: {resp.text[:200]}")
                return read(resp.iter_lines(), on_step) if on_step else resp.json()
            finally:
                resp.close()
        except httpx.HTTPError as exc:
            raise SessionUnavailable(f"the session launcher couldn't be reached ({type(exc).__name__})") from exc


def executor_from_env(env=None) -> DockerExecutor:
    e = env if env is not None else os.environ
    token_file = e.get("VALXEL_SESSION_LAUNCHER_TOKEN_FILE", "")
    token = Path(token_file).read_text(encoding="utf-8").strip() if token_file else e.get("VALXEL_SESSION_LAUNCHER_TOKEN", "")
    if not token:
        raise RuntimeError("VALXEL_SESSION_EXECUTOR is docker but there's no launcher token "
                           "(VALXEL_SESSION_LAUNCHER_TOKEN_FILE)")
    return DockerExecutor(url=e.get("VALXEL_SESSION_LAUNCHER_URL", "http://session-launcher:8083"), token=token)

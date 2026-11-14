"""Docker on a lite-mode VM: the Docker CLI, or an in-memory stand-in for tests.

The tool may run on the VM itself or in Valxel Cloud. Either way it drives Docker through the CLI:
`DOCKER_HOST=ssh://valxel@<vm>` reaches a remote VM over SSH with no Docker port exposed.
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
from collections.abc import Callable
from typing import Protocol

import yaml


class DockerError(RuntimeError):
    pass


class Docker(Protocol):
    def up(self, project: str, spec: dict, *, networks: tuple[str, ...] = ()) -> None: ...
    # Applies the whole Compose file: changed services restart, removed ones stop. `networks`
    # are created first if missing (the environment's edge network).
    def down(self, project: str) -> None: ...  # stops and removes containers; volumes stay
    def ps(self, project: str) -> list[dict]: ...  # [{"service", "state": running|exited|..., "health", "exit_code"}]
    def logs(self, project: str, service: str, *, minutes: int = 60, tail: int = 200) -> list[str]: ...
    def projects(self) -> list[str]: ...


Runner = Callable[..., subprocess.CompletedProcess]


class DockerCli:
    def __init__(self, host: str = "", *, runner: Runner = subprocess.run, timeout: int = 600) -> None:
        self.host = host
        self._run_fn = runner
        self.timeout = timeout

    def _run(self, args: list[str], *, stdin: str | None = None, check: bool = True) -> str:
        env = dict(os.environ)
        if self.host:
            env["DOCKER_HOST"] = self.host
        res = self._run_fn(["docker", *args], input=stdin, capture_output=True, text=True, env=env,
                           timeout=self.timeout)
        if check and res.returncode != 0:
            raise DockerError(f"docker {' '.join(args[:3])}: {(res.stderr or res.stdout).strip()[-500:]}")
        return res.stdout or ""

    def up(self, project, spec, *, networks=()) -> None:
        for n in networks:
            if not self._run(["network", "ls", "-q", "--filter", f"name=^{n}$"]).strip():
                self._run(["network", "create", n])
        self._run(["compose", "-p", project, "-f", "-", "up", "-d", "--remove-orphans", "--pull", "missing"],
                  stdin=yaml.safe_dump(spec, sort_keys=False))

    def down(self, project) -> None:
        self._run(["compose", "-p", project, "down", "--remove-orphans"])

    def ps(self, project) -> list[dict]:
        out = self._run(["compose", "-p", project, "ps", "-a", "--format", "json"]).strip()
        if not out:
            return []
        rows = json.loads(out) if out.startswith("[") else [json.loads(line) for line in out.splitlines() if line.strip()]
        return [{"service": r.get("Service", ""), "state": (r.get("State") or "").lower(),
                 "health": (r.get("Health") or "").lower(), "exit_code": r.get("ExitCode", 0)} for r in rows]

    def logs(self, project, service, *, minutes=60, tail=200) -> list[str]:
        out = self._run(["compose", "-p", project, "logs", "--no-color", "--no-log-prefix", "--since",
                         f"{int(minutes)}m", "--tail", str(int(tail)), service], check=False)
        return [line for line in out.splitlines() if line.strip()]

    def projects(self) -> list[str]:
        out = self._run(["compose", "ls", "-a", "--format", "json"]).strip()
        return sorted(p.get("Name", "") for p in (json.loads(out) if out else []))


class StubDocker:
    """In memory. Every service runs as soon as it is applied, unless a test sets
    `states[(project, service)]` (e.g. "exited") or `health[(project, service)]` ("unhealthy")."""

    def __init__(self) -> None:
        self.applied: dict[str, dict] = {}  # project -> the last Compose file
        self.history: list[tuple[str, str]] = []  # (action, project)
        self.networks: set[str] = set()
        self.states: dict[tuple[str, str], str] = {}
        self.health: dict[tuple[str, str], str] = {}
        self.log_lines: dict[tuple[str, str], list[str]] = {}
        self.fail_up: str = ""  # tests: the error `up` raises
        self._lock = threading.Lock()

    def up(self, project, spec, *, networks=()) -> None:
        if self.fail_up:
            raise DockerError(self.fail_up)
        with self._lock:
            self.networks.update(networks)
            self.applied[project] = json.loads(json.dumps(spec))
            self.history.append(("up", project))

    def down(self, project) -> None:
        with self._lock:
            self.applied.pop(project, None)
            self.history.append(("down", project))

    def ps(self, project) -> list[dict]:
        with self._lock:
            spec = self.applied.get(project) or {}
            return [{"service": s, "state": self.states.get((project, s), "running"),
                     "health": self.health.get((project, s), ""), "exit_code": 0}
                    for s in (spec.get("services") or {})]

    def logs(self, project, service, *, minutes=60, tail=200) -> list[str]:
        return list(self.log_lines.get((project, service), []))[-tail:]

    def projects(self) -> list[str]:
        with self._lock:
            return sorted(self.applied)

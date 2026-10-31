"""OpenChoreo interface: WorkflowRuns for builds, promotion along the DeploymentPipeline.

Agents may promote only to non-production environments (VX-3). Production promotion is a
human action at G6; in a real deployment the approver promotes in the OpenChoreo portal
and a watcher resumes the LangGraph thread.
"""

from __future__ import annotations

import itertools
import threading
from typing import Protocol

PIPELINE = ("dev", "staging", "production")


class OpenChoreo(Protocol):
    def run_workflow(self, *, component: str, ref: str, kind: str, project: str | None = None) -> dict: ...
    def promote(self, *, component: str, env: str, actor: str, actor_type: str) -> dict: ...
    def environment(self, component: str) -> str | None: ...


class StubOpenChoreo:
    def __init__(self) -> None:
        self.runs: list[dict] = []
        self.deployments: dict[str, str] = {}  # component -> current highest env
        self.promotions: list[dict] = []
        self._seq = itertools.count(1)
        self._lock = threading.Lock()

    def run_workflow(self, *, component, ref, kind, project=None) -> dict:
        with self._lock:
            run = {"run_id": f"wfr-{next(self._seq)}", "component": component, "ref": ref, "kind": kind,
                   "project": project, "status": "succeeded"}
            self.runs.append(run)
            if kind == "main":
                self.deployments[component] = "dev"
            return dict(run)

    def environment(self, component: str) -> str | None:
        with self._lock:
            return self.deployments.get(component)

    def promote(self, *, component, env, actor, actor_type) -> dict:
        if env not in PIPELINE:
            raise ValueError(f"Unknown environment {env!r}")
        if env == "production" and actor_type != "human":
            raise PermissionError("Agents cannot promote to production (VX-3); G6 is a human action")
        with self._lock:
            current = self.deployments.get(component)
            if current is None or PIPELINE.index(env) != PIPELINE.index(current) + 1:
                raise ValueError(f"{component}: cannot promote from {current!r} to {env!r}")
            self.deployments[component] = env
            rec = {"component": component, "env": env, "actor": actor, "actor_type": actor_type}
            self.promotions.append(rec)
            return dict(rec)

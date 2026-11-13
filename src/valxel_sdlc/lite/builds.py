"""Image builds in lite mode.

There is no OpenChoreo build plane: the template's own CI builds the image, scans and signs it,
and pushes it to the registry pinned by digest. A finished build is a release, as in OpenChoreo:
the image digest and the workload.yaml at the commit that was built, both immutable.

`Builds` is what `ComposeRuntime` needs from that CI. The GitHub Actions implementation lands with
the image-builds step; `StubBuilds` stands in for it here and in tests.
"""

from __future__ import annotations

import hashlib
import itertools
import threading
from typing import Protocol

from ..integrations.openchoreo import BUILD_STEPS


class Builds(Protocol):
    def start(self, *, component: str, ref: str, kind: str, repo_url: str) -> dict: ...  # {"run_id", "url"}
    def status(self, *, run_id: str, component: str) -> dict: ...
    # {"status": running|succeeded|failed, "message", "tasks", "started_at", "finished_at",
    #  and once succeeded: "image" (repo@sha256:...) and "workload" (the parsed workload.yaml)}


class StubBuilds:
    """Builds succeed at once with a digest made from the component and ref, unless a test sets
    `results[component]`. `workloads[component]` is the workload.yaml each build carries."""

    def __init__(self, registry: str = "ghcr.io/example") -> None:
        self.registry = registry
        self.results: dict[str, str] = {}
        self.workloads: dict[str, dict] = {}
        self.started: list[dict] = []
        self._runs: dict[str, dict] = {}
        self._seq = itertools.count(1)
        self._lock = threading.Lock()

    def start(self, *, component, ref, kind, repo_url) -> dict:
        with self._lock:
            run = {"run_id": f"lite-run-{next(self._seq)}", "component": component, "ref": ref, "kind": kind,
                   "repo_url": repo_url, "url": ""}
            self._runs[run["run_id"]] = run
            self.started.append(run)
            return {"run_id": run["run_id"], "url": run["url"]}

    def status(self, *, run_id, component) -> dict:
        with self._lock:
            run = self._runs.get(run_id)
        if run is None:
            return {"status": "failed", "message": f"{run_id}: no such build"}
        state = self.results.get(component, "succeeded")
        out = {"status": state, "message": "" if state == "succeeded" else f"{component} build {state}",
               "tasks": list(BUILD_STEPS), "started_at": "2026-11-13T15:10:00+00:00",
               "finished_at": "2026-11-13T15:14:00+00:00"}
        if state == "succeeded":
            name = component.rsplit("/", 1)[-1]
            digest = hashlib.sha256(f"{component}@{run['ref']}".encode()).hexdigest()
            out |= {"image": f"{self.registry}/{name}@sha256:{digest}",
                    "workload": self.workloads.get(component) or {"metadata": {"name": name}, "endpoints": []}}
        return out

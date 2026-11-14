"""Image builds in lite mode.

Builds stay on Valxel's OpenChoreo build plane in both phases of a lite product (hosted while
building, on the customer's VM after go-live): it builds, scans and signs the image as in full
mode. A finished build is a release, as in OpenChoreo: the image pinned by digest and the
workload.yaml at the commit that was built, both immutable.

`Builds` is what `ComposeRuntime` needs from the build plane. `OpenChoreoBuilds` is the real one;
`StubBuilds` stands in for it in tests.
"""

from __future__ import annotations

import hashlib
import itertools
import threading
from typing import Protocol

import yaml

from ..integrations.openchoreo import BUILD_STEPS

WORKLOAD_FILE = "workload.yaml"


class Builds(Protocol):
    def start(self, *, component: str, ref: str, kind: str, repo_url: str, project: str | None = None,
              namespace: str | None = None) -> dict: ...  # {"run_id", "url"}
    def status(self, *, run_id: str, component: str, ref: str = "", project: str | None = None,
               namespace: str | None = None) -> dict: ...
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

    def start(self, *, component, ref, kind, repo_url, project=None, namespace=None) -> dict:
        with self._lock:
            run = {"run_id": f"lite-run-{next(self._seq)}", "component": component, "ref": ref, "kind": kind,
                   "repo_url": repo_url, "url": ""}
            self._runs[run["run_id"]] = run
            self.started.append(run)
            return {"run_id": run["run_id"], "url": run["url"]}

    def status(self, *, run_id, component, ref="", project=None, namespace=None) -> dict:
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


class OpenChoreoBuilds:
    """Builds on the OpenChoreo build plane, for a VM runtime.

    `oc` is the OpenChoreo client (OpenChoreoMcp: run_workflow, run_status, release_image) and
    `github` reads the component's workload.yaml at the commit that was built. `repo_of` maps an
    OpenChoreo component to its repo when the names differ (default: the component is "org/repo")."""

    def __init__(self, oc, github, *, repo_of=None) -> None:
        self.oc = oc
        self.github = github
        self.repo_of = repo_of or (lambda component: component)

    def start(self, *, component, ref, kind, repo_url, project=None, namespace=None) -> dict:
        run = self.oc.run_workflow(component=component, ref=ref, kind=kind, project=project, namespace=namespace)
        return {"run_id": run["run_id"], "url": run.get("url", "")}

    def status(self, *, run_id, component, ref="", project=None, namespace=None) -> dict:
        s = dict(self.oc.run_status(run_id=run_id, component=component, project=project, namespace=namespace))
        if s.get("status") != "succeeded":
            return s
        rel = self.oc.release_image(component=component, project=project, namespace=namespace)
        text = self.github.get_file(repo=self.repo_of(component), path=WORKLOAD_FILE, ref=ref)
        if text is None:
            return {**s, "status": "failed", "message": f"{component}: no {WORKLOAD_FILE} at {ref or 'the default branch'}"}
        workload = yaml.safe_load(text) or {}
        if not isinstance(workload, dict):
            return {**s, "status": "failed", "message": f"{component}: {WORKLOAD_FILE} isn't a mapping"}
        return {**s, "image": rel["image"], "workload": workload, "openchoreo_release": rel["release"]}

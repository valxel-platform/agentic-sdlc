"""OpenChoreo interface: component builds (WorkflowRuns) and deployments (ReleaseBindings).

OpenChoreo 1.x model: a WorkflowRun builds a commit; the result becomes the component's latest
ComponentRelease, an immutable snapshot; a ReleaseBinding binds one release to one environment.
Deploying to the first environment binds the latest release; promoting binds the release that
the previous environment runs. Bindings report readiness in their status.

Agents may deploy only to non-production environments (VX-3). Production is bound after the G6
approval, on behalf of the human approver, and the pipeline waits until it is ready.

`OpenChoreoMcp` drives the control plane's MCP server (`<api host>/mcp`) with a client-
credentials token for OpenChoreo's confidential service client. Tool arguments are matched to
the published tool schemas, so the adapter tolerates renamed parameters across releases.
"""

from __future__ import annotations

import itertools
import threading
from typing import Any, Protocol

from .mcp_client import McpError, McpTools, fit_args, pick_tool

PIPELINE = ("dev", "staging", "production")


class OpenChoreo(Protocol):
    def run_workflow(self, *, component: str, ref: str, kind: str, project: str | None = None,
                     namespace: str | None = None) -> dict: ...  # {"run_id", "status", "url"}
    def run_status(self, *, run_id: str, component: str, project: str | None = None,
                   namespace: str | None = None) -> dict: ...  # {"status": running|succeeded|failed, "message"}
    def promote(self, *, component: str, env: str, actor: str, actor_type: str, project: str | None = None,
                namespace: str | None = None, pipeline: tuple[str, ...] = PIPELINE) -> dict: ...
    def deployment(self, *, component: str, env: str, project: str | None = None,
                   namespace: str | None = None) -> dict: ...  # {"state": ready|progressing|failed|absent, ...}
    def environment(self, component: str) -> str | None: ...  # furthest environment requested so far
    def register_component(self, *, component: str, project: str | None, namespace: str | None,
                           component_type: str, workflow: str | None, repo_url: str, branch: str) -> str: ...


def check_promotion(env: str, actor_type: str, pipeline: tuple[str, ...]) -> None:
    if env not in pipeline:
        raise ValueError(f"Unknown environment {env!r}; the pipeline is {list(pipeline)}")
    if env == pipeline[-1] and actor_type != "human":
        raise PermissionError("Agents cannot promote to production (VX-3); G6 is a human action")


class StubOpenChoreo:
    """In memory. Builds succeed and deployments are ready at once unless a test says otherwise
    through `run_results` (component -> status) or `deploy_results` ((component, env) -> state)."""

    def __init__(self) -> None:
        self.runs: list[dict] = []
        self.deployments: dict[str, str] = {}  # component -> furthest env requested
        self.promotions: list[dict] = []
        self.run_results: dict[str, str] = {}
        self.deploy_results: dict[tuple[str, str], str] = {}
        self.run_tasks: dict[str, list[dict]] = {}  # component -> build steps
        self.components: dict[str, dict] = {}  # registered by provisioning
        self._seq = itertools.count(1)
        self._lock = threading.Lock()

    def register_component(self, *, component, project, namespace, component_type, workflow, repo_url,
                           branch) -> str:
        with self._lock:
            new = component not in self.components
            self.components.setdefault(component, {"project": project, "namespace": namespace,
                                                   "component_type": component_type, "workflow": workflow,
                                                   "repo_url": repo_url, "branch": branch})
        return "created" if new else "exists"

    def run_workflow(self, *, component, ref, kind, project=None, namespace=None) -> dict:
        with self._lock:
            run = {"run_id": f"wfr-{next(self._seq)}", "component": component, "ref": ref, "kind": kind,
                   "project": project, "status": "running", "url": ""}
            self.runs.append(run)
            return dict(run)

    def run_status(self, *, run_id, component, project=None, namespace=None) -> dict:
        return {"status": self.run_results.get(component, "succeeded"), "message": "",
                "tasks": list(self.run_tasks.get(component, []))}

    def environment(self, component: str) -> str | None:
        with self._lock:
            return self.deployments.get(component)

    def promote(self, *, component, env, actor, actor_type, project=None, namespace=None,
                pipeline=PIPELINE) -> dict:
        check_promotion(env, actor_type, pipeline)
        with self._lock:
            current = self.deployments.get(component)
            target, prev = pipeline.index(env), (pipeline.index(current) if current in pipeline else -1)
            if target > prev + 1:
                raise ValueError(f"{component}: cannot promote from {current!r} to {env!r}")
            if target == prev + 1:
                self.deployments[component] = env
            rec = {"component": component, "env": env, "actor": actor, "actor_type": actor_type,
                   "release": f"{component.rsplit('/', 1)[-1]}-r1"}
            self.promotions.append(rec)
            return dict(rec)

    def deployment(self, *, component, env, project=None, namespace=None) -> dict:
        with self._lock:
            current = self.deployments.get(component)
        if current is None or PIPELINE.index(current) < PIPELINE.index(env if env in PIPELINE else "dev"):
            return {"state": "absent"}
        return {"state": self.deploy_results.get((component, env), "ready"), "release": "r1", "url": ""}


# --------------------------------------------------------------------------- MCP adapter

_NS = ("namespace", "namespace_name", "namespaceName")
_PROJECT = ("project", "project_name", "projectName")
_COMPONENT = ("component", "component_name", "componentName")
_ENV = ("environment", "environment_name", "env", "environmentName")
_RELEASE = ("release", "release_name", "component_release", "releaseName")
_RUN = ("name", "workflow_run", "workflow_run_name", "run_name", "workflowRunName")


def _get(d: Any, *paths: str) -> Any:
    """First non-empty value among dotted paths, e.g. 'spec.environment'."""
    for path in paths:
        cur = d
        for part in path.split("."):
            cur = cur.get(part) if isinstance(cur, dict) else None
        if cur not in (None, "", []):
            return cur
    return None


def _list(res: Any) -> list[dict]:
    if isinstance(res, list):
        return [r for r in res if isinstance(r, dict)]
    if isinstance(res, dict):
        for k in ("items", "data", "release_bindings", "releaseBindings", "bindings", "results"):
            if isinstance(res.get(k), list):
                return [r for r in res[k] if isinstance(r, dict)]
        if _get(res, "name", "metadata.name"):
            return [res]
    return []


def _condition(obj: dict, ctype: str) -> str | None:
    for c in _get(obj, "status.conditions", "conditions") or []:
        if isinstance(c, dict) and c.get("type") == ctype:
            return str(c.get("status"))
    return None


class OpenChoreoMcp:
    def __init__(self, server: McpTools, *, default_namespace: str = "default", console_url: str = "") -> None:
        self.server = server
        self.default_namespace = default_namespace
        self.console_url = console_url.rstrip("/")
        self._requested: dict[str, str] = {}
        self._lock = threading.Lock()

    @staticmethod
    def name(component: str) -> str:
        """Repos are "<org>/<repo>"; the OpenChoreo component is the repo name unless the
        product's runtime maps it (the caller passes the mapped name)."""
        return component.rsplit("/", 1)[-1]

    def _call(self, candidates: tuple[str, ...], wanted: dict) -> Any:
        tool = pick_tool(self.server, *candidates)
        return self.server.call(tool, fit_args(self.server, tool, wanted))

    def _scope(self, component: str, project: str | None, namespace: str | None) -> dict:
        return {_NS: namespace or self.default_namespace, _PROJECT: project, _COMPONENT: self.name(component)}

    # -- components
    def register_component(self, *, component, project, namespace, component_type, workflow, repo_url,
                           branch) -> str:
        """Create the component if missing. The pipeline triggers builds itself (autoBuild off) and
        deploys through release bindings (autoDeploy off), so every change passes the gates."""
        scope = self._scope(component, project, namespace)
        try:
            if self._call(("get_component",), scope):
                return "exists"
        except McpError:
            pass  # not found
        self._call(("create_component",), {
            **scope,
            ("component_type", "componentType", "type"): component_type,
            ("workflow", "workflow_name", "build_workflow"): workflow,
            ("repository_url", "repo_url", "git_repository", "repository"): repo_url,
            ("branch", "git_branch"): branch,
            ("auto_deploy", "autoDeploy"): False,
            ("auto_build", "autoBuild"): False,
        })
        return "created"

    # -- builds
    def run_workflow(self, *, component, ref, kind, project=None, namespace=None) -> dict:
        res = self._call(("trigger_workflow_run",), {
            **self._scope(component, project, namespace), ("commit", "revision", "git_commit", "ref"): ref})
        run_id = _get(res, "name", "metadata.name", "workflow_run", "workflowRun.name") if isinstance(res, dict) else res
        if not run_id:
            raise McpError(f"openchoreo: trigger_workflow_run returned no run name: {str(res)[:200]}")
        return {"run_id": str(run_id), "component": component, "ref": ref, "kind": kind, "project": project,
                "status": "running", "url": self._console(project, component, namespace)}

    def run_status(self, *, run_id, component, project=None, namespace=None) -> dict:
        res = self._call(("get_workflow_run_status", "get_workflow_run"), {
            **self._scope(component, project, namespace), _RUN: run_id})
        res = res if isinstance(res, dict) else {"status": res}
        if _condition(res, "WorkflowFailed") == "True":
            status = "failed"
        elif _condition(res, "WorkflowSucceeded") == "True":
            status = "succeeded"
        else:
            raw = str(_get(res, "status.phase", "phase", "status") or "").lower()
            status = ("succeeded" if raw in ("succeeded", "success", "completed")
                      else "failed" if raw in ("failed", "error") else "running")
        failed = [t for t in _get(res, "status.tasks", "tasks") or []
                  if isinstance(t, dict) and str(t.get("phase", "")).lower() in ("failed", "error")]
        message = "; ".join(f"{t.get('name')}: {t.get('message', '')}".strip(": ") for t in failed)
        tasks = [{"name": t.get("name", ""), "phase": t.get("phase", ""), "message": t.get("message", "")}
                 for t in _get(res, "status.tasks", "tasks") or [] if isinstance(t, dict)]
        return {"status": status, "message": message, "tasks": tasks}

    # -- deployments
    def _bindings(self, component, project, namespace) -> list[dict]:
        return _list(self._call(("list_release_bindings",), self._scope(component, project, namespace)))

    @staticmethod
    def _env_of(b: dict) -> str:
        return str(_get(b, "environment", "spec.environment", "environmentName") or "")

    @staticmethod
    def _release_of(b: dict) -> str:
        return str(_get(b, "release", "spec.releaseName", "spec.release", "releaseName") or "")

    def _latest_release(self, component, project, namespace) -> str:
        comp = self._call(("get_component",), self._scope(component, project, namespace))
        latest = _get(comp, "status.latestRelease.name", "latestRelease.name", "latest_release")
        if latest:
            return str(latest)
        res = self._call(("create_component_release",), self._scope(component, project, namespace))
        name = _get(res, "name", "metadata.name")
        if not name:
            raise McpError(f"openchoreo: {self.name(component)} has no release to deploy")
        return str(name)

    def promote(self, *, component, env, actor, actor_type, project=None, namespace=None,
                pipeline=PIPELINE) -> dict:
        check_promotion(env, actor_type, pipeline)
        bindings = {self._env_of(b): b for b in self._bindings(component, project, namespace)}
        idx = pipeline.index(env)
        if idx == 0:
            release = self._latest_release(component, project, namespace)
        else:
            prev = bindings.get(pipeline[idx - 1])
            release = self._release_of(prev) if prev else ""
            if not release:
                raise ValueError(f"{component}: nothing is deployed to {pipeline[idx - 1]} to promote")
        scope = self._scope(component, project, namespace)
        existing = bindings.get(env)
        if existing is None:
            self._call(("create_release_binding",), {**scope, _ENV: env, _RELEASE: release})
        elif self._release_of(existing) != release:
            self._call(("update_release_binding",), {
                **scope, _ENV: env, ("binding", "release_binding", "name", "binding_name"): _get(existing, "name", "metadata.name"),
                _RELEASE: release, ("release_state", "state"): "Active"})
        with self._lock:
            cur = self._requested.get(component)
            if cur is None or (cur in pipeline and pipeline.index(cur) < idx):
                self._requested[component] = env
        return {"component": component, "env": env, "release": release, "actor": actor, "actor_type": actor_type}

    def deployment(self, *, component, env, project=None, namespace=None) -> dict:
        b = next((x for x in self._bindings(component, project, namespace) if self._env_of(x) == env), None)
        if b is None:
            return {"state": "absent"}
        ready = _condition(b, "Ready")
        raw = str(_get(b, "status.status", "status.state", "state") or "").lower()
        if ready == "True" or raw in ("ready", "active", "deployed"):
            state = "ready"
        elif raw in ("failed", "error") or _condition(b, "Failed") == "True":
            state = "failed"
        else:
            state = "progressing"
        return {"state": state, "release": self._release_of(b), "url": _get(b, "status.endpoints.0.url") or ""}

    def environment(self, component: str) -> str | None:
        with self._lock:
            return self._requested.get(component)

    def _console(self, project, component, namespace) -> str:
        if not self.console_url:
            return ""
        return (f"{self.console_url}/catalog/{namespace or self.default_namespace}/component/"
                f"{self.name(component)}")

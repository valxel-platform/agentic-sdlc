"""OpenChoreo interface: component builds (WorkflowRuns) and deployments (ReleaseBindings).

OpenChoreo 1.x model: a WorkflowRun builds a commit; the result becomes the component's latest
ComponentRelease, an immutable snapshot; a ReleaseBinding binds one release to one environment.
Deploying to the first environment binds the latest release; promoting binds the release that
the previous environment runs. Bindings report readiness in their status.

Managed services (VX-19) follow the same model: a Resource (from a platform ClusterResourceType)
gets an immutable ResourceRelease per change, and a ResourceReleaseBinding binds a release to one
environment with that environment's size. The pipeline binds resources before the components
that use them, and never deletes one.

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
    # Change-set environments (vrixel_sdlc.changeset_env): one per open change set, on the data plane
    # of an existing environment, outside the deployment pipeline, deleted when the change set merges.
    def create_environment(self, *, name: str, like: str, project: str | None = None,
                           namespace: str | None = None, labels: dict | None = None) -> dict: ...
    def delete_environment(self, *, name: str, project: str | None = None, namespace: str | None = None) -> None: ...
    def list_environments(self, *, namespace: str | None = None) -> list[dict]: ...  # [{"name", "created", "labels"}]
    def bind_latest(self, *, component: str, env: str, actor: str, project: str | None = None,
                    namespace: str | None = None) -> dict: ...  # the component's newest release, in env
    def unbind(self, *, component: str, env: str, project: str | None = None, namespace: str | None = None) -> None: ...
    def register_component(self, *, component: str, project: str | None, namespace: str | None,
                           component_type: str, workflow: str | None, repo_url: str, branch: str,
                           parameters: dict | None = None) -> str: ...  # the type's parameters, e.g. an agent's isolation
    # Adds the traits a component lacks (by instanceName); never changes or removes one. Returns
    # the instance names added.
    def ensure_traits(self, *, component: str, traits: list[dict], project: str | None = None,
                      namespace: str | None = None) -> list[str]: ...
    # Operations: what a component is doing in an environment, and putting its previous release back.
    def observe(self, *, component: str, env: str, project: str | None = None, namespace: str | None = None,
                minutes: int = 60) -> dict: ...  # {"available", "logs": [str], "metrics": {...}}
    def rollback(self, *, component: str, env: str, actor: str, actor_type: str, project: str | None = None,
                 namespace: str | None = None) -> dict: ...  # {"from", "to"}
    # Managed services (VX-19).
    def list_resources(self, *, project: str, namespace: str | None = None) -> list[dict]: ...  # [{"name", "type", "parameters"}]
    def apply_resource(self, *, name: str, type: str, parameters: dict, project: str,
                       namespace: str | None = None) -> dict: ...  # {"name", "release", "change": created|updated|unchanged}
    def bind_resource(self, *, name: str, env: str, size: str, actor: str, actor_type: str, project: str,
                      namespace: str | None = None, pipeline: tuple[str, ...] = PIPELINE,
                      retain: bool = False) -> dict: ...  # retain: keep the data if the binding goes
    def resource_status(self, *, name: str, env: str, project: str,
                        namespace: str | None = None) -> dict: ...  # {"state": ready|progressing|failed|absent, "message"}
    # A product's own environments (vrixel_sdlc.environments): created at the product gate on the
    # product's data planes, which the platform registers once their clusters exist. Production is
    # created for the person who approved the gate, never by an agent (VX-3).
    def data_plane(self, *, name: str, namespace: str | None = None) -> dict: ...  # {"state": ready|absent, ...}
    def ensure_project(self, *, project: str, pipeline: str, namespace: str | None = None) -> str: ...  # created|exists
    def ensure_environment(self, *, name: str, data_plane: str, production: bool, actor: str,
                           namespace: str | None = None, labels: dict | None = None) -> str: ...  # created|exists
    def ensure_pipeline(self, *, name: str, stages: list[str], namespace: str | None = None) -> str: ...
    # The capacity a release binding runs with in one environment (vrixel_sdlc.capacity.binding_configs):
    # componentTypeEnvironmentConfigs and traitEnvironmentConfigs, replacing what was set before.
    def configure_binding(self, *, component: str, env: str, configs: dict, traits: dict,
                          project: str | None = None, namespace: str | None = None) -> dict: ...
    # Inventory (read only): where a component runs and what a resource is, per environment.
    def component_bindings(self, *, component: str, project: str | None = None,
                           namespace: str | None = None) -> list[dict]: ...  # [{"env", "release", "state", "binding"}]
    def resource_bindings_of(self, *, name: str, project: str,
                             namespace: str | None = None) -> list[dict]: ...
    # [{"env", "release", "state", "binding", "size", "outputs": [{"name", "value"} | {"name", "secret": {"name", "key"}}]}]


# Who may roll production back: a person, or the tool under the platform's remediation policy
# (ops/remediation.py: only a recent release, and the release restored was already approved).
ROLLBACK_ACTORS = ("human", "policy")


# Who may roll production back: a person, or the tool under the platform's remediation policy
# (ops/remediation.py: only a recent release, and the release restored was already approved).
ROLLBACK_ACTORS = ("human", "policy")


def check_promotion(env: str, actor_type: str, pipeline: tuple[str, ...]) -> None:
    if env not in pipeline:
        raise ValueError(f"Unknown environment {env!r}; the pipeline is {list(pipeline)}")
    if env == pipeline[-1] and actor_type != "human":
        raise PermissionError("Agents cannot promote to production (VX-3); G6 is a human action")


# The platform build workflow's steps (platform/openchoreo/build-workflow.yaml), as the stub reports them.
BUILD_STEPS = tuple({"name": n, "phase": "Succeeded", "message": ""}
                    for n in ("checkout", "build-image", "push", "scan-image", "sbom", "sign", "attest-provenance"))


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
        self.observations: dict[tuple[str, str], dict] = {}  # (component, env) -> what observe returns
        self.rollbacks: list[dict] = []
        self.resources: dict[str, dict] = {}  # name -> {"type", "parameters", "project", "releases": n}
        self.resource_bindings: list[dict] = []
        self.resource_results: dict[tuple[str, str], str] = {}  # (name, env) -> state
        self.traits: dict[str, list[dict]] = {}  # component -> traits
        self.environments: dict[str, dict] = {}  # change-set environments: name -> {"like", "created", ...}
        self.env_bindings: dict[tuple[str, str], str] = {}  # (component, change-set env) -> release
        self.env_results: dict[tuple[str, str], str] = {}  # tests: (component, env) -> state
        self.refuse_environments = False  # tests: an OpenChoreo that can't create environments
        self.data_planes: set[str] = set()  # registered data planes (the platform registers them)
        self.projects: dict[str, dict] = {}
        self.product_environments: dict[str, dict] = {}
        self.pipelines: dict[str, list[str]] = {}
        self.binding_configs: dict[tuple[str, str], dict] = {}  # (component, env) -> capacity applied
        self._seq = itertools.count(1)
        self._lock = threading.Lock()

    def register_component(self, *, component, project, namespace, component_type, workflow, repo_url,
                           branch, parameters=None) -> str:
        with self._lock:
            new = component not in self.components
            self.components.setdefault(component, {"project": project, "namespace": namespace,
                                                   "component_type": component_type, "workflow": workflow,
                                                   "repo_url": repo_url, "branch": branch,
                                                   "parameters": dict(parameters or {})})
        return "created" if new else "exists"

    def ensure_traits(self, *, component, traits, project=None, namespace=None) -> list[str]:
        from ..ops.alert_rules import missing

        with self._lock:
            have = self.traits.setdefault(component, [])
            add = missing(have, traits)
            have.extend(add)
        return [t["instanceName"] for t in add]

    def run_workflow(self, *, component, ref, kind, project=None, namespace=None) -> dict:
        with self._lock:
            run = {"run_id": f"wfr-{next(self._seq)}", "component": component, "ref": ref, "kind": kind,
                   "project": project, "status": "running", "url": ""}
            self.runs.append(run)
            return dict(run)

    def run_status(self, *, run_id, component, project=None, namespace=None) -> dict:
        return {"status": self.run_results.get(component, "succeeded"), "message": "",
                "tasks": list(self.run_tasks.get(component, BUILD_STEPS)),
                "started_at": "2026-11-02T15:10:00+00:00", "finished_at": "2026-11-02T15:16:00+00:00"}

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

    def data_plane(self, *, name, namespace=None) -> dict:
        with self._lock:
            return {"state": "ready" if name in self.data_planes else "absent", "name": name}

    def ensure_project(self, *, project, pipeline, namespace=None) -> str:
        with self._lock:
            new = project not in self.projects
            self.projects.setdefault(project, {"pipeline": pipeline, "namespace": namespace})
        return "created" if new else "exists"

    def ensure_environment(self, *, name, data_plane, production, actor, namespace=None, labels=None) -> str:
        if production and not str(actor).startswith("human:"):
            raise PermissionError("Agents cannot create production environments (VX-3)")
        with self._lock:
            if data_plane not in self.data_planes:
                raise ValueError(f"{data_plane}: no such data plane")
            new = name not in self.product_environments
            self.product_environments.setdefault(name, {"data_plane": data_plane, "production": production,
                                                        "actor": actor, "namespace": namespace,
                                                        "labels": dict(labels or {})})
        return "created" if new else "exists"

    def ensure_pipeline(self, *, name, stages, namespace=None) -> str:
        with self._lock:
            new = name not in self.pipelines
            self.pipelines.setdefault(name, list(stages))
        return "created" if new else "exists"

    def configure_binding(self, *, component, env, configs, traits, project=None, namespace=None) -> dict:
        with self._lock:
            self.binding_configs[(component, env)] = {"configs": configs, "traits": traits}
        return {"component": component, "env": env}

    def create_environment(self, *, name, like, project=None, namespace=None, labels=None) -> dict:
        if self.refuse_environments:
            raise McpError("openchoreo: no tool create_environment")
        from datetime import datetime, timezone

        with self._lock:
            env = self.environments.setdefault(name, {"name": name, "like": like, "project": project,
                                                      "namespace": namespace, "labels": dict(labels or {}),
                                                      "created": datetime.now(timezone.utc).isoformat()})
            return dict(env)

    def delete_environment(self, *, name, project=None, namespace=None) -> None:
        with self._lock:
            self.environments.pop(name, None)
            for key in [k for k in self.env_bindings if k[1] == name]:
                del self.env_bindings[key]
            self.resource_bindings = [b for b in self.resource_bindings if b["env"] != name]

    def list_environments(self, *, namespace=None) -> list[dict]:
        with self._lock:
            return [dict(e) for e in self.environments.values()]

    def bind_latest(self, *, component, env, actor, project=None, namespace=None) -> dict:
        with self._lock:
            if env not in self.environments:
                raise ValueError(f"{env}: no such environment")
            release = f"{component.rsplit('/', 1)[-1]}-r{next(self._seq)}"
            self.env_bindings[(component, env)] = release
        return {"component": component, "env": env, "release": release, "actor": actor, "actor_type": "agent"}

    def unbind(self, *, component, env, project=None, namespace=None) -> None:
        with self._lock:
            self.env_bindings.pop((component, env), None)

    def observe(self, *, component, env, project=None, namespace=None, minutes=60) -> dict:
        return dict(self.observations.get((component, env)) or {"available": True, "logs": [], "metrics": {}})

    def rollback(self, *, component, env, actor, actor_type, project=None, namespace=None) -> dict:
        if actor_type not in ROLLBACK_ACTORS:
            raise PermissionError("Only a person, or the tool under the remediation policy, can roll back production (VX-3)")
        with self._lock:
            rec = {"component": component, "env": env, "from": "r2", "to": "r1", "actor": actor}
            self.rollbacks.append(rec)
        return dict(rec)

    def list_resources(self, *, project, namespace=None) -> list[dict]:
        with self._lock:
            return [{"name": n, "type": r["type"], "parameters": dict(r["parameters"])}
                    for n, r in sorted(self.resources.items()) if r["project"] == project]

    def apply_resource(self, *, name, type, parameters, project, namespace=None) -> dict:
        with self._lock:
            prior = self.resources.get(name)
            if prior and prior["type"] != type:
                raise ValueError(f"{name} is a {prior['type']}; the pipeline never replaces a resource")
            if prior and prior["parameters"] == parameters:
                return {"name": name, "release": f"{name}-r{prior['releases']}", "change": "unchanged"}
            n = (prior or {}).get("releases", 0) + 1
            self.resources[name] = {"type": type, "parameters": dict(parameters), "project": project,
                                    "namespace": namespace, "releases": n}
            return {"name": name, "release": f"{name}-r{n}", "change": "updated" if prior else "created"}

    def bind_resource(self, *, name, env, size, actor, actor_type, project, namespace=None, pipeline=PIPELINE,
                      retain=False) -> dict:
        check_promotion(env, actor_type, pipeline)
        with self._lock:
            r = self.resources.get(name)
            if r is None:
                raise ValueError(f"{name}: no such resource to bind")
            rec = {"name": name, "env": env, "size": size, "release": f"{name}-r{r['releases']}", "actor": actor,
                   "actor_type": actor_type, "retain": retain}
            self.resource_bindings.append(rec)
            return dict(rec)

    def resource_status(self, *, name, env, project, namespace=None) -> dict:
        with self._lock:
            bound = any(b["name"] == name and b["env"] == env for b in self.resource_bindings)
        if not bound:
            return {"state": "absent", "message": ""}
        state = self.resource_results.get((name, env), "ready")
        return {"state": state, "message": "" if state == "ready" else f"{name} is {state} in {env}"}

    def component_bindings(self, *, component, project=None, namespace=None) -> list[dict]:
        with self._lock:
            current = self.deployments.get(component)
            rolled = sum(1 for r in self.rollbacks if r["component"] == component)
        if current not in PIPELINE:
            return []
        return [{"env": env, "release": f"{component.rsplit('/', 1)[-1]}-r{2 - rolled if env == 'production' else 2}",
                 "state": self.deploy_results.get((component, env), "ready"), "binding": f"{component.rsplit('/', 1)[-1]}-{env}"}
                for env in PIPELINE[:PIPELINE.index(current) + 1]]

    def resource_bindings_of(self, *, name, project, namespace=None) -> list[dict]:
        with self._lock:
            latest = {b["env"]: b for b in self.resource_bindings if b["name"] == name}
            outputs = dict(getattr(self, "resource_outputs", {}))
        return [{"env": env, "release": b["release"], "size": b["size"], "binding": f"{name}-{env}",
                 "state": self.resource_results.get((name, env), "ready"), "outputs": list(outputs.get((name, env), []))}
                for env, b in latest.items()]

    def deployment(self, *, component, env, project=None, namespace=None) -> dict:
        with self._lock:
            current = self.deployments.get(component)
            bound = self.env_bindings.get((component, env))
        if bound:  # a change-set environment
            return {"state": self.env_results.get((component, env), "ready"), "release": bound,
                    "url": f"https://{component.rsplit('/', 1)[-1]}.{env}.example.internal"}
        if env not in PIPELINE:
            return {"state": "absent"}
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
                           branch, parameters=None) -> str:
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
            **({("parameters", "component_parameters", "componentParameters"): dict(parameters)} if parameters else {}),
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
        return {"status": status, "message": message, "tasks": tasks,
                "started_at": str(_get(res, "status.startedAt", "startedAt", "status.startTime") or ""),
                "finished_at": str(_get(res, "status.completedAt", "status.finishedAt", "completedAt",
                                        "status.completionTime") or "")}

    # -- deployments
    def _bindings(self, component, project, namespace) -> list[dict]:
        return _list(self._call(("list_release_bindings",), self._scope(component, project, namespace)))

    @staticmethod
    def _env_of(b: dict) -> str:
        return str(_get(b, "environment", "spec.environment", "environmentName") or "")

    @staticmethod
    def _release_of(b: dict) -> str:
        return str(_get(b, "release", "spec.releaseName", "spec.release", "releaseName") or "")

    def ensure_traits(self, *, component, traits, project=None, namespace=None) -> list[str]:
        """Patch the component's traits list with the ones it lacks, keeping every existing trait."""
        from ..ops.alert_rules import missing

        scope = self._scope(component, project, namespace)
        comp = self._call(("get_component",), scope) or {}
        have = list(_get(comp, "spec.traits", "traits") or [])
        add = missing(have, traits)
        if add:
            self._call(("patch_component",), {_NS: scope[_NS], _COMPONENT: scope[_COMPONENT],
                                              ("traits",): have + add})
        return [t["instanceName"] for t in add]

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

    # -- the product's own environments
    def data_plane(self, *, name, namespace=None) -> dict:
        try:
            dp = self._call(("get_data_plane", "get_dataplane"), {**self._ns(namespace),
                                                                   ("name", "data_plane", "dataplane", "data_plane_name"): name})
        except McpError:
            return {"state": "absent", "name": name}
        return {"state": "ready" if dp else "absent", "name": name}

    def ensure_project(self, *, project, pipeline, namespace=None) -> str:
        scope = {**self._ns(namespace), _PROJECT: project}
        try:
            if self._call(("get_project",), scope):
                return "exists"
        except McpError:
            pass
        self._call(("create_project",), {**scope, ("deployment_pipeline", "deploymentPipeline", "pipeline"): pipeline})
        return "created"

    def ensure_environment(self, *, name, data_plane, production, actor, namespace=None, labels=None) -> str:
        if production and not str(actor).startswith("human:"):
            raise PermissionError("Agents cannot create production environments (VX-3)")
        if self._env(name, namespace):
            return "exists"
        self._call(("create_environment",), {
            **self._ns(namespace), ("name", "environment_name", "environmentName"): name,
            ("display_name", "displayName"): name.capitalize(),
            ("data_plane_ref", "dataPlaneRef", "data_plane", "dataPlane"): data_plane,
            ("is_production", "isProduction", "production"): bool(production),
            ("labels",): {"vrixel.io/created-by": str(actor).replace(":", "-")[:63], **(labels or {})}})
        return "created"

    def ensure_pipeline(self, *, name, stages, namespace=None) -> str:
        scope = {**self._ns(namespace), ("name", "pipeline", "deployment_pipeline", "pipeline_name"): name}
        try:
            if self._call(("get_deployment_pipeline",), scope):
                return "exists"
        except McpError:
            pass
        paths = [{"sourceEnvironmentRef": a, "targetEnvironmentRefs": [{"name": b}]} for a, b in zip(stages, stages[1:])]
        self._call(("create_deployment_pipeline",), {**scope, ("promotion_paths", "promotionPaths", "paths"): paths})
        return "created"

    def configure_binding(self, *, component, env, configs, traits, project=None, namespace=None) -> dict:
        scope = self._scope(component, project, namespace)
        b = next((x for x in self._bindings(component, project, namespace) if self._env_of(x) == env), None)
        if b is None:
            raise ValueError(f"{component}: not bound in {env}")
        self._call(("update_release_binding",), {
            **scope, _ENV: env, ("binding", "release_binding", "name", "binding_name"): _get(b, "name", "metadata.name"),
            ("component_type_environment_configs", "componentTypeEnvironmentConfigs"): configs,
            ("trait_environment_configs", "traitEnvironmentConfigs"): traits})
        return {"component": component, "env": env}

    # -- change-set environments
    def create_environment(self, *, name, like, project=None, namespace=None, labels=None) -> dict:
        """An Environment on the same data plane as `like` (dev), marked as a change set's."""
        ns = self._ns(namespace)
        have = self._env(name, namespace)
        if have:
            return {"name": name, "created": str(_get(have, "metadata.creationTimestamp", "createdAt") or "")}
        base = self._env(like, namespace) or {}
        plane = _get(base, "spec.dataPlaneRef.name", "spec.dataPlaneRef", "dataPlaneRef", "data_plane", "dataPlane")
        self._call(("create_environment",), {
            **ns, ("name", "environment_name", "environmentName"): name,
            ("display_name", "displayName"): f"Change set {name}",
            ("data_plane_ref", "dataPlaneRef", "data_plane", "dataPlane"): plane if isinstance(plane, str) else (plane or {}).get("name", ""),
            ("is_production", "isProduction", "production"): False,
            ("labels",): {"vrixel.io/change-set": "true", **(labels or {})}})
        return {"name": name, "created": ""}

    def _env(self, name, namespace) -> dict | None:
        try:
            return self._call(("get_environment",), {**self._ns(namespace), _ENV: name}) or None
        except McpError:
            return None

    def delete_environment(self, *, name, project=None, namespace=None) -> None:
        self._call(("delete_environment",), {**self._ns(namespace), _ENV: name})

    def list_environments(self, *, namespace=None) -> list[dict]:
        out = []
        for e in _list(self._call(("list_environments",), self._ns(namespace))):
            out.append({"name": str(_get(e, "name", "metadata.name") or ""),
                        "created": str(_get(e, "metadata.creationTimestamp", "createdAt", "created") or ""),
                        "labels": dict(_get(e, "metadata.labels", "labels") or {})})
        return out

    def bind_latest(self, *, component, env, actor, project=None, namespace=None) -> dict:
        """Bind the component's newest release (the build of the change set's head) to `env`,
        outside the deployment pipeline: a change-set environment is never promoted from."""
        if env in PIPELINE:
            raise ValueError(f"{env} is a pipeline environment: promote to it instead")
        release = self._latest_release(component, project, namespace)
        scope = self._scope(component, project, namespace)
        existing = next((b for b in self._bindings(component, project, namespace) if self._env_of(b) == env), None)
        if existing is None:
            self._call(("create_release_binding",), {**scope, _ENV: env, _RELEASE: release})
        elif self._release_of(existing) != release:
            self._call(("update_release_binding",), {
                **scope, _ENV: env, ("binding", "release_binding", "name", "binding_name"): _get(existing, "name", "metadata.name"),
                _RELEASE: release, ("release_state", "state"): "Active"})
        return {"component": component, "env": env, "release": release, "actor": actor, "actor_type": "agent"}

    def unbind(self, *, component, env, project=None, namespace=None) -> None:
        b = next((x for x in self._bindings(component, project, namespace) if self._env_of(x) == env), None)
        if b is not None:
            self._call(("delete_release_binding",), {**self._scope(component, project, namespace), _ENV: env,
                                                     ("binding", "release_binding", "name", "binding_name"): _get(b, "name", "metadata.name")})

    def environment(self, component: str) -> str | None:
        with self._lock:
            return self._requested.get(component)

    # -- operations
    def observe(self, *, component, env, project=None, namespace=None, minutes=60) -> dict:
        """Recent logs and metrics from OpenChoreo's observability plane. Servers without these
        tools report available=False; triage then works from the alerts alone."""
        scope = {**self._scope(component, project, namespace), _ENV: env,
                 ("since_minutes", "minutes", "duration_minutes", "window_minutes"): minutes}
        out: dict = {"available": False, "logs": [], "metrics": {}}
        try:
            res = self._call(("get_component_logs", "query_component_logs", "get_logs"),
                             {**scope, ("limit", "max_lines"): 200})
            lines = _list(res) if not isinstance(res, str) else [{"log": x} for x in res.splitlines()]
            out["logs"] = [str(_get(x, "log", "message", "line") or x)[:500] for x in lines][:200]
            out["available"] = True
        except McpError:
            pass
        try:
            res = self._call(("get_component_metrics", "query_component_metrics", "get_metrics"), scope)
            out["metrics"] = res if isinstance(res, dict) else {"raw": str(res)[:2000]}
            out["available"] = True
        except McpError:
            pass
        return out

    def rollback(self, *, component, env, actor, actor_type, project=None, namespace=None) -> dict:
        """Point the environment's release binding at the release before the current one."""
        if actor_type not in ROLLBACK_ACTORS:
            raise PermissionError("Only a person, or the tool under the remediation policy, can roll back production (VX-3)")
        scope = self._scope(component, project, namespace)
        binding = next((b for b in self._bindings(component, project, namespace) if self._env_of(b) == env), None)
        if binding is None:
            raise ValueError(f"{component} has nothing deployed to {env}")
        current = self._release_of(binding)
        names = [str(_get(r, "name", "metadata.name")) for r in _list(self._call(("list_component_releases",), scope))]
        if current not in names or names.index(current) == 0:
            raise ValueError(f"{component}: no release before {current or 'the current one'} to roll back to")
        previous = names[names.index(current) - 1]
        self._call(("update_release_binding",), {
            **scope, _ENV: env, ("binding", "release_binding", "name", "binding_name"): _get(binding, "name", "metadata.name"),
            _RELEASE: previous, ("release_state", "state"): "Active"})
        return {"component": component, "env": env, "from": current, "to": previous, "actor": actor}

    def _console(self, project, component, namespace) -> str:
        if not self.console_url:
            return ""
        return (f"{self.console_url}/catalog/{namespace or self.default_namespace}/component/"
                f"{self.name(component)}")

    # -- managed services (VX-19): OpenChoreo's MCP tools list_resources, get_resource,
    # create_resource, update_resource and *_resource_release_binding(s). Resources are
    # namespace-scoped and owned by a project; a binding pins a release to one environment.
    def _ns(self, namespace) -> dict:
        return {_NS: namespace or self.default_namespace}

    def list_resources(self, *, project, namespace=None) -> list[dict]:
        res = self._call(("list_resources",), {**self._ns(namespace), _PROJECT: project})
        return [{"name": str(_get(r, "name", "metadata.name") or ""),
                 "type": str(_get(r, "spec.type.name", "type.name", "type") or ""),
                 "parameters": dict(_get(r, "spec.parameters", "parameters") or {})} for r in _list(res)]

    def _resource(self, name, namespace) -> dict | None:
        try:
            res = self._call(("get_resource",), {**self._ns(namespace), ("name", "resource_name"): name})
        except McpError:
            return None
        return res if isinstance(res, dict) and res else None

    @staticmethod
    def _latest(res) -> str:
        return str(_get(res, "status.latestRelease.name", "latestRelease.name") or "") if isinstance(res, dict) else ""

    def apply_resource(self, *, name, type, parameters, project, namespace=None) -> dict:
        prior = self._resource(name, namespace)
        if prior:
            was = str(_get(prior, "spec.type.name", "type.name") or "")
            if was and was != type:
                raise ValueError(f"{name} is a {was}; the pipeline never replaces a resource")
            if dict(_get(prior, "spec.parameters", "parameters") or {}) == parameters:
                return {"name": name, "release": self._latest(prior), "change": "unchanged"}
            res = self._call(("update_resource",), {**self._ns(namespace), ("name",): name,
                                                    ("parameters",): parameters})
            return {"name": name, "release": self._latest(res), "change": "updated"}
        res = self._call(("create_resource",), {
            **self._ns(namespace), _PROJECT: project, ("name",): name, ("type_name", "type"): type,
            ("type_kind",): "ClusterResourceType", ("parameters",): parameters})
        return {"name": name, "release": self._latest(res), "change": "created"}

    def _resource_bindings(self, name, namespace) -> list[dict]:
        return _list(self._call(("list_resource_release_bindings",),
                                {**self._ns(namespace), ("resource_name", "name"): name}))

    def bind_resource(self, *, name, env, size, actor, actor_type, project, namespace=None, pipeline=PIPELINE,
                      retain=False) -> dict:
        check_promotion(env, actor_type, pipeline)
        bindings = self._resource_bindings(name, namespace)
        existing = next((b for b in bindings if self._env_of(b) == env), None)
        idx = pipeline.index(env)
        if idx == 0:
            release = self._latest(self._resource(name, namespace))
        else:
            prev = next((b for b in bindings if self._env_of(b) == pipeline[idx - 1]), None)
            release = str(_get(prev, "spec.resourceRelease", "resourceRelease", "resource_release") or "") if prev else ""
        if not release:
            raise ValueError(f"{name}: no release to bind to {env}")
        common = {**self._ns(namespace), ("resource_release",): release,
                  ("resource_type_environment_configs",): {"size": size},
                  ("retain_policy",): "Retain" if retain else "Delete"}
        if existing:
            self._call(("update_resource_release_binding",),
                       {**common, ("name",): str(_get(existing, "name", "metadata.name"))})
        else:
            self._call(("create_resource_release_binding",), {
                **common, ("name",): f"{name}-{env}", ("project_name",): project, ("resource_name",): name,
                ("environment",): env})
        return {"name": name, "env": env, "size": size, "release": release, "actor": actor, "actor_type": actor_type}

    @staticmethod
    def _state_of(b: dict) -> str:
        ready = _condition(b, "Ready")
        if ready == "True":
            return "ready"
        failed = any(isinstance(c, dict) and c.get("status") == "False" and "fail" in str(c.get("reason", "")).lower()
                     for c in _get(b, "status.conditions") or [])
        return "failed" if failed else "progressing"

    def component_bindings(self, *, component, project=None, namespace=None) -> list[dict]:
        return [{"env": self._env_of(b), "release": self._release_of(b), "state": self._state_of(b),
                 "binding": str(_get(b, "name", "metadata.name") or "")}
                for b in self._bindings(component, project, namespace)]

    def resource_bindings_of(self, *, name, project, namespace=None) -> list[dict]:
        out = []
        for b in self._resource_bindings(name, namespace):
            outputs = []
            for o in _get(b, "status.outputs", "outputs") or []:
                if not isinstance(o, dict) or not o.get("name"):
                    continue
                if o.get("value") not in (None, ""):
                    outputs.append({"name": o["name"], "value": str(o["value"])})
                else:  # a secret or config map stays on the data plane: only where it is
                    ref = o.get("secretKeyRef") or o.get("configMapKeyRef") or {}
                    outputs.append({"name": o["name"], "secret": {"name": str(ref.get("name", "")),
                                                                  "key": str(ref.get("key", ""))}})
            out.append({"env": self._env_of(b), "release": str(_get(b, "spec.resourceRelease", "resourceRelease") or ""),
                        "state": self._state_of(b), "binding": str(_get(b, "name", "metadata.name") or ""),
                        "size": str(_get(b, "spec.resourceTypeEnvironmentConfigs.size") or ""), "outputs": outputs})
        return out

    def resource_status(self, *, name, env, project, namespace=None) -> dict:
        b = next((x for x in self._resource_bindings(name, namespace) if self._env_of(x) == env), None)
        if b is None:
            return {"state": "absent", "message": ""}
        if _condition(b, "Ready") == "True":
            return {"state": "ready", "message": ""}
        failed = [c for c in _get(b, "status.conditions") or [] if isinstance(c, dict) and c.get("status") == "False"
                  and "fail" in str(c.get("reason", "")).lower()]
        if failed:
            return {"state": "failed", "message": str(failed[0].get("message", ""))}
        return {"state": "progressing", "message": str(_get(b, "status.message") or "")}

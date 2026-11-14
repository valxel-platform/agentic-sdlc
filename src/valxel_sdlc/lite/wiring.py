"""Which runtime a product deploys through, and the VM runtime for a live lite product.

A full product, and a lite product still hosted while it is built, deploy through OpenChoreo. A
lite product that has gone live deploys through its own `ComposeRuntime` on its VMs (product.yaml
`runtime.vms`). `RuntimeRouter` sits where the OpenChoreo client was and sends each call to the
right one, by the call's project; everything above it is unchanged.

Builds stay on the OpenChoreo build plane in both cases (lite.builds.OpenChoreoBuilds).

The VM runtime's state and the secrets its workloads read are files under VALXEL_LITE_STATE_DIR
(default: <artifact dir>/lite), one pair per product. They move to Valxel Cloud's database, with
the secrets encrypted, before the first outside customer.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Callable
from pathlib import Path

import yaml

from .builds import OpenChoreoBuilds
from .docker import Docker, DockerCli
from .runtime import ComposeRuntime
from .state import FileState


class FileSecrets:
    """A product's secrets for its VM environments: YAML {env: {secret name: {key: value}}}, with an
    optional `"*"` environment for values every environment shares."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def _load(self) -> dict:
        return (yaml.safe_load(self.path.read_text()) or {}) if self.path.exists() else {}

    def get(self, *, namespace, env, name, key) -> str | None:
        data = self._load()
        for scope in (env, "*"):
            value = ((data.get(scope) or {}).get(name) or {}).get(key)
            if value is not None:
                return str(value)
        return None


def state_dir(services) -> Path:
    base = os.environ.get("VALXEL_LITE_STATE_DIR", "")
    if base:
        return Path(base)
    artifacts = getattr(getattr(services, "settings", None), "artifact_dir", None) or "."
    return Path(artifacts) / "lite"


def base_openchoreo(services):
    oc = services.openchoreo
    return getattr(oc, "base", oc)


def nonprod_plane(product_id: str) -> str:
    return f"{product_id.lower()}-nonprod"


def prod_plane(product_id: str) -> str:
    return f"{product_id.lower()}-prod"


def lite_runtime(services, product_id: str, vms: dict, *, runtime: dict | None = None,
                 docker_for: Callable[[str], Docker] | None = None) -> ComposeRuntime:
    """The product's VM runtime: dev and staging on the non-production VM, production on its own VM
    if it has one, else on the same VM."""
    docker_for = docker_for or (lambda host: DockerCli(host))
    nonprod = docker_for(vms["non_production"])
    prod = docker_for(vms["production"]) if vms.get("production") else nonprod
    repo_of = {comp: repo for repo, comp in ((runtime or {}).get("components") or {}).items()}
    folder = state_dir(services)
    pid = product_id.lower()
    return ComposeRuntime(default=nonprod, hosts={nonprod_plane(product_id): nonprod, prod_plane(product_id): prod},
                          builds=OpenChoreoBuilds(base_openchoreo(services), services.github,
                                                  repo_of=lambda c: repo_of.get(c, c)),
                          secrets=FileSecrets(folder / f"{pid}-secrets.yaml"), state=FileState(folder / f"{pid}.json"))


def live_lite(runtime: dict | None) -> bool:
    rt = runtime or {}
    return rt.get("mode") == "lite" and rt.get("phase") == "live" and bool((rt.get("vms") or {}).get("non_production"))


# Calls that name no project: what in their arguments says which product they are for.
def _project_of_plane(name: str) -> str:
    return name.rsplit("-", 1)[0] if name.endswith(("-nonprod", "-prod", "-pipeline")) else ""


class RuntimeRouter:
    """The OpenChoreo interface, routed per product: live lite products to their VM runtime,
    everything else to OpenChoreo (`base`)."""

    def __init__(self, base, services, *, docker_for: Callable[[str], Docker] | None = None) -> None:
        self.base = base
        self.services = services
        self.docker_for = docker_for
        self._runtimes: dict[str, tuple[tuple, ComposeRuntime]] = {}
        self._project_of_component: dict[str, str] = {}
        self._lock = threading.Lock()

    def _product(self, project: str):
        """The registered product whose OpenChoreo project this is, and its runtime block."""
        reg = getattr(self.services.settings, "registry", {}) or {}
        for pid in reg:
            if pid.lower() == project:
                try:
                    return pid, self.services.snapshot(pid).product.runtime or {}
                except Exception:
                    return pid, {}
        return None, {}

    def runtime_for(self, project: str | None):
        """The VM runtime when `project` is a live lite product, else None."""
        if not project:
            return None
        pid, rt = self._product(str(project).lower())
        if not pid or not live_lite(rt):
            return None
        key = tuple(sorted(rt["vms"].items()))
        with self._lock:
            known = self._runtimes.get(pid)
            if known is None or known[0] != key:
                known = (key, lite_runtime(self.services, pid, rt["vms"], runtime=rt, docker_for=self.docker_for))
                self._runtimes[pid] = known
            return known[1]

    def _target(self, kwargs: dict, args: tuple):
        project = kwargs.get("project")
        component = kwargs.get("component") or (args[0] if args else None)
        if project and component:
            with self._lock:
                self._project_of_component[component] = project
        if not project and component:
            with self._lock:
                project = self._project_of_component.get(component)
        if not project:
            for k in ("data_plane", "name", "pipeline"):
                if isinstance(kwargs.get(k), str):
                    project = _project_of_plane(kwargs[k]) or project
        return self.runtime_for(project) or self.base

    def __getattr__(self, name: str):
        attr = getattr(self.base, name)
        if not callable(attr):
            return attr

        def call(*args, **kwargs):
            return getattr(self._target(kwargs, args), name)(*args, **kwargs)
        return call

    def list_environments(self, *, namespace=None) -> list[dict]:
        out = list(self.base.list_environments(namespace=namespace))
        with self._lock:
            runtimes = [rt for _, rt in self._runtimes.values()]
        for rt in runtimes:
            out += rt.list_environments(namespace=namespace)
        return out

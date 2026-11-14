"""Go-live: a lite product moves from Valxel's hosting to the customer's VM.

While it is built, a lite product runs on Valxel's shared OpenChoreo plane, with dev and staging
only (valxel_sdlc.environments). When the customer decides to go to production, a person runs
go-live with the VM (or two) the customer has given Valxel access to:

    releases   what runs in hosted staging: each component's image pinned by digest, and its
               workload.yaml from the repo's default branch
    secrets    every secret those workloads read has a value for dev, staging and production
               (nothing is deployed until all do)
    vm         the VM runtime's project, pipeline and environments (production for the person
               running go-live, VX-3)
    deploy     the same releases to dev, then staging, then production on the VM: nothing is rebuilt
    manifest   a pull request to the governance repo: runtime.phase live, the VMs, the date. Once
               merged, the product deploys through its VM runtime (lite.wiring.RuntimeRouter) and
               any feature waiting at G6 for go-live moves on.

`retire_hosted` then removes the product's hosted dev and staging from the shared plane.
Each step stops go-live if it fails; running it again is safe.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

import yaml

from ..environments import NON_PRODUCTION, STAGES, hosted, names
from ..integrations.openchoreo import PIPELINE
from .docker import Docker
from .wiring import base_openchoreo, lite_runtime, nonprod_plane, prod_plane
from .workload import secret_refs

WORKLOAD_FILE = "workload.yaml"


class GoLiveError(RuntimeError):
    pass


def _components(snap) -> list[tuple[str, str]]:
    """(repo, OpenChoreo component) for each of the product's deployable repos."""
    rt = snap.product.runtime or {}
    mapped = rt.get("components") or {}
    return [(r.name, mapped.get(r.name, r.name)) for r in snap.product.repos]


def _manifest_text(text: str, vms: dict, today: str) -> str:
    """product.yaml with the product live on its VMs. Edits the runtime block in place so the
    file's comments and order survive."""
    lines = text.splitlines()
    out, in_runtime, done = [], False, False
    vm_lines = ["  vms:"] + [f"    {k}: {v}" for k, v in sorted(vms.items()) if v] + [f"  went_live: '{today}'"]
    for line in lines:
        if line.startswith("runtime:"):
            in_runtime = True
            out.append(line)
            continue
        if in_runtime and line and not line.startswith(" "):
            if not done:
                out += ["  phase: live", *vm_lines]
                done = True
            in_runtime = False
        if in_runtime and line.strip().startswith(("phase:", "went_live:")):
            continue
        if in_runtime and line.strip().startswith("vms:"):
            in_runtime = "skip-vms"
            continue
        if in_runtime == "skip-vms":
            if line.startswith("    "):
                continue
            in_runtime = True
        out.append(line)
    if in_runtime and not done:
        out += ["  phase: live", *vm_lines]
    return "\n".join(out) + "\n"


def go_live(services, *, product_id: str, approver: str, vms: dict,
            docker_for: Callable[[str], Docker] | None = None, today: str = "") -> dict:
    """Move a hosted lite product to its VMs. `vms`: {"non_production": "ssh://valxel@host",
    "production": optional, its own VM}. Returns {"steps", "ok"}."""
    steps: list[dict] = []
    today = today or datetime.now(UTC).date().isoformat()

    def step(name: str, fn):
        try:
            got = fn() or {}
            steps.append({"step": name, "ok": True, **{k: v for k, v in got.items() if k != "_"}})
            return got
        except Exception as exc:
            steps.append({"step": name, "ok": False, "error": f"{type(exc).__name__}: {exc}"})
            raise GoLiveError(name) from exc

    snap = services.snapshot(product_id)
    rt = snap.product.runtime or {}
    who = approver.split(",")[0].strip()
    try:
        if not who or who.startswith("agent:"):
            raise PermissionError("Go-live puts the product in production: a person runs it (VX-3)")
        if not hosted(rt):
            raise ValueError(f"{product_id} isn't a lite product hosted while it's built "
                             f"(mode {rt.get('mode') or 'full'}, phase {rt.get('phase')})")
        if not (vms or {}).get("non_production"):
            raise ValueError("go-live needs the non-production VM: vms.non_production")
    except Exception as exc:
        return {"ok": False, "steps": [{"step": "check", "ok": False, "error": f"{type(exc).__name__}: {exc}"}]}

    oc = base_openchoreo(services)
    project, namespace = rt.get("openchoreo_project"), rt.get("namespace")
    staging = PIPELINE[1] if len(PIPELINE) > 2 else NON_PRODUCTION[-1]
    vm = lite_runtime(services, product_id, vms, runtime=rt, docker_for=docker_for)

    def releases():
        found = []
        for repo, comp in _components(snap):
            bound = next((b for b in oc.component_bindings(component=comp, project=project, namespace=namespace)
                          if b.get("env") == staging), None)
            if not bound or not bound.get("release"):
                continue  # not deployed yet: nothing to move
            rel = oc.release_image(component=comp, project=project, namespace=namespace, release=bound["release"])
            text = services.github.get_file(repo=repo, path=WORKLOAD_FILE, ref="")
            if text is None:
                raise GoLiveError(f"{repo} has no {WORKLOAD_FILE} on its default branch")
            found.append({"repo": repo, "component": comp, "release": rel["release"], "image": rel["image"],
                          "workload": yaml.safe_load(text) or {}})
        if not found:
            raise GoLiveError(f"nothing runs in {product_id}'s hosted {staging}: deploy a feature there first")
        return {"components": [f["component"] for f in found], "_": found}

    def secrets(found):
        missing = sorted({f"{env}: {name}/{key}" for f in found for name, key in secret_refs(f["workload"])
                          for env in STAGES if vm.secrets.get(namespace=project, env=env, name=name, key=key) is None})
        if missing:
            raise GoLiveError("no value for " + ", ".join(missing))
        return {"checked": sum(len(secret_refs(f["workload"])) for f in found)}

    def environments():
        n = names(product_id)
        done = {"project": vm.ensure_project(project=project, pipeline=n["pipeline"]),
                "pipeline": vm.ensure_pipeline(name=n["pipeline"], stages=list(STAGES))}
        labels = {"valxel.io/product": product_id.lower()}
        for env in NON_PRODUCTION:
            done[env] = vm.ensure_environment(name=env, data_plane=nonprod_plane(product_id), production=False,
                                              actor="agent:go-live", labels=labels)
        done["production"] = vm.ensure_environment(name="production", data_plane=prod_plane(product_id),
                                                   production=True, actor=f"human:{who}", labels=labels)
        return {"created": done}

    def deploy(found):
        for f in found:
            vm.register_component(component=f["component"], project=project, namespace=None, component_type="",
                                  workflow=None, repo_url=f"https://github.com/{f['repo']}", branch="main")
            vm.import_release(component=f["component"], image=f["image"], workload=f["workload"], source=f["release"])
        for env in STAGES:
            human = env == STAGES[-1]
            for f in found:
                vm.promote(component=f["component"], env=env, actor=who if human else "agent:go-live",
                           actor_type="human" if human else "agent", project=project)
        states = {f"{f['component']}@{env}": vm.deployment(component=f["component"], env=env, project=project)["state"]
                  for f in found for env in STAGES}
        return {"states": states}

    def manifest():
        gov = snap.pins["product"]
        repo = f"{gov['org']}/{gov['repo']}"
        current = services.github.get_file(repo=repo, path="product.yaml", ref="") or ""
        branch = f"valxel/go-live-{product_id.lower()}"
        services.github.put_file(repo=repo, branch=branch, path="product.yaml",
                                 content=_manifest_text(current, vms, today),
                                 message=f"{product_id} goes live on its VMs")
        pr = services.github.open_pr(
            repo=repo, branch=branch, title=f"{product_id} goes live", files=["product.yaml"], labels=["valxel", "go-live"],
            body=(f"{who} moved {product_id} from Valxel's hosting to its VMs on {today}: dev, staging and production run "
                  "the releases that ran in hosted staging. Merge to deploy through the VMs from now on; features "
                  "waiting at G6 for go-live then ask for approval. Afterwards, retire the hosted environments."))
        return {"url": pr["url"], "repo": repo}

    try:
        found = step("releases", releases)["_"]
        step("secrets", lambda: secrets(found))
        step("vm", environments)
        step("deploy", lambda: deploy(found))
        step("manifest_pr", manifest)
    except GoLiveError:
        return {"ok": False, "steps": steps}
    return {"ok": True, "steps": steps}


def retire_hosted(services, *, product_id: str) -> dict:
    """Remove a live lite product's components from its hosted dev and staging."""
    snap = services.snapshot(product_id)
    rt = snap.product.runtime or {}
    if rt.get("mode") != "lite" or rt.get("phase") != "live":
        raise ValueError(f"{product_id} isn't live yet: its hosted environments are still in use")
    oc = base_openchoreo(services)
    project, namespace = rt.get("openchoreo_project"), rt.get("namespace")
    removed = []
    for _, comp in _components(snap):
        for env in NON_PRODUCTION:
            oc.unbind(component=comp, env=env, project=project, namespace=namespace)
            removed.append(f"{comp}@{env}")
    return {"removed": removed}

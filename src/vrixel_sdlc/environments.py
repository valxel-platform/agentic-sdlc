"""A product's own environments, set up at its product gate, and the capacity each deployment runs with.

    one OpenChoreo control plane, workflow plane and non-production observability plane, shared
    each product:  <pid>-nonprod   data plane (cluster) for dev, staging and change-set environments
                   <pid>-prod      data plane (cluster) for production
                   <pid>-observe   production observability plane: production logs never leave the product

    product gate approved
      -> clusters: a pull request to the platform's infrastructure repo (VRIXEL_INFRA_REPO) adding the
         product's two data-plane clusters and its production observability plane, from the platform's
         templates (platform/openchoreo/clusters.yaml); the platform's GitOps creates them and registers
         them with the control plane
      -> environments: once the data planes are registered, the product's project, its dev and staging
         environments on <pid>-nonprod, production on <pid>-prod (created for the person who approved
         the gate, VX-3), and the deployment pipeline dev -> staging -> production. Until then the step
         waits; `vrixel environments --product <PID>` (or the setup API) finishes it.
    every deployment
      -> the release binding gets that environment's capacity from runtime/capacity.yaml
         (vrixel_sdlc.capacity.binding_configs): replicas, CPU, memory, and the autoscaler's range

Every step is safe to repeat.
"""

from __future__ import annotations

import os

import yaml

STAGES = ("dev", "staging", "production")
NON_PRODUCTION = ("dev", "staging")
AUTOSCALER = "autoscaler"  # the platform's ClusterTrait (platform/openchoreo/autoscaler.yaml)
# Non-production runs on spot nodes and scales to zero outside working hours (the platform's
# scheduler reads `awake`); production never does.
AWAKE = "Mon-Fri 07:00-19:00 America/Chicago"


def names(product_id: str) -> dict:
    pid = product_id.lower()
    return {"project": pid, "non_production": f"{pid}-nonprod", "production": f"{pid}-prod",
            "observability": f"{pid}-observe", "pipeline": f"{pid}-pipeline"}


def runtime_entry(product_id: str, tier: int) -> dict:
    """What product.yaml's runtime says about where the product runs."""
    n = names(product_id)
    return {"tier": tier, "stage": "pilot",
            "data_planes": {"non_production": n["non_production"], "production": n["production"]},
            "observability": {"non_production": "shared", "production": n["observability"]}}


def cluster_files(product_id: str, tier: int, template: str, zones: int, awake: str = AWAKE) -> dict[str, str]:
    """The product's clusters for the infrastructure repo, from the platform's template (a YAML
    document with {product}, {name}, {kind}, {zones} and {tier} placeholders, one per cluster kind)."""
    n = names(product_id)
    out = {}
    for kind, name, z, spot, awake in (("data-plane", n["non_production"], 1, "true", awake),
                                       ("data-plane", n["production"], zones, "false", ""),
                                       ("observability-plane", n["observability"], min(zones, 2), "false", "")):
        text = template.format(product=product_id.lower(), name=name, kind=kind, zones=z, tier=tier, spot=spot,
                               awake=awake)
        out[f"clusters/{product_id.lower()}/{name}.yaml"] = text
    return out


DEFAULT_TEMPLATE = """# {name}: the {kind} cluster of product {product} (tier {tier}), created by the platform's
# GitOps from this file and registered with the shared OpenChoreo control plane.
apiVersion: platform.vrixel.io/v1alpha1
kind: ProductCluster
metadata:
  name: {name}
  labels: {{ vrixel.io/product: "{product}", vrixel.io/plane: "{kind}" }}
spec:
  plane: {kind}
  zones: {zones}
  nodes: {{ min: 1, max: 10, architecture: arm64, spot: {spot} }}
  awake: "{awake}"
"""


def setup(services, *, product_id: str, tier: int, approver: str, infra_repo: str | None = None,
          template: str = "", zones: int = 2, thread_id: str = "", awake: str = AWAKE) -> list[dict]:
    """The product's clusters (a pull request) and its OpenChoreo project, environments and
    pipeline (when its data planes are registered). Returns the steps, like inception setup."""
    steps: list[dict] = []
    n = names(product_id)
    gh, oc = services.github, services.openchoreo
    infra_repo = infra_repo if infra_repo is not None else os.environ.get("VRIXEL_INFRA_REPO", "")

    def step(name: str, fn) -> None:
        try:
            steps.append({"step": name, "ok": True, **(fn() or {})})
        except Exception as exc:  # recorded; the rest still runs
            steps.append({"step": name, "ok": False, "error": f"{type(exc).__name__}: {exc}"})

    def clusters() -> dict:
        files = cluster_files(product_id, tier, template or DEFAULT_TEMPLATE, zones, awake)
        if not infra_repo:
            return {"skipped": True, "files": sorted(files),
                    "why": "VRIXEL_INFRA_REPO isn't set: give the platform team these cluster definitions"}
        branch = f"vrixel/clusters-{product_id.lower()}"
        for path, text in files.items():
            gh.put_file(repo=infra_repo, branch=branch, path=path, content=text,
                        message=f"{thread_id or product_id}: {path}")
        pr = gh.open_pr(repo=infra_repo, branch=branch, title=f"Clusters for {product_id}", files=sorted(files),
                        labels=["vrixel", "inception"],
                        body=(f"The data-plane clusters and production observability plane for {product_id} (tier "
                              f"{tier}), approved at its product gate by {approver}. Once they're registered with "
                              "the control plane, the tool creates the product's environments."))
        return {"url": pr["url"], "repo": infra_repo, "files": sorted(files)}

    step("clusters_pr", clusters)
    step("environments", lambda: environments(services, product_id=product_id, approver=approver))
    return steps


def environments(services, *, product_id: str, approver: str) -> dict:
    """The project, environments and pipeline, once both data planes are registered."""
    n = names(product_id)
    oc = services.openchoreo
    if not callable(getattr(oc, "ensure_environment", None)):
        return {"skipped": True, "why": "this OpenChoreo client can't create environments"}
    waiting = [dp for dp in (n["non_production"], n["production"]) if oc.data_plane(name=dp).get("state") != "ready"]
    if waiting:
        return {"waiting": waiting,
                "why": f"waiting for the data planes {', '.join(waiting)}; run `vrixel environments --product "
                       f"{product_id}` once the platform has registered them"}
    done = {"project": oc.ensure_project(project=n["project"], pipeline=n["pipeline"]),
            "pipeline": oc.ensure_pipeline(name=n["pipeline"], stages=list(STAGES))}
    labels = {"vrixel.io/product": product_id.lower()}
    for env in NON_PRODUCTION:
        done[env] = oc.ensure_environment(name=env, data_plane=n["non_production"], production=False,
                                          actor="agent:setup", labels=labels)
    who = approver.split(",")[0].strip() or "unknown"
    done["production"] = oc.ensure_environment(name="production", data_plane=n["production"], production=True,
                                               actor=f"human:{who}", labels=labels)
    return {"created": done, "approved_by": who}


# ------------------------------------------------------------------------------ on deployment

def apply_capacity(services, snap, target: dict, env: str) -> dict | None:
    """Give a component's binding in `env` its capacity. A failure never stops a deployment; the
    next deployment tries again."""
    from .capacity import binding_configs

    cap = getattr(snap, "capacity", None) or {}
    configure = getattr(services.openchoreo, "configure_binding", None)
    if not cap or not callable(configure):
        return None
    got = binding_configs(cap, target["component"], env)
    if not got:
        return None
    configs, traits = got
    short = target["component"].rsplit("/", 1)[-1]
    try:
        if traits:
            ensure = getattr(services.openchoreo, "ensure_traits", None)
            if callable(ensure):
                ensure(component=target["component"], project=target.get("project"), namespace=target.get("namespace"),
                       traits=[{"name": AUTOSCALER, "kind": "ClusterTrait", "instanceName": f"{short}-{AUTOSCALER}",
                                "parameters": {}}])
            traits = {f"{short}-{AUTOSCALER}": traits[AUTOSCALER]}
        configure(component=target["component"], env=env, configs=configs, traits=traits,
                  project=target.get("project"), namespace=target.get("namespace"))
    except Exception as exc:
        return {"component": target["component"], "env": env, "error": f"{type(exc).__name__}: {exc}"}
    return {"component": target["component"], "env": env, "replicas": configs["replicas"],
            "autoscaler": traits or None}


def load(text: str) -> dict:
    raw = yaml.safe_load(text or "") or {}
    return raw if isinstance(raw, dict) else {}

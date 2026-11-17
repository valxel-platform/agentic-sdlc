"""OpenChoreo targets, capacity, AWS resources and alarms for a feature's components."""

from __future__ import annotations


from ..integrations.openchoreo import PIPELINE
from ..state import FeatureState


def oc_target(lane, repo: str) -> dict:
    """Where a repo builds and runs in OpenChoreo, from the owning product's runtime block."""
    rt = (lane.runtime if lane is not None else None) or {}
    return {"component": (rt.get("components") or {}).get(repo, repo), "project": rt.get("openchoreo_project"),
            "namespace": rt.get("namespace"), "pipeline": tuple(rt.get("pipeline") or PIPELINE)}


def _resource_owner(snap):
    """component short name -> (project, namespace): the component's lane runtime, or the product's
    for shared lanes (they run inside the product)."""
    lanes = snap.lane_of_repo()
    product_rt = snap.product.runtime or {}

    def target(component: str):
        repo = next((r for r in lanes if r.split("/", 1)[-1] == component), None)
        rt = ((lanes[repo].runtime if repo else None) or product_rt)
        return rt.get("openchoreo_project"), rt.get("namespace")

    return target


def _staging_targets(snap) -> dict[str, float]:
    """Requests or events per second per component for staging's load test: its pods at the plan's headroom."""
    cap = snap.capacity or {}
    util = float((cap.get("headroom") or {}).get("utilization", 0.6))
    out = {}
    for repo, c in (cap.get("components") or {}).items():
        pods = int((c.get("staging") or {}).get("min") or 0)
        per_pod = float(c.get("measured_per_pod") or c.get("per_pod") or 0)
        ratio = float((c.get("staging") or {}).get("cpu") or 1) / float(((c.get("production") or {}).get("growth") or {}).get("cpu") or 1)
        if pods and per_pod:
            out[repo] = round(pods * per_pod * ratio * util, 1)
    return out


def _capacity_change(services, state: FeatureState, snap) -> list[dict]:
    """The approved maximums the design's added load raises (empty without a capacity plan)."""
    from ..capacity import change

    ref = (state.get("artifacts") or {}).get("HLD")
    cap = getattr(snap, "capacity", None) or {}
    if not ref or not cap:
        return []
    try:
        return change(cap, services.artifacts.load(ref))
    except Exception:
        return []


def _apply_capacity(services, snap, target: dict, env: str, *, repo: str = "", role: str = "",
                    ref: str = "") -> dict | None:
    """The environment's replicas, CPU, memory and autoscaling from the product's approved capacity
    plan (runtime/capacity.yaml); for a job, its schedule, time zone and time limit from job.yaml at
    the commit being deployed (jobs.py); for a connector, the webhook paths its route exposes and the
    hosts its egress allows, from integration.yaml at that commit (connectors.py). The last
    environment in the pipeline is production."""
    from .. import channels, connectors, jobs
    from ..environments import apply_capacity

    pipeline = list(target.get("pipeline") or [])
    name = "production" if pipeline and env == pipeline[-1] else env
    extra: dict = {}
    if repo and role == jobs.ROLE:
        extra = jobs.read_configs(services.github, repo, ref, name)
    elif repo and role == connectors.ROLE:
        extra = connectors.read_configs(services.github, repo, ref)
    elif repo and role == channels.ROLE:
        extra = channels.read_configs(services.github, repo, ref)
    try:
        return apply_capacity(services, snap, target, name, extra=extra)
    except Exception:
        return None


def _ensure_alert_rules(services, snap, target: dict, role: str = "") -> list[str]:
    """Give a component the rules it lacks. A failure here never stops a deployment; the next
    deployment tries again."""
    from ..ops.alert_rules import traits_for

    traits = traits_for(getattr(snap, "alert_rules", None) or {}, target["component"].rsplit("/", 1)[-1], role=role)
    ensure = getattr(services.openchoreo, "ensure_traits", None)
    if not traits or not callable(ensure):
        return []
    try:
        return ensure(component=target["component"], traits=traits, project=target["project"],
                      namespace=target["namespace"])
    except Exception:
        return []


def _alarms_firing(services, state: FeatureState) -> list[dict]:
    from ..ops.alarms import for_feature

    try:
        return for_feature(getattr(services, "alarms", None), state)
    except Exception:
        return []


def _build_cost(services, state: FeatureState) -> dict | None:
    from ..finops.views import build_so_far

    try:
        return build_so_far(getattr(services, "cost_ledger", None), state)
    except Exception:
        return None


def aws_summary(services, state: FeatureState, snap, ref) -> dict | None:
    """G4 and G6 see the AWS services, each component's access and the cost per environment (VX-19)."""
    from .. import resources

    if not ref or not snap.aws_catalog:
        return None
    rt = snap.product.runtime or {}
    return resources.summary(services.artifacts.load(ref) if isinstance(ref, dict) else "",
                             snap.aws_catalog, list(rt.get("pipeline") or PIPELINE), rt.get("aws"))


def aws_plan(services, state: FeatureState, snap, ref) -> list[dict]:
    from .. import resources

    if not ref or not snap.aws_catalog:
        return []
    return resources.plan(services.artifacts.load(ref), snap.aws_catalog, _resource_owner(snap),
                          product_id=state["product_id"], feature_id=state["feature_id"],
                          data_class=snap.product.data_class)


def bind_resource(services, state: FeatureState, res: dict, stage: str, *, actor: str, actor_type: str) -> dict:
    """Create or update an approved Resource and bind it to the stage's environment with its size."""
    from ..resources import size_for

    snap = services.feature_snapshot(state)
    pipeline = tuple((snap.product.runtime or {}).get("pipeline") or PIPELINE)
    env = env_for(stage, pipeline)
    spec = res.get("spec") or res
    applied = services.openchoreo.apply_resource(name=spec["name"], type=spec["type"], parameters=spec["parameters"],
                                                 project=spec["project"], namespace=spec.get("namespace"))
    t = (snap.aws_catalog.get("types") or {}).get(spec["type"]) or {}
    size = (size_for({"type": spec["type"], "sizes": spec.get("sizes") or {}}, env, snap.aws_catalog)
            if t.get("sizes") else "small")
    rec = services.openchoreo.bind_resource(name=spec["name"], env=env, size=size, actor=actor, actor_type=actor_type,
                                            project=spec["project"], namespace=spec.get("namespace"), pipeline=pipeline,
                                            # Stored data outlives its binding: OpenChoreo keeps it (VX-19).
                                            retain=bool(spec.get("stateful")))
    return {"repo": f"resource:{spec['name']}", "kind": "resource", "component": spec["name"], "project": spec["project"],
            "namespace": spec.get("namespace"), "pipeline": list(pipeline), "env": env, "size": size,
            "release": rec.get("release") or applied.get("release", ""), "change": applied.get("change", ""),
            "spec": spec, "bound_by": actor}


def env_for(stage: str, pipeline: tuple[str, ...]) -> str | None:
    """dev is the first environment, production the last, staging the one in between (if any)."""
    if stage == "dev":
        return pipeline[0]
    if stage == "production":
        return pipeline[-1]
    return pipeline[1] if len(pipeline) > 2 else None

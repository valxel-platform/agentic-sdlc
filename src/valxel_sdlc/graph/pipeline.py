"""The feature pipeline: supervisor routing, eight phase subgraphs, six human gates.

discovery -> G1 -> spec -> G2 -> (design -> G3 | architecture -> G4) -> join -> plan -> build
  -> ci_wait -> [G5 if high-risk] -> merge_train -> build_wait -> deploy_dev -> dev_wait
  -> deploy_staging -> staging_wait -> release -> G6 (promotes production) -> production_wait
  -> rollout (store apps only) -> operate -> finalize

Gates and escalations are `interrupt()` nodes; a durable checkpointer keeps each thread
waiting for as long as a human needs. Any phase escalates after its loop cap or budget.
The *_wait nodes pause the same way on GitHub CI and OpenChoreo (see valxel_sdlc.waits); when
one fails, the release managers decide whether to retry, carry on, or stop.
"""

from __future__ import annotations

from langgraph.graph import END, START, StateGraph

from ..state import GATES, PHASES, FeatureState
from .phase import make_phase_node
from .briefs import make_hotfix_brief, make_intake, make_profile_brief
from .delivery import make_build_wait, make_cs_build_wait, make_cs_deploy, make_cs_env, make_cs_wait, make_deploy, make_env_wait
from .gates import join, make_escalation, make_gate
from .merge_train import make_ci_wait, make_merge_train
from .provision import make_provision, make_provision_wait
from .rollout import finalize, first_step, make_flag_rollout, make_rollout
from .prototype_steps import (
    make_architecture_done,
    make_dependency_wait,
    make_design_prototype,
    make_discovery_sketch,
)
from .routing import _route, _route_targets
# Kept importable from here: other modules and tests import these from graph.pipeline.
from .briefs import hotfix_approvers  # noqa: F401
from .gates import _hld_adrs, agent_evals, release_traceability  # noqa: F401
from .merge_train import failing_repos  # noqa: F401
from .provision import repo_plans  # noqa: F401
from .routing import ESCALATION_DECISIONS, InvalidDecision, normalize  # noqa: F401


def build_pipeline(services, checkpointer=None):
    g = StateGraph(FeatureState)
    g.add_node("intake", make_intake(services))
    for phase in PHASES:
        g.add_node(phase, make_phase_node(phase, services))
        esc, esc_dest = make_escalation(phase, services)
        g.add_node(f"escalate_{phase}", esc, destinations=esc_dest)
    for gate in GATES:
        node, dest = make_gate(gate, services)
        g.add_node(gate, node, destinations=dest)
    g.add_node("join", join, destinations=("fit", END))
    # Between the UX spec and G3: today's screens, then the prototype on top of them; and a step
    # after architecture so G4 opens in the same round as G3 (graph.prototype_steps).
    node, dest = make_design_prototype(services)
    g.add_node("design_prototype", node, destinations=dest)
    node, dest = make_architecture_done(services)
    g.add_node("architecture_done", node, destinations=dest)
    node, dest = make_discovery_sketch(services)  # a rough sketch beside the brief at G1 (sketch.py)
    g.add_node("discovery_sketch", node, destinations=dest)
    from ..fit import make_fit, make_fit_gate

    node, dest = make_fit(services)
    g.add_node("fit", node, destinations=dest)
    node, dest = make_fit_gate(services)
    g.add_node("fit_gate", node, destinations=dest)
    node, dest = make_provision(services)
    g.add_node("provision", node, destinations=dest)
    node, dest = make_provision_wait(services)
    g.add_node("provision_wait", node, destinations=dest)
    g.add_node("merge_train", make_merge_train(services))
    node, dest = make_ci_wait(services)
    g.add_node("ci_wait", node, destinations=dest)
    node, dest = make_build_wait(services)
    g.add_node("build_wait", node, destinations=dest)
    for name, make in (("cs_env", make_cs_env), ("cs_build_wait", make_cs_build_wait), ("cs_deploy", make_cs_deploy),
                       ("cs_wait", make_cs_wait)):
        node, dest = make(services)
        g.add_node(name, node, destinations=dest)
    node, dest = make_dependency_wait(services)  # before release: what it depends on is live (T-107)
    g.add_node("dependency_wait", node, destinations=dest)
    for stage, after in (("dev", "deploy_staging"), ("staging", "dependency_wait")):
        node, dest = make_deploy(stage, services, after)
        g.add_node(f"deploy_{stage}", node, destinations=dest)
        node, dest = make_env_wait(services, stage, ok=after, retry=f"deploy_{stage}")
        g.add_node(f"{stage}_wait", node, destinations=dest)
    node, dest = make_env_wait(services, "production", ok="rollout", retry="g6")
    g.add_node("production_wait", node, destinations=dest)
    node, dest = make_rollout(services)
    g.add_node("rollout", node, destinations=dest)
    node, dest = make_flag_rollout(services)
    g.add_node("flag_rollout", node, destinations=dest)
    g.add_node("finalize", finalize)

    g.add_node("hotfix_brief", make_hotfix_brief(services))
    g.add_node("profile_brief", make_profile_brief(services))
    g.add_edge(START, "intake")
    g.add_conditional_edges("intake", first_step, ["hotfix_brief", "profile_brief", "discovery"])
    g.add_edge("hotfix_brief", "plan")
    g.add_edge("profile_brief", "spec")
    for phase in PHASES:
        g.add_conditional_edges(phase, _route(phase), _route_targets(phase))
    g.add_edge("merge_train", "build_wait")
    g.add_edge("finalize", END)
    return g.compile(checkpointer=checkpointer, name="valxel_feature")

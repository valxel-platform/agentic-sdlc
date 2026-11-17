"""Creating the repositories an approved design asks for."""

from __future__ import annotations

from langgraph.graph import END
from langgraph.types import Command, interrupt

from ..agents.parse import parse_new_repos
from ..state import FeatureState
from .phase import event, now
from .routing import _record, normalize


PROVISION_DECISIONS = ("retry", "resolved", "abort")


def proposed_repos(services, state: FeatureState) -> list[dict]:
    ref = (state.get("artifacts") or {}).get("HLD")
    return parse_new_repos(services.artifacts.load(ref)) if ref else []


def repo_plans(services, state: FeatureState) -> list[dict]:
    """What G4 approves and the provision step applies, resolved from the golden path."""
    import os

    from ..provisioning import plan

    product = services.snapshot(state["product_id"], state.get("governance")).product
    out = []
    for p in proposed_repos(services, state):
        rp = plan(product, p, product.repo_standards or {"defaults": {}, "roles": {}})
        if os.environ.get("VALXEL_GITHUB_APP_ID"):
            rp["ruleset"]["bypass_app_id"] = os.environ["VALXEL_GITHUB_APP_ID"]
        out.append(rp)
    return out


def make_provision(services):
    """Create the repos G4 approved, on the golden path, before planning assigns work to them."""

    def provision(state: FeatureState) -> Command:
        plans = repo_plans(services, state)
        if not plans:
            return Command(goto="plan")
        snap = services.snapshot(state["product_id"], state.get("governance"))
        entry = services.settings.entry(state["product_id"])
        gov = entry.governance
        src = services.governance.source
        manifest = src.read(gov.org, gov.repo, "product.yaml", src.resolve(gov.org, gov.repo, gov.ref)) or ""
        done = {r["repo"]: r for r in state.get("new_repos") or [] if r.get("status") == "provisioned"}
        # With the provisioning worker (security S4), each person's retry is a new request.
        extra = {"attempt": sum(1 for g in state.get("gate_decisions") or [] if g.get("gate") == "escalation:provisioning"
                                and g.get("decision") == "retry")} if getattr(services.provisioner, "queued", False) else {}
        results = [done[p["repo"]] if p["repo"] in done else
                   services.provisioner.provision(snap.product, p, feature_id=state["feature_id"],
                                                  governance_repo=gov.full_name, manifest_text=manifest, **extra)
                   for p in plans]
        if any(r["status"] == "queued" for r in results):  # the provisioning worker has them (security S4)
            return Command(goto="provision_wait", update={"new_repos": results})
        events = [event("provision", "repo_provisioned", r["repo"], url=r["url"], governance_pr=r["governance_pr"])
                  for r in results if r["status"] == "provisioned" and r["repo"] not in done]
        failed = [r for r in results if r["status"] != "provisioned"]
        if not failed:
            return Command(goto="plan", update={"new_repos": results, "events": events})

        reason = "; ".join(f"{r['repo']}: " + next((f"{s['step']} failed ({s['error']})" for s in r["steps"]
                                                    if not s["ok"]), "failed") for r in failed)
        d = normalize(interrupt({
            "type": "escalation", "phase": "provisioning", "feature_id": state["feature_id"],
            "product_id": state["product_id"], "title": state["title"],
            "reason": f"Couldn't finish setting up new repositories: {reason}",
            "findings": [f"{r['repo']}: {s['step']}: {s['error']}" for r in failed for s in r["steps"] if not s["ok"]],
            "new_repos": results, "cost_usd": round(float(state.get("cost_usd", 0.0)), 4),
            "budget_usd": state.get("token_budget_usd"), "allowed": list(PROVISION_DECISIONS)}), PROVISION_DECISIONS)
        rec = _record("escalation:provisioning", "provisioning", d)
        if d["decision"] == "abort":
            return Command(goto=END, update={"new_repos": results, "gate_decisions": [rec], "status": "aborted",
                                             "events": events + [event("provision", "aborted", d["feedback"],
                                                                       approver=d["approver"])]})
        if d["decision"] == "retry":
            return Command(goto="provision", update={"new_repos": results, "gate_decisions": [rec],
                                                     "events": events + [event("provision", "retry", d["feedback"],
                                                                               approver=d["approver"])]})
        # resolved: a person finished the setup by hand
        fixed = [{**r, "status": "provisioned", "completed_by": d["approver"]} if r["status"] != "provisioned" else r
                 for r in results]
        return Command(goto="plan", update={"new_repos": fixed, "gate_decisions": [rec],
                                            "events": events + [event("provision", "resolved", d["feedback"],
                                                                      approver=d["approver"])]})

    return provision, ("plan", "provision", "provision_wait", END)


def make_provision_wait(services):
    """Wait for the provisioning worker (security S4), then look again. Its own node, so resuming it
    can't be mistaken for an answer to the provisioning escalation."""

    def provision_wait(state: FeatureState) -> Command:
        from ..provision_queue import WAIT

        queued = [r for r in state.get("new_repos") or [] if r.get("status") == "queued"]
        if queued:
            interrupt({"type": "external", "wait": WAIT, "product_id": state["product_id"],
                       "feature_id": state["feature_id"], "since": now(),
                       "request_ids": [r["request_id"] for r in queued],
                       "label": "Waiting for the provisioning worker to create " + ", ".join(r["repo"] for r in queued)})
        return Command(goto="provision")

    return provision_wait, ("provision",)

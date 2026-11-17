"""How a feature starts: intake, the hotfix brief and a workflow profile's brief."""

from __future__ import annotations


from ..state import FeatureState, GateDecision
from .. import profiles
from .phase import event, now, touched_lanes
from .routing import PHASE_SPECS_GATE


def make_intake(services):
    def intake(state: FeatureState) -> dict:
        # Pins were resolved by the engine before the thread started; reading them here fails
        # fast if the product's governance repo is unreachable or invalid.
        snap = services.feature_snapshot(state)
        product = snap.product
        budget = min(float(state.get("token_budget_usd") or product.budget_usd), product.budget_usd)
        return {
            "status": "running",
            "data_class": product.data_class,
            "constitution_version": snap.version,
            "token_budget_usd": budget,
            "budget_base_usd": float(state.get("budget_base_usd") or budget),
            "events": [event("intake", "started", state["title"], product=product.product_id,
                             governance=f"{snap.pins['product']['org']}/{snap.pins['product']['repo']}"
                                        f"@{snap.pins['product']['sha'][:7]}")],
        }

    return intake


def hotfix_approvers(state: FeatureState, lane, gate: str) -> list[str]:
    """An incident's hotfix: on-call decides G5 and G6. A security fix from the upkeep policy: the
    lane's security approvers decide G5 (G5's own if it names none); G6 stays the release approvers'."""
    if state.get("track") == "hotfix" and gate in ("g5", "g6"):
        if (state.get("hotfix") or {}).get("source") == "finding":
            if gate == "g5":
                return list(lane.approvers.get("security") or lane.approvers.get("g5", []))
            return list(lane.approvers.get(gate, []))
        return list(lane.approvers.get("oncall") or lane.approvers.get(gate, []))
    return list(lane.approvers.get(gate, []))


def make_hotfix_brief(services):
    """An incident's hotfix skips discovery to architecture: on-call already decided it is needed.
    It starts at planning from a short spec and design written from the incident, so planning,
    tests, security and the merge and release gates still apply."""

    def hotfix_brief(state: FeatureState) -> dict:
        h = state.get("hotfix") or {}
        snap = services.feature_snapshot(state)
        repo = h.get("repo", "")
        role = next((r.role for lane in snap.lanes().values() for r in lane.repos if r.name == repo), "provider")
        if h.get("source") == "finding":
            return finding_brief(state, h, snap, repo, role)
        inc = h.get("incident_id", "the incident")
        prd = "\n".join([
            f"# Hotfix spec: {state['title']}", "", "## Goals", f"- Stop the failure behind {inc}", "",
            "## Non-goals", "- New behaviour beyond the fix", "", "## Acceptance criteria",
            f"- AC-1: Given the conditions of {inc}, when the fix is deployed, then the failure no longer occurs "
            "and its alerts resolve",
            "- AC-2: Given the existing tests, when the fix is built, then they still pass", "",
            "## Constitution articles", "- VX-5", "", "## Change", h.get("change", ""), ""])
        hld = "\n".join([
            f"# Hotfix design: {state['title']}", "", "## Builds on", f"- None: a fix within the existing design ({inc})",
            "", "## ADRs", f"- ADR-{state['feature_id']}-1: Fix forward in {repo} for {inc}; no design change", "",
            "## Affected repos", f"- {repo} ({role})", "", "## Contracts", f"- {repo}: unchanged", ""])
        arts, version = {}, snap.version
        for kind, phase, content in (("PRD", "spec", prd), ("HLD", "architecture", hld)):
            arts[kind] = services.artifacts.save(feature_id=state["feature_id"], kind=kind, phase=phase,
                                                 content=content, parents=[], constitution_version=version)
        by = h.get("approved_by", "")
        return {
            "artifacts": arts,
            "approvals": {g: True for g in ("g1", "g2", "g3", "g4")},
            "phase_status": {p: "skipped" for p in ("discovery", "spec", "design", "architecture")},
            "lanes_touched": touched_lanes(snap, [repo]),
            "events": [event("hotfix", "expedited", f"Hotfix for {inc}, approved by on-call {by}", approver=by)],
        }

    def finding_brief(state: FeatureState, h: dict, snap, repo: str, role: str) -> dict:
        """A security fix the upkeep policy decided (valxel_sdlc.upkeep): governance's policy is the
        approval to start, so it starts at planning like an incident's hotfix."""
        ids = list(h.get("findings") or [])
        acs = [f"- AC-{i}: Given {fid}, when the fix merges, then GitHub closes the alert on the default branch"
               for i, fid in enumerate(ids, 1)]
        acs.append(f"- AC-{len(ids) + 1}: Given the existing tests, when the fix is built, then they still pass "
                   "and no check is weakened")
        prd = "\n".join([
            f"# Security fix spec: {state['title']}", "", "## Goals", f"- Close {len(ids)} security finding(s) in {repo}", "",
            "## Non-goals", "- New behaviour; changes beyond what closes the findings", "", "## Acceptance criteria",
            *acs, "", "## Constitution articles", "- VX-5", "", "## Change", h.get("change", ""), ""])
        hld = "\n".join([
            f"# Security fix design: {state['title']}", "", "## Builds on", "- None: a fix within the existing design",
            "", "## ADRs", f"- ADR-{state['feature_id']}-1: Fix in {repo}; no design change", "",
            "## Affected repos", f"- {repo} ({role})", "", "## Contracts", f"- {repo}: unchanged", ""])
        arts = {}
        for kind, phase, content in (("PRD", "spec", prd), ("HLD", "architecture", hld)):
            arts[kind] = services.artifacts.save(feature_id=state["feature_id"], kind=kind, phase=phase,
                                                 content=content, parents=[], constitution_version=snap.version)
        held = (f"; held for the next release, or its own G6 on {h['release_by']}" if h.get("hold") and h.get("release_by")
                else "; released as soon as G6 approves")
        return {
            "artifacts": arts,
            "approvals": {g: True for g in ("g1", "g2", "g3", "g4")},
            "phase_status": {p: "skipped" for p in ("discovery", "spec", "design", "architecture")},
            "lanes_touched": touched_lanes(snap, [repo]),
            "events": [event("hotfix", "expedited", f"Security fix for {len(ids)} finding(s), by the upkeep policy "
                             f"({h.get('outcome', 'hotfix')}: {h.get('why', '')}){held}", approver=h.get("approved_by", ""))],
        }

    return hotfix_brief


def make_profile_brief(services):
    """A bug fix or an express change skips discovery and UX design: it starts at the spec from
    what the person wrote, and the gates the profile decides are recorded as decided by it."""

    def profile_brief(state: FeatureState) -> dict:
        profile = profiles.get(state.get("profile"))
        snap = services.feature_snapshot(state)
        ref = services.artifacts.save(feature_id=state["feature_id"], kind="OPP", phase="discovery",
                                      content=profiles.brief(state, profile), parents=[],
                                      constitution_version=snap.version)
        decided = [GateDecision(gate=g, phase=PHASE_SPECS_GATE.get(g, ""), decision="approve",
                                approver=f"profile:{profile.name}", feedback=f"{profile.title}: not a gate in this profile",
                                active_minutes=0.0, at=now()) for g in profile.skips_gates]
        return {
            "artifacts": {"OPP": ref},
            "approvals": {g: True for g in profile.skips_gates},
            "phase_status": {p: "skipped" for p in profile.skips},
            "gate_decisions": decided,
            "events": [event("intake", "profile", f"{profile.title}: starts at the spec; skips "
                             + ", ".join(profile.skips) + "; gates decided by the profile: "
                             + ", ".join(g.upper() for g in profile.skips_gates), profile=profile.name)],
        }

    return profile_brief

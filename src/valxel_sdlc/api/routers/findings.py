"""Incidents and scan findings."""

from __future__ import annotations

from types import SimpleNamespace

from fastapi import APIRouter, Depends, HTTPException

from ...web.auth import (
    User,
    can_decide,
)



def build(ctx: SimpleNamespace) -> APIRouter:
    api = APIRouter()
    eng = ctx.eng
    current_user = ctx.current_user
    lanes_for = ctx.lanes_for
    member_of = ctx.member_of
    confirm_decider = ctx.confirm_decider

    @api.get("/incidents")
    def incidents(user: User = Depends(current_user), product_id: str | None = None) -> list[dict]:
        lanes = set(lanes_for(user))
        rows = [r for r in eng().index.all() if r.get("kind") == "incident" and r["product_id"] in lanes
                and (not product_id or r["product_id"] == product_id)]
        out = []
        for r in reversed(rows):
            try:
                v = eng().view(r["feature_id"])
            except KeyError:
                continue
            out.append({k: v.get(k) for k in ("feature_id", "product_id", "title", "severity", "status", "component",
                                              "opened_at", "firing", "links")})
        return out

    # --- roadmap: initiatives and releases ---
    # --- scan upkeep: security findings on products' default branches ---
    @api.get("/findings")
    def findings(product_id: str, status: str | None = None, user: User = Depends(current_user)) -> list[dict]:
        """The product's findings register: every alert on its default branches, with its outcome,
        deadline and fix (valxel_sdlc.upkeep)."""
        member_of(user, product_id)
        statuses = tuple(x for x in (status or "").split(",") if x) or None
        keep = ("id", "repo", "kind", "tool", "rule", "severity", "title", "url", "state", "status", "outcome", "why",
                "first_seen", "deadline", "release_by", "fix_feature", "release_id", "incident_id", "accepted", "attempts")
        return [{k: r.get(k) for k in keep} for r in eng().services.findings.list(product_id, statuses)]

    @api.get("/findings/{finding_id:path}/history")
    def finding_history(finding_id: str, user: User = Depends(current_user)) -> dict:
        rec = eng().services.findings.get(finding_id)
        if rec is None:
            raise HTTPException(404, f"Unknown finding {finding_id}")
        member_of(user, rec["product_id"])
        return rec

    @api.post("/findings/{finding_id:path}/accept")
    def accept_finding(finding_id: str, body: dict, user: User = Depends(current_user)) -> dict:
        """A security approver accepts a finding's risk until a date: the same rule as a waiver in a
        feature (a reason, at most quality.yaml's waivers.max_days). It comes back when it expires."""
        rec = eng().services.findings.get(finding_id)
        if rec is None:
            raise HTTPException(404, f"Unknown finding {finding_id}")
        pid = rec["product_id"]
        member_of(user, pid)
        lane = eng().services.snapshot(pid).product
        security = list(lane.approvers.get("security") or lane.approvers.get("g5") or [])
        if not can_decide(user, {"approvers": security}):
            raise HTTPException(403, "Only the product's security approvers can accept a finding's risk")
        confirm_decider(user, security)
        try:
            return eng().upkeep.accept(finding_id, approver=user.login, reason=str(body.get("reason") or ""),
                                       days=int(body.get("days") or 0))
        except (ValueError, KeyError) as exc:
            raise HTTPException(400, str(exc)) from exc

    return api

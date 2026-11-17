"""Initiatives, inceptions, releases, notifications and change requests."""

from __future__ import annotations

import os
from types import SimpleNamespace

from fastapi import APIRouter, Depends, HTTPException

from ...duplicates import DuplicateIntent
from ...governance import GovernanceError
from ...web.auth import (
    User,
    can_decide,
)
from ..models import InitiativeIn, StartCandidateIn, InceptionIn, ReleaseIn, NotifyIn, WatchIn, RequestIn, StartIn


def build(ctx: SimpleNamespace) -> APIRouter:
    api = APIRouter()
    eng = ctx.eng
    current_user = ctx.current_user
    founder_of = ctx.founder_of
    member_of = ctx.member_of

    @api.post("/initiatives", status_code=201)
    def create_initiative(body: InitiativeIn, user: User = Depends(current_user)) -> dict:
        member_of(user, body.product_id)
        try:
            iid = eng().create_initiative(product_id=body.product_id, title=body.title, request=body.request,
                                          measure=body.measure, horizon=body.horizon, created_by=user.login,
                                          background=True, research=body.research)
        except GovernanceError as exc:
            raise HTTPException(424, f"Product governance could not be loaded: {exc}") from exc
        return {"feature_id": iid}

    @api.get("/me/notifications")
    def my_notifications(user: User = Depends(current_user)) -> dict:
        from dataclasses import asdict as _asdict

        n = eng().services.notifier
        p = _asdict(n.get(user.login, user.teams))
        return {**{k: p[k] for k in ("email", "needed", "staging", "watch")},
                "channels": {"email": bool(os.environ.get("VALXEL_SMTP_HOST")),
                             "chat": bool(os.environ.get("VALXEL_NOTIFY_WEBHOOK"))},
                "sent": [m for m in n.outbox if user.login in m["to"]][-20:]}

    @api.put("/me/notifications")
    def set_notifications(body: NotifyIn, user: User = Depends(current_user)) -> dict:
        eng().services.notifier.set(user.login, user.teams, **body.model_dump())
        return my_notifications(user)

    @api.post("/threads/{thread_id}/watch")
    def watch(thread_id: str, body: WatchIn, user: User = Depends(current_user)) -> dict:
        p = eng().services.notifier.watch(user.login, thread_id, body.on)
        return {"watching": thread_id in p.watch}

    def approvers_of(thread_id: str, v: dict) -> list[str]:
        """Who approved this kind of thread, so may merge its record: the council, or the lane's G0 approvers."""
        council = list((eng().services.settings.inception or {}).get("council") or [])
        if v.get("product_id") == "INCEPTION" or thread_id.split("-")[1] in ("REV",):
            return council
        lane = eng().services.snapshot(v["product_id"]).product
        return list(lane.approvers.get("g0") or lane.approvers.get("g1") or []) + council

    @api.get("/threads/{thread_id}/records")
    def records(thread_id: str, user: User = Depends(current_user)) -> list[dict]:
        try:
            return eng().record_prs(thread_id)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc

    @api.post("/threads/{thread_id}/records/{key}/merge")
    def merge_record(thread_id: str, key: str, user: User = Depends(current_user)) -> dict:
        """Merge an approved roadmap or setup pull request here, instead of a second click on GitHub."""
        from ...integrations.github import GitHubError

        try:
            v = eng().view(thread_id)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        who = approvers_of(thread_id, v)
        if not can_decide(user, {"approvers": who}):
            raise HTTPException(403, f"Only {', '.join(who) or 'its approvers'} can merge it")
        try:
            return eng().merge_record(thread_id, key, by=user.login)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        except (GitHubError, RuntimeError) as exc:
            raise HTTPException(409, f"GitHub didn't merge it ({exc}). Merge it on GitHub instead.") from exc

    @api.post("/initiatives/{initiative_id}/start", status_code=201)
    def start_features(initiative_id: str, body: StartIn, user: User = Depends(current_user)) -> dict:
        """Start an approved initiative's features: together, or in order."""
        try:
            v = eng().view(initiative_id)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        member_of(user, v["product_id"])
        try:
            return eng().start_candidates(initiative_id, body.indexes, order=body.order, by=user.login)
        except DuplicateIntent as exc:
            raise HTTPException(409, {"message": str(exc), "similar": exc.matches}) from exc
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @api.post("/inceptions/{inception_id}/first-feature", status_code=201)
    def first_feature(inception_id: str, user: User = Depends(current_user)) -> dict:
        try:
            v = eng().view(inception_id)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        who = approvers_of(inception_id, v)
        if not can_decide(user, {"approvers": who}):
            raise HTTPException(403, f"Only {', '.join(who) or 'the product council'} can start it")
        try:
            return {"feature_id": eng().start_first_feature(inception_id, by=user.login)}
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @api.put("/features/{feature_id}/request")
    def change_request(feature_id: str, body: RequestIn, user: User = Depends(current_user)) -> dict:
        """Change what was asked for, before the build starts (the person who asked, or a G1 approver)."""
        try:
            v = eng().view(feature_id)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        member_of(user, v["product_id"])
        g1 = eng().services.snapshot(v["product_id"]).product.approvers.get("g1", [])
        if v.get("created_by") != user.login and not can_decide(user, {"approvers": g1}):
            raise HTTPException(403, "Only the person who asked for it, or its idea approvers, can change the request")
        try:
            return eng().change_request(feature_id, body.description, by=user.login)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc

    @api.post("/threads/{thread_id}/resize", status_code=201)
    def resize(thread_id: str, user: User = Depends(current_user)) -> dict:
        """A feature at G1 becomes an initiative; an initiative at G0 becomes one feature."""
        try:
            v = eng().view(thread_id)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        member_of(user, v["product_id"])
        waiting = [p for p in v["pending"] if p.get("gate") in ("g1", "g0")]
        if v.get("created_by") != user.login and not any(can_decide(user, p) for p in waiting):
            raise HTTPException(403, "Only the person who asked for it, or its approvers, can change its size")
        try:
            new = eng().resize(thread_id, by=user.login, background=True)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        return {"feature_id": new}

    @api.post("/initiatives/{initiative_id}/features", status_code=201)
    def start_candidate(initiative_id: str, body: StartCandidateIn, user: User = Depends(current_user)) -> dict:
        try:
            v = eng().view(initiative_id)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        member_of(user, v["product_id"])
        if v["status"] != "approved":
            raise HTTPException(409, "Only an approved initiative's features can be started")
        if body.index >= len(v["candidates"]):
            raise HTTPException(404, "No such candidate feature")
        c = v["candidates"][body.index]
        try:
            fid = eng().create(product_id=v["product_id"], title=c["title"], description=c["description"],
                               created_by=user.login, background=True, initiative_id=initiative_id,
                               not_duplicate_reason=body.not_duplicate_reason)
        except DuplicateIntent as exc:
            raise HTTPException(409, {"message": str(exc), "similar": exc.matches}) from exc
        return {"feature_id": fid}

    # --- product inception: a vision to a brief, an architecture and a roadmap ---
    @api.post("/inceptions", status_code=201)
    def create_inception(body: InceptionIn, user: User = Depends(current_user)) -> dict:
        from ...inception import Vision

        settings = eng().services.settings
        starters = list((settings.inception or {}).get("starters") or [])
        if body.workspace:
            founder_of(user, body.workspace)
        elif not can_decide(user, {"approvers": starters}):
            raise HTTPException(403, f"Only {', '.join(starters) or 'the inception starters'} can start a product inception")
        existing = bool(body.product_id) and body.product_id in settings.registry
        if existing:
            member_of(user, body.product_id)
            org = settings.entry(body.product_id).governance.org
        else:
            if not body.org:
                raise HTTPException(422, "Name the GitHub org the product will live in")
            org = body.org
        v = Vision(name=body.name, org=org, vision=body.vision, goals=body.goals, users=body.users,
                   constraints=body.constraints, budget_usd=body.budget_usd, product_id=body.product_id,
                   existing=existing, research_depth=body.research, workspace="" if existing else body.workspace)
        return {"feature_id": eng().create_inception(v, created_by=user.login, background=True)}

    @api.get("/inceptions")
    def inceptions(user: User = Depends(current_user)) -> list[dict]:
        return eng().inception_summaries()

    @api.post("/releases", status_code=201)
    def create_release(body: ReleaseIn, user: User = Depends(current_user)) -> dict:
        member_of(user, body.product_id)
        try:
            rid = eng().create_release(product_id=body.product_id, title=body.title, model=body.model,
                                       features=body.features, target_date=body.target_date,
                                       initiative=body.initiative, created_by=user.login, background=True)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        return {"feature_id": rid}

    return api

"""The intake chat and its attachments."""

from __future__ import annotations

import logging
from types import SimpleNamespace

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse

from ...duplicates import DuplicateIntent
from ...gateway import PolicyViolation
from ...governance import GovernanceError
from ...intake import IntakeAgent
from ...web.auth import (
    User,
    can_decide,
)
from ..models import SubmitIn, IntakeStartIn, MessageIn, DraftIn


log = logging.getLogger(__name__)

def build(ctx: SimpleNamespace) -> APIRouter:
    api = APIRouter()
    stores = ctx.stores
    eng = ctx.eng
    current_user = ctx.current_user
    lanes_for = ctx.lanes_for
    founder_of = ctx.founder_of

    # --- intake chat ---
    def intake_session(sid: str, user: User):
        try:
            return stores["intakes"].get(sid, user.login)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc

    @api.post("/intake", status_code=201)
    def intake_start(body: IntakeStartIn | None = None, user: User = Depends(current_user)) -> dict:
        lanes = lanes_for(user)
        ws = (body.workspace if body else "") or ""
        if ws:  # a self-serve workspace's admin starts its (first) product
            founder_of(user, ws)
        elif not lanes:
            raise HTTPException(403, "You aren't a member of any product organization")
        starters = list((eng().services.settings.inception or {}).get("starters") or [])
        s = stores["intakes"].create(user.login, lanes, starter=bool(ws) or can_decide(user, {"approvers": starters}),
                                     workspace=ws)
        greeting = ("What do you want to do? Describe it in your own words: a change to a product, a goal you want "
                    "to move, or a new product. I'll work out the size with you.")
        s.messages.append({"role": "agent", "text": greeting, "at": s.created_at})
        stores["intakes"].save(s)
        return s.to_dict()

    @api.get("/intake")
    def intake_list(user: User = Depends(current_user)) -> list[dict]:
        return [s.to_dict() for s in stores["intakes"].list(user.login)]

    @api.get("/intake/{sid}")
    def intake_get(sid: str, user: User = Depends(current_user)) -> dict:
        return intake_session(sid, user).to_dict()

    @api.post("/intake/{sid}/messages")
    def intake_message(sid: str, body: MessageIn, user: User = Depends(current_user)) -> dict:
        s = intake_session(sid, user)
        try:
            IntakeAgent(eng().services).respond(s, body.text)
        except PolicyViolation as exc:
            raise HTTPException(403, str(exc)) from exc
        finally:
            stores["intakes"].save(s)
        return s.to_dict()

    @api.patch("/intake/{sid}/draft")
    def intake_draft(sid: str, body: DraftIn, user: User = Depends(current_user)) -> dict:
        s = intake_session(sid, user)
        try:
            IntakeAgent(eng().services).update_draft(s, body.model_dump(exclude_none=True))
        except PermissionError as exc:
            raise HTTPException(403, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        stores["intakes"].save(s)
        return s.to_dict()

    # --- attachments: files for the intent, kept as the strictest data class until it's submitted ---
    @api.post("/intake/{sid}/attachments", status_code=201)
    async def intake_attach(sid: str, request: Request, name: str, user: User = Depends(current_user)) -> dict:
        """The file is the request body (not a form), named by `name`."""
        from ...attachments import AttachmentError, max_bytes

        s = intake_session(sid, user)
        if s.feature_id:
            raise HTTPException(409, f"Already submitted as {s.feature_id}")
        declared = int(request.headers.get("content-length") or 0)
        if declared > max_bytes():
            raise HTTPException(413, f"{name} is larger than {max_bytes() // (1024 * 1024)} MB")
        data = bytearray()
        async for chunk in request.stream():
            data.extend(chunk)
            if len(data) > max_bytes():
                raise HTTPException(413, f"{name} is larger than {max_bytes() // (1024 * 1024)} MB")
        try:
            meta = eng().services.attachments.add(s.id, s.attachments, name, bytes(data))
        except AttachmentError as exc:
            raise HTTPException(422, str(exc)) from exc
        s.attachments.append(meta)
        # In the conversation too, by name only: the agent sees what was attached, never what's in it.
        s.messages.append({"role": "user", "text": f"Attached {meta['name']}", "at": meta["uploaded_at"],
                           "attachment": meta["id"]})
        stores["intakes"].save(s)
        return s.to_dict()

    @api.delete("/intake/{sid}/attachments/{aid}")
    def intake_detach(sid: str, aid: str, user: User = Depends(current_user)) -> dict:
        s = intake_session(sid, user)
        if s.feature_id:
            raise HTTPException(409, f"Already submitted as {s.feature_id}")
        meta = next((a for a in s.attachments if a["id"] == aid), None)
        if meta is None:
            raise HTTPException(404, f"No attachment {aid}")
        eng().services.attachments.remove(s.id, meta)
        s.attachments = [a for a in s.attachments if a["id"] != aid]
        s.messages = [m for m in s.messages if m.get("attachment") != aid]
        stores["intakes"].save(s)
        return s.to_dict()

    def attachment_response(path, meta: dict) -> FileResponse:
        if not path.exists():
            raise HTTPException(404, f"{meta['name']} is no longer stored")
        # Always a download, never rendered by the browser as a page.
        return FileResponse(path, media_type=meta.get("mime") or "application/octet-stream", filename=meta["name"],
                            content_disposition_type="attachment",
                            headers={"X-Content-Type-Options": "nosniff", "Content-Security-Policy": "sandbox"})

    @api.get("/intake/{sid}/attachments/{aid}")
    def intake_attachment(sid: str, aid: str, user: User = Depends(current_user)) -> FileResponse:
        s = intake_session(sid, user)
        meta = next((a for a in s.attachments if a["id"] == aid), None)
        if meta is None:
            raise HTTPException(404, f"No attachment {aid}")
        store = eng().services.attachments
        return attachment_response(store.path(meta, feature_id=s.feature_id) if s.feature_id else store.path(meta, sid=s.id), meta)

    @api.get("/features/{feature_id}/attachments/{aid}")
    def feature_attachment(feature_id: str, aid: str, user: User = Depends(current_user)) -> FileResponse:
        try:
            state = eng()._state(feature_id)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        if not state:
            raise HTTPException(404, f"No feature {feature_id}")
        if state.get("product_id") not in lanes_for(user):
            raise HTTPException(403, f"You aren't a member of {state.get('product_id')}'s organization")
        meta = next((a for a in state.get("attachments") or [] if a["id"] == aid), None)
        if meta is None:
            raise HTTPException(404, f"No attachment {aid}")
        return attachment_response(eng().services.attachments.path(meta, feature_id=feature_id), meta)

    def keep_case(s) -> None:
        """An intake eval case: what the person said, and the draft they confirmed (valxel_sdlc.evals)."""
        store = getattr(eng().services, "evals", None)
        if store is None:
            return
        from ...intake import eval_case

        try:
            store.capture(eval_case(eng().services, s))
        except Exception:
            log.warning("intake %s: eval case not kept", s.id, exc_info=True)

    def submit_sized(s, user: User) -> dict:
        """An initiative (to G0) or a new product (to inception) from the front door."""
        from ...intake import AGENT_PROPOSES

        d = s.draft
        measure = "" if d.success_metric == AGENT_PROPOSES else d.success_metric
        try:
            if d.size == "initiative":
                if d.product_id not in lanes_for(user):
                    raise HTTPException(403, f"You aren't a member of {d.product_id}'s organization")
                s.feature_id = eng().create_initiative(
                    product_id=d.product_id, title=d.title, request=d.request(), measure=measure,
                    horizon="" if d.horizon == AGENT_PROPOSES else d.horizon, created_by=user.login,
                    background=True, research=d.research or "quick")
                s.kind, what = "initiative", "an initiative: research first, then the draft for G0"
            else:
                from ...inception import Vision

                starters = list((eng().services.settings.inception or {}).get("starters") or [])
                if s.workspace:
                    founder_of(user, s.workspace)
                elif not can_decide(user, {"approvers": starters}):
                    raise HTTPException(403, f"Only {', '.join(starters) or 'the inception starters'} can start a product")
                from ...intake import installation_cloud

                cloud = ({"provider": installation_cloud(), "region": d.region, "accounts": dict(d.cloud_accounts),
                          "placement": "dedicated"} if d.cloud == "given" else
                         {"provider": installation_cloud(), "placement": "shared"} if d.cloud == "shared" else
                         {"provider": installation_cloud(), "later": True})
                v = Vision(name=d.name or d.title, org=d.org, vision=d.problem,
                           goals=[measure] if measure else [],
                           users=[] if d.users == AGENT_PROPOSES else [d.users], budget_usd=d.budget_usd,
                           attachments_text="", research_depth=d.research or "quick", workspace=s.workspace,
                           cloud=cloud)
                s.feature_id = eng().create_inception(v, created_by=user.login, background=True)
                s.kind, what = "inception", "a new product: research first, then the brief for the product council"
        except GovernanceError as exc:
            raise HTTPException(424, f"Product governance could not be loaded: {exc}") from exc
        s.messages.append({"role": "agent", "text": f"Started {s.feature_id}, {what}.", "at": s.messages[-1]["at"]})
        stores["intakes"].save(s)
        keep_case(s)
        return s.to_dict()

    @api.post("/intake/{sid}/submit", status_code=201)
    def intake_submit(sid: str, body: SubmitIn | None = None, user: User = Depends(current_user)) -> dict:
        s = intake_session(sid, user)
        if s.feature_id:
            raise HTTPException(409, f"Already submitted as {s.feature_id}")
        if s.draft.missing():
            raise HTTPException(422, f"Still needed: {', '.join(s.draft.missing())}")
        if s.draft.size in ("initiative", "product"):
            return submit_sized(s, user)
        if s.draft.product_id not in lanes_for(user):
            raise HTTPException(403, f"You aren't a member of {s.draft.product_id}'s organization")
        if s.duplicate:
            raise HTTPException(409, {"message": "This looks like an existing feature. Add to it, or say how this one is "
                                      "different.", "similar": s.draft.similar})
        try:
            s.feature_id = eng().create(product_id=s.draft.product_id, title=s.draft.title,
                                        description=s.draft.description(), created_by=user.login, background=True,
                                        not_duplicate_reason=s.draft.not_duplicate_reason, pilot=bool(body and body.pilot),
                                        profile=(body.profile if body else ""),
                                        attachments=(s.id, list(s.attachments)),
                                        ui=s.draft.ui if s.ui_checked else None)
        except DuplicateIntent as exc:  # a matching feature appeared since the check
            s.draft.similar, s.overlap_checked = exc.matches, True
            stores["intakes"].save(s)
            raise HTTPException(409, {"message": str(exc), "similar": exc.matches}) from exc
        except GovernanceError as exc:
            raise HTTPException(424, f"Product governance could not be loaded: {exc}") from exc
        s.kind = "feature"
        s.messages.append({"role": "agent", "text": f"Submitted as {s.feature_id}. Agents have started on it.",
                           "at": s.messages[-1]["at"]})
        stores["intakes"].save(s)
        keep_case(s)
        return s.to_dict()

    return api

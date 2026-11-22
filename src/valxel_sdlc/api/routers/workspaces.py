"""Workspaces: members, roles, approver groups, plans and connections."""

from __future__ import annotations

import logging
import os
from dataclasses import asdict
from types import SimpleNamespace

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response, RedirectResponse

from ...web.auth import (
    User,
)
from ..context import CONNECT_COOKIE
from ..models import WorkspaceIn, PlanIn, MemberIn, MemberUpdate, GroupIn, GroupMembers, AwsCheckIn, GatesIn


log = logging.getLogger(__name__)


def build(ctx: SimpleNamespace) -> APIRouter:
    api = APIRouter()
    auth = ctx.auth
    signer = ctx.signer
    eng = ctx.eng
    current_user = ctx.current_user
    role_in = ctx.role_in
    admin_of = ctx.admin_of
    local_path = ctx.local_path
    wsx = ctx.wsx

    # --- workspaces: members, roles, approver groups, gate mappings (valxel_sdlc.workspaces) ---
    def ws_call(fn, *args, **kwargs):
        from ...workspaces import WorkspaceError

        try:
            return fn(*args, **kwargs)
        except WorkspaceError as exc:
            raise HTTPException(400, str(exc)) from exc
        except PermissionError as exc:
            raise HTTPException(403, str(exc)) from exc

    def send_invite(member: dict, workspace: str, user: User, url: str) -> bool:
        n = getattr(eng().services, "notifier", None)
        name = (wsx().get(workspace) or {}).get("name") or workspace
        return bool(n and member.get("email") and n.invite(member["email"], workspace=name, by=user.name or user.login,
                                                           role=member.get("role", "member"), url=url))

    @api.get("/workspaces")
    def my_workspaces(user: User = Depends(current_user)) -> list[dict]:
        return [{**(wsx().get(w) or {"id": w, "name": w}), "role": r} for w, r in sorted(user.roles.items())]

    @api.post("/workspaces", status_code=201)
    def create_workspace(body: WorkspaceIn, user: User = Depends(current_user)) -> dict:
        """A new workspace, with the signed-in person as its owner, in every approver group."""
        return ws_call(wsx().create, body.id, name=body.name, actor=user.login, plan=body.plan,
                       owner={"name": user.name or user.login, "email": user.email,
                              "github_login": user.login if user.idp == "github" else ""},
                       self_serve=body.plan != "self-hosted")

    @api.get("/plans")
    def list_plans(user: User = Depends(current_user)) -> dict:
        """The plans table for sign-up, and where the self-hosted install guide is (valxel_sdlc.billing)."""

        from ...billing import INSTALL_GUIDE, TRIAL_DAYS, plans

        return {"plans": [asdict(p) for p in plans()], "trial_days": TRIAL_DAYS,
                "install_guide": os.environ.get("VALXEL_INSTALL_GUIDE_URL") or INSTALL_GUIDE}

    @api.get("/workspaces/{workspace}/billing")
    def workspace_billing(workspace: str, user: User = Depends(current_user)) -> dict:
        from ...billing import NoBilling, status

        if workspace not in user.roles:
            raise HTTPException(404, f"No workspace {workspace}")
        c = getattr(eng().services, "connections", None)
        provider = (c.billing if c is not None else None) or NoBilling()
        live = bool(c is not None and c.of(workspace, "aws"))
        return status(wsx().get(workspace) or {"id": workspace}, provider, live=live)

    @api.put("/workspaces/{workspace}/plan")
    def change_plan(workspace: str, body: PlanIn, user: User = Depends(current_user)) -> dict:
        """Owners move a hosted workspace between Lite and Full."""
        if role_in(user, workspace) != "owner":
            raise HTTPException(403, f"Only {workspace}'s owners change its plan")
        admin_of(user, workspace)  # and from a strong sign-in
        return ws_call(wsx().set_plan, workspace, body.plan, actor=user.login)

    @api.get("/workspaces/{workspace}")
    def workspace_view(workspace: str, user: User = Depends(current_user)) -> dict:
        """Members see the workspace; admins also see invitations and the gate mappings to edit."""
        if workspace not in user.roles:
            raise HTTPException(404, f"No workspace {workspace}")
        w = wsx()
        return {**(w.get(workspace) or {"id": workspace}), "role": user.roles[workspace],
                "members": w.members(workspace), "groups": w.groups(workspace),
                "products": [{"id": p, "gates": (w.mapping(p) or {}).get("gates", {}),
                              "version": (w.mapping(p) or {}).get("version", 0)} for p in w.products(workspace)]}

    @api.post("/workspaces/{workspace}/members", status_code=201)
    def invite_member(workspace: str, body: MemberIn, user: User = Depends(current_user)) -> dict:
        """A profile for someone, invited by email. The link is returned for the admin to share until
        invitation emails are sent (sign-in design, A3)."""
        admin_of(user, workspace)
        m, token = ws_call(wsx().invite, workspace, body.model_dump(), role=body.role, actor=user.login,
                           products=body.products)
        url = f"{auth.public_url}/join/{workspace}/{token}"
        return {**m, "invite_url": url, "emailed": send_invite(m, workspace, user, url)}

    @api.post("/workspaces/{workspace}/members/{member_id}/resend")
    def resend_invite(workspace: str, member_id: str, user: User = Depends(current_user)) -> dict:
        admin_of(user, workspace)
        token = ws_call(wsx().resend, workspace, member_id, actor=user.login)
        url = f"{auth.public_url}/join/{workspace}/{token}"
        return {"invite_url": url, "emailed": send_invite(wsx().member(workspace, member_id) or {}, workspace, user, url)}

    @api.post("/workspaces/{workspace}/join/{token}")
    def join_workspace(workspace: str, token: str, user: User = Depends(current_user)) -> dict:
        return ws_call(wsx().accept, workspace, token, login=user.login, email=user.email)

    @api.patch("/workspaces/{workspace}/members/{member_id}")
    def update_member(workspace: str, member_id: str, body: MemberUpdate, user: User = Depends(current_user)) -> dict:
        role = admin_of(user, workspace)
        return ws_call(wsx().update_member, workspace, member_id, actor=user.login, actor_role=role, role=body.role,
                       products=body.products, status=body.status)

    @api.delete("/workspaces/{workspace}/members/{member_id}", status_code=204)
    def remove_member(workspace: str, member_id: str, user: User = Depends(current_user)):

        role = admin_of(user, workspace)
        ws_call(wsx().remove_member, workspace, member_id, actor=user.login, actor_role=role)
        return Response(status_code=204)

    @api.post("/workspaces/{workspace}/members/{member_id}/sign-out")
    def sign_member_out(workspace: str, member_id: str, user: User = Depends(current_user)) -> dict:
        """Ends every session the member has now, on every device (web/sessions.py): for a stolen
        laptop, or alongside removing or suspending them. They can sign in again unless removed."""
        admin_of(user, workspace)
        m = wsx().member(workspace, member_id)
        if m is None:
            raise HTTPException(404, f"No member {member_id} in {workspace}")
        logins = {x for x in (m.get("github_login"), m.get("email")) if x}
        for login in logins:
            ctx.stores["sessions"].revoke_login(login, by=user.login, reason=f"signed out by an admin of {workspace}")
        eng().services.audit.record("session.revoke", actor=user.login, resource=("person", sorted(logins)),
                                    workspace=workspace, details={"member": member_id})
        return {"signed_out": sorted(logins)}

    @api.post("/workspaces/{workspace}/groups", status_code=201)
    def create_group(workspace: str, body: GroupIn, user: User = Depends(current_user)) -> dict:
        admin_of(user, workspace)
        return ws_call(wsx().create_group, workspace, body.name, actor=user.login)

    @api.put("/workspaces/{workspace}/groups/{group}")
    def set_group(workspace: str, group: str, body: GroupMembers, user: User = Depends(current_user)) -> dict:
        admin_of(user, workspace)
        return ws_call(wsx().set_group, workspace, group, members=body.members, actor=user.login)

    @api.delete("/workspaces/{workspace}/groups/{group}", status_code=204)
    def delete_group(workspace: str, group: str, user: User = Depends(current_user)):

        admin_of(user, workspace)
        ws_call(wsx().delete_group, workspace, group, actor=user.login)
        return Response(status_code=204)

    @api.put("/workspaces/{workspace}/products/{product_id}/gates")
    def set_gates(workspace: str, product_id: str, body: GatesIn, user: User = Depends(current_user)) -> dict:
        """Which groups approve each of the product's gates. A new version: features already under way
        keep the one they started with."""
        admin_of(user, workspace)
        if eng().services.settings.registry.get(product_id) is None or wsx().workspace_of(product_id) != workspace:
            raise HTTPException(404, f"{product_id} isn't in {workspace}")
        return ws_call(wsx().set_mapping, product_id, body.gates, actor=user.login)

    # --- connections: each workspace's tools (valxel_sdlc.connections) ---
    def conns():
        c = getattr(eng().services, "connections", None)
        if c is None:
            raise HTTPException(404, "Connections are off")
        return c

    @api.get("/workspaces/{workspace}/connections")
    def workspace_connections(workspace: str, user: User = Depends(current_user)) -> list[dict]:
        """Every tool, connected or not. Members see it; admins connect and disconnect."""
        if workspace not in user.roles:
            raise HTTPException(404, f"No workspace {workspace}")
        return conns().page(workspace)

    @api.get("/workspaces/{workspace}/connections/{tool}/install")
    def install_tool(workspace: str, tool: str, back: str = "", user: User = Depends(current_user)) -> RedirectResponse:
        """To the vendor's consent screen: GitHub's install page for one of Valxel's Apps, or the
        OAuth consent of Jira, Lucid, Sentry or PagerDuty."""
        from ...connections import GITHUB_APPS, OAUTH_TOOLS, ConnectionError_

        admin_of(user, workspace)
        if tool in OAUTH_TOOLS:
            try:
                url, st = conns().oauth_start(signer, workspace=workspace, tool=tool, actor=user.login)
            except ConnectionError_ as exc:
                raise HTTPException(409, str(exc)) from exc
            resp = RedirectResponse(url, status_code=302)
            resp.set_cookie(CONNECT_COOKIE, st, max_age=1800, httponly=True, samesite="lax",
                            secure=auth.secure_cookies, path="/api/connections")
            return resp
        if tool not in GITHUB_APPS:
            raise HTTPException(404, f"{tool} can't be connected from here yet")
        c = conns()
        try:
            url = c.github.install_url(tool, c.github_state(signer, workspace=workspace, tool=tool, actor=user.login,
                                                            back=local_path(back) if back else "")) \
                if c.github else None
        except ConnectionError_ as exc:
            raise HTTPException(409, str(exc)) from exc
        if url is None:
            raise HTTPException(409, "GitHub installs aren't set up on this Valxel")
        return RedirectResponse(url, status_code=302)

    @api.get("/connections/github/callback")
    def github_installed(request: Request, state: str = "", code: str = "", installation_id: str = "",
                         setup_action: str = "", user: User = Depends(current_user)) -> RedirectResponse:
        """Where GitHub sends the person after an install: check it's theirs, record it, and go back
        to the connections page saying what happened."""
        from urllib.parse import quote as q

        from ...connections import ConnectionError_

        st = signer.loads(state) or {}
        back = f"/workspaces/{q(str(st.get('ws') or ''))}?tab=connections" if st.get("ws") else "/workspaces?tab=connections"
        if st.get("back"):  # the sign-up wizard, say
            back = local_path(str(st["back"]))
            back += "&" if "?" in back else "?"
            back += f"tool={q(str(st.get('tool') or ''))}"
        if setup_action == "request":  # an org member asked; an org owner must approve the install
            return RedirectResponse(back + "&note=" + q("Waiting for an owner of the GitHub org to approve the install"),
                                    status_code=302)
        try:
            if st.get("ws"):
                admin_of(user, st["ws"])
            rec = conns().github_installed(signer, state=state, code=code, installation_id=installation_id,
                                           actor=user.login)
        except ConnectionError_ as exc:
            return RedirectResponse(back + "&error=" + q(str(exc)), status_code=302)
        for fid in eng().waiting_on_external():  # features waiting for this connection go on now
            try:
                if any(p.get("wait") == "connection" and p.get("workspace") == rec["workspace"]
                       for p in eng().pending(fid)):
                    eng().poke(fid)
            except Exception:
                log.exception("Couldn't wake %s after the GitHub install", fid)
        return RedirectResponse(back + "&connected=" + q(rec["account"]), status_code=302)

    @api.get("/connections/oauth/callback")
    @api.get("/connections/sentry/setup")
    def oauth_connected(request: Request, state: str = "", code: str = "", error: str = "", installationId: str = "",
                        orgSlug: str = "", user: User = Depends(current_user)) -> RedirectResponse:
        """Where Jira, Lucid and PagerDuty send the person after consent (and Sentry after its
        install): exchange the code, record what it reaches, back to the connections page."""
        from urllib.parse import quote as q

        from ...connections import ConnectionError_

        cookie = request.cookies.get(CONNECT_COOKIE, "")
        st = signer.loads(cookie) or {}
        back = f"/workspaces/{q(str(st.get('ws') or ''))}?tab=connections" if st.get("ws") else "/workspaces?tab=connections"

        def done(resp: RedirectResponse) -> RedirectResponse:
            resp.delete_cookie(CONNECT_COOKIE, path="/api/connections")
            return resp

        if error:
            return done(RedirectResponse(back + "&error=" + q(f"The connection wasn't made: {error[:100]}"), status_code=302))
        try:
            if st.get("ws"):
                admin_of(user, st["ws"])
            rec = conns().oauth_finished(signer, state=state, cookie_state=cookie, code=code, actor=user.login,
                                         installation=installationId, org=orgSlug)
        except ConnectionError_ as exc:
            return done(RedirectResponse(back + "&error=" + q(str(exc)), status_code=302))
        return done(RedirectResponse(back + "&connected=" + q(rec["account"]), status_code=302))

    @api.get("/workspaces/{workspace}/connections/aws/launch")
    def aws_launch(workspace: str, mode: str = "", region: str = "us-east-1", user: User = Depends(current_user)) -> dict:
        """The AWS console's page that creates the stack, its parameters filled in for this workspace."""
        from ...connections import ConnectionError_

        admin_of(user, workspace)
        mode = mode or (wsx().get(workspace) or {}).get("plan", "lite")
        try:
            return {"url": conns().aws_launch(workspace, mode=mode, region=region, actor=user.login), "mode": mode}
        except ConnectionError_ as exc:
            raise HTTPException(409, str(exc)) from exc

    @api.post("/workspaces/{workspace}/connections/aws/check")
    def aws_check(workspace: str, body: AwsCheckIn, user: User = Depends(current_user)) -> dict:
        """After the stack is created: Valxel assumes its roles; the account is connected when both work."""
        from ...connections import ConnectionError_

        admin_of(user, workspace)
        mode = body.mode or (wsx().get(workspace) or {}).get("plan", "lite")
        try:
            return conns().aws_check(workspace, account=body.account, mode=mode, actor=user.login)
        except ConnectionError_ as exc:
            raise HTTPException(422, str(exc)) from exc

    @api.delete("/workspaces/{workspace}/connections/{tool}/{account}")
    def disconnect_tool(workspace: str, tool: str, account: str, user: User = Depends(current_user)) -> dict:
        """Forget the connection here. The vendor's side (the App, the stack) is the customer's to remove."""
        admin_of(user, workspace)
        try:
            return conns().disconnect(workspace, tool, account, actor=user.login)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc

    @api.get("/workspaces/{workspace}/audit")
    def workspace_audit(workspace: str, limit: int = 200, user: User = Depends(current_user)) -> list[dict]:
        admin_of(user, workspace)
        return wsx().store.audit_log(workspace, max(1, min(limit, 1000)))

    @api.get("/workspaces/{workspace}/audit/verify")
    def verify_workspace_audit(workspace: str, user: User = Depends(current_user)) -> dict:
        """Whether every entry still chains to the one before it (valxel_sdlc.workspaces.verify_chain)."""
        from ...workspaces import verify_chain

        admin_of(user, workspace)
        return verify_chain(wsx().store.audit_chain(workspace))

    return api

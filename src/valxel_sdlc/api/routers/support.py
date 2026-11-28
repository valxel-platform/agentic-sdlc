"""The Support page, and the tool as Grafana's and MLflow's sign-in (valxel_sdlc.support,
web/oidc_provider.py).

    GET  /api/support                     the products the person may support, with their links
    GET  /api/support/{product}           one product: links, and its MLflow access if they're an admin
    PUT  /api/support/{product}/settings  which groups may open its agent traces (admins)
    GET  /api/support/open                opens Grafana or MLflow for one product, in a new window

    GET  /api/oidc/.well-known/openid-configuration, /api/oidc/jwks
    GET  /api/oidc/authorize              only after /api/support/open, for the product opened
    POST /api/oidc/token                  Grafana's and oauth2-proxy's servers
    GET  /api/oidc/userinfo
"""

from __future__ import annotations

import base64
import html
import logging
import time
from types import SimpleNamespace
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel

from ... import support
from ...web.oidc_provider import NOTE_COOKIE, NOTE_TTL, OIDCError, claims_for, provider_from_env

log = logging.getLogger(__name__)


class SupportSettingsIn(BaseModel):
    mlflow_groups: list[str]


def _page(status: int, title: str, message: str) -> HTMLResponse:
    """A plain page in the tool, for a sign-in it won't complete."""
    body = (f"<!doctype html><meta charset=utf-8><title>{html.escape(title)}</title>"
            f"<main style='font:16px/1.6 system-ui;max-width:36rem;margin:4rem auto'><h1 style='font-size:20px'>"
            f"{html.escape(title)}</h1><p>{html.escape(message)}</p>"
            "<p><a href='/support'>Open the Support page</a></p></main>")
    return HTMLResponse(body, status_code=status)


def build(ctx: SimpleNamespace) -> APIRouter:
    api = APIRouter()
    auth, signer, eng = ctx.auth, ctx.signer, ctx.eng
    current_user, lanes_for, role_in = ctx.current_user, ctx.lanes_for, ctx.role_in
    provider = provider_from_env()

    def workspaces():
        return eng().services.workspaces

    def workspace_of(product_id: str) -> str:
        entry = eng().services.settings.registry.get(product_id)
        if entry is None:
            raise HTTPException(404, f"No product {product_id}")
        return getattr(entry, "workspace", "") or ""

    def granted(user, product_id: str) -> support.Access:
        ws = workspace_of(product_id)
        store = workspaces()
        groups = store.support(product_id).get("mlflow_groups", []) if store is not None and ws else []
        return support.access(user, product_id, ws, lanes_for(user), groups)

    def audit(user, product_id: str, action: str, detail: dict) -> None:
        store, ws = workspaces(), workspace_of(product_id)
        if store is not None and ws:
            store.store.audit(ws, user.login, action, {"product": product_id, **detail})
        log.info("support %s: %s %s %s", action, user.login, product_id, detail)

    # ------------------------------------------------------------------ the Support page

    @api.get("/support")
    def products(user=Depends(current_user)) -> dict:
        reg = eng().services.settings.registry
        out = []
        for pid in sorted(reg):
            a = granted(user, pid)
            if a.grafana or a.mlflow:
                out.append({"product": pid, "name": getattr(reg[pid], "name", "") or pid,
                            "links": support.links(pid, a)})
        return {"products": out, "configured": bool(provider)}

    # Before /support/{product_id}, which would otherwise take "open" as a product.
    @api.get("/support/open")
    def open_(request: Request, product: str, to: str, plane: str = "", user=Depends(current_user)):
        """Leaves a two-minute note of what this person is opening, then sends them on: Grafana's or
        oauth2-proxy's sign-in comes straight back to /api/oidc/authorize, which honours only the note."""
        if provider is None:
            raise HTTPException(503, "Sign-in for Grafana and MLflow isn't set up in this installation")
        if to not in ("grafana", "mlflow") or (to == "mlflow" and plane not in support.PLANES):
            raise HTTPException(400, "to is grafana, or mlflow with a plane (prod or nonprod)")
        if not granted(user, product).allows(to):
            raise HTTPException(403, f"You can't open {to} for {product}")
        target = (f"{support.grafana_url()}/login/generic_oauth" if to == "grafana"
                  else f"{support.mlflow_url(product, plane)}/oauth2/start?rd=%2F")
        if not target.startswith("http"):
            raise HTTPException(503, f"{to} isn't configured for this installation")
        note = signer.dumps({"to": to, "product": product, "plane": plane if to == "mlflow" else "",
                             "login": user.login, "exp": int(time.time()) + NOTE_TTL})
        resp = RedirectResponse(target, status_code=303)
        resp.set_cookie(NOTE_COOKIE, note, max_age=NOTE_TTL, httponly=True, samesite="lax",
                        secure=auth.secure_cookies, path="/api/oidc")
        return resp

    @api.get("/support/{product_id}")
    def product(product_id: str, user=Depends(current_user)) -> dict:
        a = granted(user, product_id)
        if not (a.grafana or a.mlflow):
            raise HTTPException(403, "You can't support this product")
        ws = workspace_of(product_id)
        out: dict = {"product": product_id, "links": support.links(product_id, a), "grafana_role": a.grafana,
                     "mlflow": a.mlflow}
        store = workspaces()
        if store is not None and ws and role_in(user, ws) in support.EDITOR_ROLES:
            out["settings"] = {**store.support(product_id),
                               "groups": [{"slug": g["slug"], "name": g["name"]} for g in store.groups(ws)]}
        return out

    @api.put("/support/{product_id}/settings")
    def set_settings(product_id: str, body: SupportSettingsIn, user=Depends(current_user)) -> dict:
        ws = workspace_of(product_id)
        store = workspaces()
        if store is None or not ws or role_in(user, ws) not in support.EDITOR_ROLES:
            raise HTTPException(403, "Only the workspace's admins choose who may open agent traces")
        from ...workspaces import WorkspaceError

        try:
            return store.set_support(product_id, mlflow_groups=body.mlflow_groups, actor=user.login)
        except WorkspaceError as exc:
            raise HTTPException(400, str(exc)) from exc

    # ------------------------------------------------------------------ OpenID Connect

    def need_provider():
        if provider is None:
            raise HTTPException(404, "Not found")
        provider.codes = ctx.stores.get("oidc_codes") or provider.codes
        return provider

    def oidc_error(exc: OIDCError) -> JSONResponse:
        return JSONResponse({"error": exc.error, "error_description": exc.description}, status_code=exc.status,
                            headers={"Cache-Control": "no-store"})

    @api.get("/oidc/.well-known/openid-configuration")
    def discovery() -> dict:
        return need_provider().discovery()

    @api.get("/oidc/jwks")
    def jwks() -> dict:
        return need_provider().keys.jwks()

    @api.get("/oidc/authorize")
    def authorize(request: Request):
        p = need_provider()
        q = dict(request.query_params)
        try:
            client = p.check_authorize(q)
        except OIDCError as exc:
            return _page(exc.status, "This sign-in can't continue", exc.description)
        user = ctx.signed_in(request)
        if user is None:
            return _page(401, "Sign in to the Valxel SDLC first",
                         "Grafana and MLflow open from a product's Support page, once you're signed in.")
        note = signer.loads(request.cookies.get(NOTE_COOKIE)) or {}
        wanted = (client.kind, client.product, client.plane) if client.kind == "mlflow" else (client.kind,)
        have = ((note.get("to"), note.get("product"), note.get("plane")) if client.kind == "mlflow"
                else (note.get("to"),))
        if note.get("login") != user.login or int(note.get("exp") or 0) < time.time() or have != wanted:
            return _page(403, f"Open {client.kind.capitalize()} from the Support page",
                         f"{client.kind.capitalize()} signs you in for one product at a time, chosen on its "
                         "Support page in the Valxel SDLC.")
        product_id = note["product"]
        a = granted(user, product_id)
        if not a.allows(client.kind):
            audit(user, product_id, "support.signin", {"client": client.id, "granted": False})
            return _page(403, "You don't have access", f"You can't open {client.kind} for {product_id}.")
        code = p.issue_code(client, q, claims_for(user, client, product_id, a.grafana or "Viewer"))
        audit(user, product_id, "support.signin", {"client": client.id, "granted": True,
                                                   **({"role": a.grafana} if client.kind == "grafana" else {}),
                                                   **({"plane": client.plane} if client.plane else {})})
        params = {"code": code, **({"state": q["state"]} if q.get("state") else {}), "iss": p.issuer}
        resp = RedirectResponse(f"{client.redirect_uri}?{urlencode(params)}", status_code=303)
        resp.delete_cookie(NOTE_COOKIE, path="/api/oidc")
        return resp

    @api.post("/oidc/token")
    async def token(request: Request):
        p = need_provider()
        form = {k: str(v) for k, v in (await request.form()).items()}
        basic = None
        header = request.headers.get("authorization", "")
        if header.lower().startswith("basic "):
            from urllib.parse import unquote

            try:
                cid, _, secret = base64.b64decode(header[6:]).decode().partition(":")
                basic = (unquote(cid), unquote(secret))
            except Exception:  # noqa: BLE001
                return oidc_error(OIDCError(401, "invalid_client", "Malformed client credentials"))
        try:
            return JSONResponse(p.token(form, basic), headers={"Cache-Control": "no-store", "Pragma": "no-cache"})
        except OIDCError as exc:
            return oidc_error(exc)

    @api.api_route("/oidc/userinfo", methods=["GET", "POST"])
    def userinfo(request: Request):
        p = need_provider()
        header = request.headers.get("authorization", "")
        if not header.lower().startswith("bearer "):
            return oidc_error(OIDCError(401, "invalid_token", "A bearer token is required"))
        try:
            return p.userinfo(header[7:])
        except OIDCError as exc:
            return oidc_error(exc)

    return api

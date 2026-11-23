"""Sign-in: GitHub, OIDC, passkeys, the dev login and the session."""

from __future__ import annotations

import os
import secrets
import time
from dataclasses import asdict
from types import SimpleNamespace

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse

from ...governance import GovernanceError
from ...intake import IntakeAgent
from ...web.auth import (
    SESSION_COOKIE,
    SESSION_TTL,
    NEXT_COOKIE,
    OIDC_COOKIE,
    STATE_COOKIE,
    User,
    new_state,
    session_payload,
)
from ..models import DevLoginIn


def build(ctx: SimpleNamespace) -> APIRouter:
    api = APIRouter()
    auth = ctx.auth
    signer = ctx.signer
    oauth = ctx.oauth
    eng = ctx.eng
    current_user = ctx.current_user
    lanes_for = ctx.lanes_for
    local_path = ctx.local_path

    # --- auth ---
    cognito = auth.cognito()
    oidc = auth.oidc()  # the team's own identity provider (T-148)

    @api.get("/auth/config")
    def auth_config() -> dict:
        """How to sign in: the buttons the sign-in page shows."""
        providers = ([{"id": "github", "label": "Sign in with GitHub"}] if auth.github_enabled and auth.mode != "dev" else [])
        providers += cognito.options() if cognito and auth.mode != "dev" else []
        providers += oidc.options() if oidc and auth.mode != "dev" else []
        out: dict = {"mode": auth.mode, "providers": providers}
        if auth.mode == "dev":  # offer the approver identities from governance as quick picks
            teams: set[str] = set()
            for pid in eng().services.settings.registry:
                try:
                    for names in eng().services.snapshot(pid).product.approvers.values():
                        teams |= {n for n in names if "/" in n}
                except GovernanceError:
                    continue
            out["teams"] = sorted(teams)
        return out

    def _signin(action: str, outcome: str, idp: str, actor: str = "", reason: str = "") -> None:
        """Sign-ins, refused ones too, to the audit trail (valxel_sdlc.audit_events)."""
        who = actor or "anonymous"
        eng().services.audit.record(action, actor=who, resource=("person", [who]), outcome=outcome,
                                    reason=reason, details={"idp": idp})

    def _set_session(resp, user: User) -> None:
        resp.set_cookie(SESSION_COOKIE, signer.dumps(session_payload(user)), max_age=SESSION_TTL,
                        httponly=True, samesite="lax", secure=auth.secure_cookies, path="/")

    @api.get("/auth/login")
    def login(next: str = "/", provider: str = "github", step_up: bool = False,  # noqa: A002 - query names
              hint: str = "") -> RedirectResponse:
        if provider in ("google", "email", "passkey", "oidc"):
            return oidc_login(provider, local_path(next), step_up or provider == "passkey", hint)
        if auth.mode == "dev" or not auth.github_enabled:
            raise HTTPException(404, "GitHub sign-in is not enabled")
        st = new_state()
        resp = RedirectResponse(oauth.authorize_url(st), status_code=302)
        resp.set_cookie(STATE_COOKIE, st, max_age=600, httponly=True, samesite="lax",
                        secure=auth.secure_cookies, path="/api/auth")
        resp.set_cookie(NEXT_COOKIE, local_path(next), max_age=600, httponly=True, samesite="lax",
                        secure=auth.secure_cookies, path="/api/auth")
        return resp

    @api.get("/auth/callback")
    def callback(request: Request, code: str = "", error: str = "") -> RedirectResponse:
        state_param = request.query_params.get("state", "")
        if error or not code:
            _signin("signin", "failure", "github", reason=(error or "no code")[:100])
            return RedirectResponse("/?signin=failed", status_code=302)
        if not state_param or state_param != request.cookies.get(STATE_COOKIE):
            _signin("signin", "denied", "github", reason="state mismatch")
            raise HTTPException(400, "Sign-in expired or was started elsewhere; try again")
        try:
            user = oauth.identity(oauth.exchange(code))
        except PermissionError as exc:
            _signin("signin", "denied", "github", reason="not allowed")
            raise HTTPException(401, str(exc)) from exc
        _signin("signin", "success", "github", user.login)
        resp = RedirectResponse(local_path(request.cookies.get(NEXT_COOKIE) or "/"), status_code=302)
        resp.delete_cookie(STATE_COOKIE, path="/api/auth")
        resp.delete_cookie(NEXT_COOKIE, path="/api/auth")
        _set_session(resp, user)
        return resp

    def _provider_for(option: str):
        """Which provider an option goes to: Cognito's Google, email and passkey; the team's own
        provider's sign-in, and its step-up when Cognito isn't there to offer a passkey."""
        if auth.mode == "dev":
            return None, ""
        if option == "oidc" or (option == "passkey" and cognito is None):
            return oidc, "oidc"
        return cognito, "cognito"

    def oidc_login(option: str, next_path: str, step_up: bool, hint: str) -> RedirectResponse:
        provider, idp = _provider_for(option)
        if provider is None:
            raise HTTPException(404, "That sign-in isn't enabled")
        from ...web.identity import SignInError, pkce

        st, nonce = new_state(), new_state()
        verifier, _ = pkce()
        try:
            url = provider.authorize_url(option="email" if option == "passkey" and idp == "cognito" else option,
                                         state=st, nonce=nonce, verifier=verifier, step_up=step_up, hint=hint[:254])
        except (SignInError, OSError) as exc:  # the provider's discovery document can't be read
            _signin("signin", "failure", idp, reason=str(exc)[:100])
            raise HTTPException(502, "The identity provider can't be reached; try again shortly") from exc
        resp = RedirectResponse(url, status_code=302)
        # Signed, so the callback trusts its own values; short-lived, and only sent to /api/auth.
        resp.set_cookie(OIDC_COOKIE, signer.dumps({"state": st, "nonce": nonce, "verifier": verifier, "idp": idp,
                                                   "next": next_path, "exp": int(time.time()) + 600}),
                        max_age=600, httponly=True, samesite="lax", secure=auth.secure_cookies, path="/api/auth")
        return resp

    @api.get("/auth/oidc/callback")
    def oidc_callback(request: Request, code: str = "", state: str = "", error: str = "") -> RedirectResponse:
        from ...web.identity import SignInError

        saved = signer.loads(request.cookies.get(OIDC_COOKIE))
        idp = "oidc" if (saved or {}).get("idp") == "oidc" else "cognito"
        provider = oidc if idp == "oidc" else cognito
        if error or not code:
            _signin("signin", "failure", idp, reason=(error or "no code")[:100])
            return RedirectResponse("/?signin=failed", status_code=302)
        if provider is None:
            raise HTTPException(404, "That sign-in isn't enabled")
        if not saved or not state or not secrets.compare_digest(state, str(saved.get("state", ""))):
            _signin("signin", "denied", idp, reason="state mismatch")
            raise HTTPException(400, "Sign-in expired or was started elsewhere; try again")
        try:
            who = provider.identity(code, verifier=saved["verifier"], nonce=saved["nonce"])
        except SignInError as exc:
            _signin("signin", "denied", idp, reason="identity refused")
            raise HTTPException(401, str(exc)) from exc
        except OSError as exc:  # the provider's token endpoint can't be reached
            _signin("signin", "failure", idp, reason=str(exc)[:100])
            raise HTTPException(502, "The identity provider can't be reached; try again shortly") from exc
        _signin("signin", "success", idp, who.email)
        # The person is their verified email: invitations, groups and approvals find them by it,
        # whichever way they signed in (GitHub's verified email links the same way). An identity
        # provider's groups come as idp:<group>, approvers a gate can name.
        user = User(login=who.email, name=who.name or who.email.split("@", 1)[0], avatar_url=who.picture,
                    email=who.email, idp=idp, strong=who.strong, teams=[f"idp:{g}" for g in who.groups])
        resp = RedirectResponse(local_path(saved.get("next") or "/"), status_code=302)
        resp.delete_cookie(OIDC_COOKIE, path="/api/auth")
        _set_session(resp, user)
        return resp

    @api.get("/auth/passkey")
    def add_passkey(user: User = Depends(current_user)) -> RedirectResponse:
        """Cognito's page for adding a passkey to the signed-in person's account."""
        if cognito is None or user.idp != "cognito":
            raise HTTPException(404, "Passkeys are for Google and email sign-in; GitHub has its own two-factor sign-in")
        return RedirectResponse(cognito.passkey_url(), status_code=302)

    @api.post("/auth/dev-login")
    def dev_login(body: DevLoginIn) -> JSONResponse:
        if auth.mode != "dev":
            raise HTTPException(404, "Dev sign-in is disabled")
        orgs = sorted(set(body.orgs) | {t.split("/", 1)[0] for t in body.teams})
        user = User(login=body.login, name=body.login, orgs=orgs, teams=list(body.teams), email=body.email.lower(),
                    idp="dev")
        resp = JSONResponse(asdict(user))
        _set_session(resp, user)
        _signin("signin", "success", "dev", user.login)
        return resp

    @api.post("/auth/logout")
    def logout(request: Request) -> JSONResponse:
        session = signer.loads(request.cookies.get(SESSION_COOKIE)) or {}
        if isinstance(session, dict) and session.get("login"):
            # Revoked on the server too: a copy of the cookie stops working (web/sessions.py).
            ctx.stores["sessions"].revoke(str(session.get("sid") or ""), str(session["login"]),
                                          by=str(session["login"]), reason="signed out")
            _signin("signout", "success", str(session.get("idp") or ""), str(session["login"]))
        resp = JSONResponse({"ok": True})
        resp.delete_cookie(SESSION_COOKIE, path="/")
        return resp

    @api.post("/auth/logout-everywhere")
    def logout_everywhere(user: User = Depends(current_user)) -> JSONResponse:
        """Every session of yours, on every device, including this one: for a lost laptop or a
        session you don't recognise."""
        ctx.stores["sessions"].revoke_login(user.login, by=user.login, reason="signed out everywhere")
        _signin("signout", "success", user.idp, user.login, reason="everywhere")
        resp = JSONResponse({"ok": True})
        resp.delete_cookie(SESSION_COOKIE, path="/")
        return resp

    @api.get("/me")
    def me(user: User = Depends(current_user)) -> dict:
        lanes = lanes_for(user)
        names = IntakeAgent(eng().services).lane_names(lanes)
        tel = eng().services.telemetry
        return {**asdict(user), "lanes": [{"id": k, "name": v} for k, v in names.items()],
                # The web app sends its spans to /otlp/v1/traces when this is on (web/src/telemetry.ts).
                "tracing": bool(tel is not None and tel.enabled and tel.cfg.endpoint),
                # The tool's owners (VALXEL_EVAL_APPROVERS): its health across products, drift, safe mode.
                "tool_owner": tool_owner(user)}

    # --- governance and features ---
    @api.get("/healthz")
    def healthz() -> dict:
        from ... import strict

        return {"ok": True, "mode": eng().services.settings.mode, "auth": auth.mode,
                "delivery": os.environ.get("VALXEL_DELIVERY", "stub"), "strict": strict.enabled()}

    @api.get("/healthz/watcher")
    def watcher_health() -> dict:
        """The watcher's housekeeping jobs that haven't succeeded for 26 hours (watcher_jobs.py), for
        monitoring: names, hours and error types only. Not for probes: it reads the shared notes."""
        stale = eng().jobs.stale(eng().watcher_jobs())
        return {"ok": not stale, "stale": stale}

    return api


def tool_owner(user) -> bool:
    owners = {t for t in os.environ.get("VALXEL_EVAL_APPROVERS", "").split(",") if t}
    return bool(owners & set(user.teams)) or user.login in owners

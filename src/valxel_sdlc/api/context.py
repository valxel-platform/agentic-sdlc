"""What every route group shares: the engine, sign-in, and who may do what.

create_app builds one Context; each module in api/routers takes it and returns its routes."""

from __future__ import annotations

import time
from types import SimpleNamespace
from typing import Any

from fastapi import HTTPException, Request

from ..engine import FeatureEngine
from ..intake import IntakeStore
from ..web.auth import (
    ESCALATION_GATE,
    SESSION_COOKIE,
    PRIVILEGED_TTL,
    AuthConfig,
    GitHubOAuth,
    SessionSigner,
    User,
    can_decide,
    needs_passkey,
    user_from_payload,
)

CONNECT_COOKIE = "valxel_connect"  # an OAuth tool connection's state, between Valxel and the vendor


def make_context(engine: FeatureEngine | None, auth: AuthConfig | None, oauth: GitHubOAuth | None) -> SimpleNamespace:
    """The shared state and helpers, as closures over this app's engine and sign-in settings."""
    state: dict[str, Any] = {"engine": engine, "pool": None}
    auth = auth or AuthConfig.from_env()
    signer = SessionSigner(auth.ring())
    oauth = oauth or GitHubOAuth(auth)
    stores: dict[str, Any] = {"intakes": IntakeStore()}  # Postgres once the engine has a database

    def eng() -> FeatureEngine:
        return state["engine"]

    def with_memberships(user: User) -> User:
        """The person's workspaces, roles and groups, read now (valxel_sdlc.workspaces): a member
        removed or suspended loses them on their next request, whatever their cookie says."""
        ws = eng().services.workspaces
        if ws is None:
            return user
        mine = ws.memberships(user.login, user.email)
        user.roles = {m["workspace"]: m["role"] for m in mine}
        user.products = {m["workspace"]: list(m.get("products") or []) for m in mine}
        user.groups = ws.groups_of({m["id"] for m in mine}, set(user.roles))
        return user

    def current_user(request: Request) -> User:
        user = user_from_payload(signer.loads(request.cookies.get(SESSION_COOKIE)))
        if user is None:
            raise HTTPException(401, "Sign in to continue")
        user = with_memberships(user)
        from ..workspaces import rank

        if (any(rank(r) >= rank("approver") for r in user.roles.values())
                and time.time() - (user.signed_in_at or 0) > PRIVILEGED_TTL):
            raise HTTPException(401, "Approvers and admins sign in again every 4 hours")
        if (request.method not in ("GET", "HEAD") and "application/json" not in request.headers.get("content-type", "")
                and request.headers.get("x-valxel-client") != "web"):
            # With SameSite=Lax cookies this blocks cross-site form posts. A request that isn't JSON (a
            # file upload, a DELETE) carries X-Valxel-Client instead, which a cross-site form can't send.
            raise HTTPException(415, "Send JSON")
        return user

    def lanes_for(user: User) -> list[str]:
        """Products the person may see: their GitHub org's (as before), and their workspaces' (all, or
        the products their profile lists)."""
        reg = eng().services.settings.registry
        if auth.mode == "dev" and not user.orgs and not user.roles:
            return list(reg)
        return [pid for pid, e in reg.items() if user.member_of(e.governance.org)
                or (e.workspace in user.roles and (not user.products.get(e.workspace)
                                                   or pid in user.products[e.workspace]))]

    def role_in(user: User, workspace: str) -> str:
        return user.roles.get(workspace, "")

    def may_start(user: User, product_id: str) -> None:
        """Viewers see a product but don't start work on it."""
        ws = eng().services.settings.registry[product_id].workspace
        if role_in(user, ws) == "viewer":
            raise HTTPException(403, "Viewers can't start features; ask an admin for the member role")

    def founder_of(user: User, workspace: str) -> None:
        """An admin of a self-serve workspace may start its products: their Product group approves
        the product gate, not the platform's council."""
        admin_of(user, workspace)
        if not (wsx().get(workspace) or {}).get("self_serve"):
            raise HTTPException(403, f"{workspace}'s products are started by the product council")

    def admin_of(user: User, workspace: str) -> str:
        from ..workspaces import rank

        role = role_in(user, workspace)
        if rank(role) < rank("admin"):
            raise HTTPException(403, f"Only {workspace}'s admins can do that")
        if not user.strong:
            raise HTTPException(403, "Changing a workspace needs a passkey sign-in. Choose 'Use a passkey' "
                                     "(add one first from your workspace page), then try again.")
        return role

    def item_approvers(fid: str, item: dict) -> list[str]:
        """Approvers for an inbox item: the gate's own list, or for an escalation the approvers of
        the gate that phase leads to."""
        if item.get("phase") == "fit" and item.get("approvers"):  # the design's and the architecture's approvers
            return list(item["approvers"])
        if item["type"] in ("gate", "handback") and item.get("approvers") is not None:
            return list(item["approvers"])
        if item["type"] == "handback":  # handback packets carry no approvers: use the lane's gate list
            lane = item.get("lane") or item.get("product_id")
        else:
            lane = item.get("product_id")
        view = eng().view(fid)
        snap = eng().services.snapshot(view["product_id"], view["governance"])
        lanes = snap.lanes()
        gate = item.get("gate") or ESCALATION_GATE.get(item.get("phase", ""), "g4")
        approvers = lanes.get(lane or view["product_id"], snap.product).approvers
        return list(approvers.get(gate) or (approvers.get("g5", []) if gate == "security" else []))

    def decorate(fid: str, item: dict, user: User) -> dict:
        if item["type"] == "external":  # CI or OpenChoreo, not a person
            return {**item, "approvers": [], "can_decide": False}
        approvers = item_approvers(fid, item)
        return {**item, "approvers": approvers, "can_decide": can_decide(user, {"approvers": approvers}),
                "needs_passkey": needs_passkey(user, approvers)}

    def local_path(path: str) -> str:
        """A page of this app to return to after sign-in, or "/": never another site."""
        ok = path.startswith("/") and not path.startswith(("//", "/\\", "/api/")) and "\n" not in path and "\r" not in path
        return path[:300] if ok else "/"

    def config_gate():
        if "config_gate" not in stores:
            from ..evals import ConfigGate

            stores["config_gate"] = ConfigGate.from_env(eng().services, eng().services.github)
        return stores["config_gate"]

    def wsx():
        ws = eng().services.workspaces
        if ws is None:
            raise HTTPException(404, "Workspaces are off")
        return ws

    def member_of(user: User, product_id: str) -> None:
        if product_id not in lanes_for(user):
            if product_id not in eng().services.settings.registry:
                raise HTTPException(404, f"Unknown product {product_id}")
            raise HTTPException(403, f"You aren't a member of {product_id}'s organization")

    return SimpleNamespace(state=state, auth=auth, signer=signer, oauth=oauth, stores=stores, eng=eng, with_memberships=with_memberships, current_user=current_user, lanes_for=lanes_for, role_in=role_in, may_start=may_start, founder_of=founder_of, admin_of=admin_of, item_approvers=item_approvers, decorate=decorate, local_path=local_path, config_gate=config_gate, wsx=wsx, member_of=member_of)

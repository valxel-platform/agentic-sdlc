"""HTTP API and web app.

Run with `uvicorn --factory valxel_sdlc.api.app:create_app`.

Everything under /api needs a signed-in user (GitHub sign-in, or dev sign-in for local use).
Decisions are recorded as the signed-in user and are allowed only for a gate's approvers; intents
can be submitted only to lanes whose GitHub org the person belongs to. The built web app (web/)
is served from / when present.

With DATABASE_URL set, threads, the usage ledger and the feature index persist in Postgres;
without it, everything is in memory (useful for tests and quick demos).
"""

from __future__ import annotations

import asyncio
import json
import os
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response, PlainTextResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

from .. import profiles
from ..duplicates import DuplicateIntent
from ..config import load_settings
from ..engine import FeatureEngine, InMemoryIndex, PostgresIndex
from ..gateway import InMemoryLedger, PolicyViolation, PostgresLedger
from ..governance import GovernanceError
from ..graph import InvalidDecision
from ..intake import IntakeAgent, IntakeStore
from ..services import build_services, flag_modes, integration_modes
from ..worksync import PostgresLinkStore, WorkSync
from ..web.auth import (
    ESCALATION_GATE,
    SESSION_COOKIE,
    SESSION_TTL,
    STATE_COOKIE,
    AuthConfig,
    GitHubOAuth,
    SessionSigner,
    User,
    can_decide,
    new_state,
    session_payload,
    user_from_payload,
)

WEB_DIST = Path(
    os.environ.get("VALXEL_WEB_DIST")
    or Path(os.environ.get("VALXEL_ROOT") or Path(__file__).resolve().parents[3]) / "web" / "dist"
)


def record_review_effort(engine, kind: str, event: dict) -> None:
    """A person's review or review comment on one of the tool's pull requests counts as their
    effort in the pilot report (bots and the tool's own app don't)."""
    if kind not in ("pull_request_review", "pull_request_review_comment") or event.get("action") not in (
            "submitted", "created"):
        return
    sender = event.get("sender") or {}
    login = str(sender.get("login") or "")
    if not login or sender.get("type") == "Bot" or login.endswith("[bot]"):
        return
    ref = ((event.get("pull_request") or {}).get("head") or {}).get("ref") or ""
    if not ref.startswith("feature/") or getattr(engine.services, "effort", None) is None:
        return
    fid = ref.split("/", 1)[1]
    from ..pilot import ESTIMATED_MINUTES

    what = "pr_review" if kind == "pull_request_review" else "pr_comment"
    item = event.get("review") or event.get("comment") or {}
    engine.services.effort.add({"feature_id": fid, "product_id": fid.split("-FEAT-", 1)[0], "kind": what,
                                "who": login, "ref": str(item.get("id") or ""), "minutes": ESTIMATED_MINUTES[what],
                                "detail": {"state": item.get("state", ""), "pr": (event.get("pull_request") or {}).get("html_url", "")}})


class FeatureIn(BaseModel):
    product_id: str = Field(examples=["CLN"])
    title: str = Field(min_length=3, max_length=200)
    description: str = ""
    budget_usd: float | None = Field(default=None, gt=0)
    simulate_faults: dict[str, int] = Field(default_factory=dict,
                                            description="Stub mode only: phase -> defective attempts")
    not_duplicate_reason: str = Field("", max_length=1000,
                                      description="Required when the request looks like an existing feature")
    initiative_id: str = Field("", description="The approved initiative this feature delivers; empty = unplanned")
    pilot: bool = Field(False, description="Measure it in the pilot report, with the shadow modes on")
    profile: str = Field("", pattern="^(|feature|express)$",
                         description="How much of the pipeline it goes through; empty = the product's default")


class ReadingIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: float
    start: str = Field(min_length=10, max_length=40)
    end: str = Field(min_length=10, max_length=40)
    n: int | None = Field(default=None, ge=0)
    k: int | None = Field(default=None, ge=0)


class InitiativeIn(BaseModel):
    product_id: str
    title: str = Field(min_length=3, max_length=200)
    request: str = Field(min_length=10, max_length=4000, description="The outcome wanted and what it involves")
    measure: str = Field("", max_length=500)
    horizon: str = Field("", pattern="^(|now|next|later)$")
    research: str = Field("quick", pattern="^(quick|thorough|own)$",
                          description="quick or thorough web research, or 'own': your research is in the request")


class ProblemIn(BaseModel):
    product_id: str
    title: str = Field(min_length=3, max_length=200)
    description: str = Field("", max_length=4000)
    component: str = Field("", max_length=100)
    severity: str = Field("sev3", pattern="^sev[1-4]$")


class StartCandidateIn(BaseModel):
    index: int = Field(ge=0)
    not_duplicate_reason: str = Field("", max_length=1000)


class InceptionIn(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    org: str = Field("", max_length=100, pattern=r"^$|^[A-Za-z0-9][A-Za-z0-9-]*$")
    vision: str = Field(min_length=20, max_length=8000, description="The idea: the problem, for whom, and what changes")
    goals: list[str] = Field(default_factory=list, description="'<outcome> (measure: <metric with a number>)'")
    users: list[str] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)
    budget_usd: float = Field(0.0, ge=0)
    product_id: str = Field("", pattern=r"^$|^[A-Z]{2,6}$", description="An existing product to describe, or a proposed id")
    research: str = Field("quick", pattern="^(quick|thorough|own)$",
                          description="quick or thorough web research, or 'own': your research is in the vision")


class ReleaseIn(BaseModel):
    product_id: str
    title: str = Field(min_length=3, max_length=200)
    model: str = Field("launch", pattern="^(launch|train)$")
    features: list[str] = Field(min_length=1)
    target_date: str = Field("", max_length=20)
    initiative: str = ""


class NotifyIn(BaseModel):
    email: str | None = Field(default=None, max_length=200, pattern=r"^$|^[^@\s]+@[^@\s]+\.[^@\s]+$")
    needed: bool | None = None
    staging: bool | None = None


class WatchIn(BaseModel):
    on: bool = True


class ItemRef(BaseModel):
    feature_id: str
    interrupt_id: str


class BatchIn(BaseModel):
    items: list[ItemRef] = Field(min_length=1, max_length=50)
    active_minutes: float = Field(0.0, ge=0)


class RequestIn(BaseModel):
    description: str = Field(min_length=10, max_length=8000)


class StartIn(BaseModel):
    indexes: list[int] = Field(min_length=1, max_length=50)
    order: str = Field("together", pattern="^(together|sequence)$")


class LineComment(BaseModel):
    quote: str = Field("", max_length=1000, description="The line the comment is about, as written")
    text: str = Field(min_length=1, max_length=2000)


class DecisionIn(BaseModel):
    decision: str = Field(examples=["approve", "revise", "takeover", "handback", "resolved", "retry", "abort"])
    feedback: str = ""
    active_minutes: float = Field(default=0.0, ge=0)
    budget_usd: float | None = Field(default=None, gt=0, description="Escalations only: raise the feature budget")
    artifact_content: str | None = Field(default=None, description="Escalations and hand-backs: the person's own "
                                        "version of the phase's document")
    remember: bool = Field(False, description="Learn from this correction: propose a lesson for future features")
    lesson: str | None = Field(default=None, max_length=400, description="The lesson in the person's words "
                               "(implies remember)")
    action_ids: list[str] | None = Field(default=None, description="Incidents only: which proposed actions to approve")
    reason: str | None = Field(default=None, description="Why it's sent back (revise, retry, abort): one of the "
                               "pilot's reasons, e.g. missing_requirement, wrong_approach, quality")
    override_reason: str | None = Field(default=None, max_length=1000, description="G6 only: release although the "
                                        "traceability matrix has gaps (or tracker drift that blocks), and why. "
                                        "Only the product's traceability approvers may.")
    retire: list[str] | None = Field(default=None, description="Initiative reviews, stop: the proposed features to "
                                     "retire (each opens a retire intent)")
    review_by: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$",
                                  description="Initiative reviews, continue: the next review date")
    ui: bool | None = Field(default=None, description="G2 only: whether UX design runs (it changes what people see)")
    comments: list[LineComment] | None = Field(default=None, max_length=50,
                                               description="Revise: comments on specific lines of the document")


class SubmitIn(BaseModel):
    pilot: bool = Field(False, description="Measure it in the pilot report, with the shadow modes on")
    profile: str = Field("", pattern="^(|feature|express)$",
                         description="How much of the pipeline it goes through; empty = the product's default")


class DevLoginIn(BaseModel):
    login: str = Field(min_length=1, max_length=39)
    teams: list[str] = Field(default_factory=list)
    orgs: list[str] = Field(default_factory=list)


class MessageIn(BaseModel):
    text: str = Field(min_length=1, max_length=4000)


class DraftIn(BaseModel):
    product_id: str | None = None
    title: str | None = Field(default=None, max_length=200)
    problem: str | None = None
    users: str | None = None
    success_metric: str | None = None
    ui: bool | None = Field(default=None, description="Whether the intent changes what people see")
    experience: dict[str, str] | None = Field(default=None, description="The experience brief: where, main_task, "
                                              "context, look")
    size: str | None = Field(default=None, pattern="^(feature|initiative|product)$")
    horizon: str | None = None
    research: str | None = None
    name: str | None = Field(default=None, max_length=120)
    org: str | None = Field(default=None, max_length=100)
    budget_usd: float | None = Field(default=None, ge=0)


class EvalRunIn(BaseModel):
    ref: str = Field(..., description="The config repo commit to evaluate")
    base_ref: str = Field("", description="The commit to compare with (default: the config in use)")


def create_engine_from_env() -> tuple[FeatureEngine, Any]:
    settings = load_settings()
    if settings.database_url:
        from langgraph.checkpoint.postgres import PostgresSaver
        from psycopg.rows import dict_row
        from psycopg_pool import ConnectionPool

        pool = ConnectionPool(settings.database_url, max_size=10,
                              kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row})
        saver = PostgresSaver(pool)
        saver.setup()
        from ..integrations.catalog import PostgresCatalog
        from ..ops.signals import PostgresSignalStore

        services = build_services(settings, ledger=PostgresLedger(pool), catalog=PostgresCatalog(pool))
        from ..evals import PostgresEvalStore

        services.evals = PostgresEvalStore(pool)
        services.signals = PostgresSignalStore(pool)
        from ..outcomes.store import PostgresReadingStore

        services.readings = PostgresReadingStore(pool)
        from ..ops.cost import PostgresCostReviews

        services.cost_reviews = PostgresCostReviews(pool)
        from ..finops.ledger import PostgresCostLedger

        services.cost_ledger = PostgresCostLedger(pool)
        from ..ops.alarms import PostgresAlarmBoard

        services.alarms = PostgresAlarmBoard(pool)
        from ..ops.changes import PostgresChangeLog

        services.changes = PostgresChangeLog(pool)
        from ..ops.servicenow import PostgresReceipts

        services.change_receipts = PostgresReceipts(pool)
        from ..ops.inventory import PostgresSnapshots

        services.inventory_snapshots = PostgresSnapshots(pool)
        from ..pilot import PostgresEffortStore

        services.effort = PostgresEffortStore(pool)
        from ..lessons import PostgresLessonStore

        services.lessons = PostgresLessonStore(pool)
        from ..provision_queue import PostgresProvisionQueue

        services.provision_queue = PostgresProvisionQueue(pool)
        work = WorkSync(services, PostgresLinkStore(pool),
                        os.environ.get("VALXEL_PUBLIC_URL") or "http://localhost:8080")
        from ..cluster import PostgresCluster
        from ..events import PostgresBus

        return FeatureEngine(services, saver, PostgresIndex(pool), work=work,
                             bus=PostgresBus(pool, settings.database_url),
                             cluster=PostgresCluster(pool, settings.database_url)), pool
    from langgraph.checkpoint.memory import InMemorySaver

    services = build_services(settings, ledger=InMemoryLedger())
    return FeatureEngine(services, InMemorySaver(), InMemoryIndex()), None


def create_app(engine: FeatureEngine | None = None, auth: AuthConfig | None = None,
               oauth: GitHubOAuth | None = None, web_dist: Path | None = WEB_DIST) -> FastAPI:
    state: dict[str, Any] = {"engine": engine, "pool": None}
    auth = auth or AuthConfig.from_env()
    signer = SessionSigner(auth.ring())
    oauth = oauth or GitHubOAuth(auth)
    stores: dict[str, Any] = {"intakes": IntakeStore()}  # Postgres once the engine has a database

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        if state["engine"] is None:
            state["engine"], state["pool"] = create_engine_from_env()
            from .. import strict

            if strict.enabled():  # refuse to serve while anything would quietly run as a stand-in
                strict.check(state["engine"].services, auth_mode=auth.mode)
            if state["pool"] is not None:
                from ..intake import PostgresIntakeStore

                stores["intakes"] = PostgresIntakeStore(state["pool"])
            # Polls CI and OpenChoreo for features waiting on them; webhooks make it immediate.
            state["engine"].start_watcher(float(os.environ.get("VALXEL_WATCH_INTERVAL_S", "30")))
        yield
        state["engine"].stop_watcher()
        if state["pool"] is not None:
            state["pool"].close()

    app = FastAPI(title="Valxel SDLC", version="0.2.0", lifespan=lifespan)
    api = APIRouter(prefix="/api")

    def eng() -> FeatureEngine:
        return state["engine"]

    def current_user(request: Request) -> User:
        user = user_from_payload(signer.loads(request.cookies.get(SESSION_COOKIE)))
        if user is None:
            raise HTTPException(401, "Sign in to continue")
        if (request.method not in ("GET", "HEAD") and "application/json" not in request.headers.get("content-type", "")
                and request.headers.get("x-valxel-client") != "web"):
            # With SameSite=Lax cookies this blocks cross-site form posts. A request that isn't JSON (a
            # file upload, a DELETE) carries X-Valxel-Client instead, which a cross-site form can't send.
            raise HTTPException(415, "Send JSON")
        return user

    def lanes_for(user: User) -> list[str]:
        reg = eng().services.settings.registry
        if auth.mode == "dev" and not user.orgs:
            return list(reg)
        return [pid for pid, e in reg.items() if user.member_of(e.governance.org)]

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
        return {**item, "approvers": approvers, "can_decide": can_decide(user, {"approvers": approvers})}

    # --- auth ---
    @api.get("/auth/config")
    def auth_config() -> dict:
        out: dict = {"mode": auth.mode}
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

    def _set_session(resp, user: User) -> None:
        resp.set_cookie(SESSION_COOKIE, signer.dumps(session_payload(user)), max_age=SESSION_TTL,
                        httponly=True, samesite="lax", secure=auth.secure_cookies, path="/")

    @api.get("/auth/login")
    def login() -> RedirectResponse:
        if auth.mode != "github":
            raise HTTPException(404, "GitHub sign-in is not enabled")
        st = new_state()
        resp = RedirectResponse(oauth.authorize_url(st), status_code=302)
        resp.set_cookie(STATE_COOKIE, st, max_age=600, httponly=True, samesite="lax",
                        secure=auth.secure_cookies, path="/api/auth")
        return resp

    @api.get("/auth/callback")
    def callback(request: Request, code: str = "", error: str = "") -> RedirectResponse:
        state_param = request.query_params.get("state", "")
        if error or not code:
            return RedirectResponse("/?signin=failed", status_code=302)
        if not state_param or state_param != request.cookies.get(STATE_COOKIE):
            raise HTTPException(400, "Sign-in expired or was started elsewhere; try again")
        try:
            user = oauth.identity(oauth.exchange(code))
        except PermissionError as exc:
            raise HTTPException(401, str(exc)) from exc
        resp = RedirectResponse("/", status_code=302)
        resp.delete_cookie(STATE_COOKIE, path="/api/auth")
        _set_session(resp, user)
        return resp

    @api.post("/auth/dev-login")
    def dev_login(body: DevLoginIn) -> JSONResponse:
        if auth.mode != "dev":
            raise HTTPException(404, "Dev sign-in is disabled")
        orgs = sorted(set(body.orgs) | {t.split("/", 1)[0] for t in body.teams})
        user = User(login=body.login, name=body.login, orgs=orgs, teams=list(body.teams))
        resp = JSONResponse(asdict(user))
        _set_session(resp, user)
        return resp

    @api.post("/auth/logout")
    def logout() -> JSONResponse:
        resp = JSONResponse({"ok": True})
        resp.delete_cookie(SESSION_COOKIE, path="/")
        return resp

    @api.get("/me")
    def me(user: User = Depends(current_user)) -> dict:
        lanes = lanes_for(user)
        names = IntakeAgent(eng().services).lane_names(lanes)
        return {**asdict(user), "lanes": [{"id": k, "name": v} for k, v in names.items()]}

    # --- governance and features ---
    @api.get("/healthz")
    def healthz() -> dict:
        from .. import strict

        return {"ok": True, "mode": eng().services.settings.mode, "auth": auth.mode,
                "delivery": os.environ.get("VALXEL_DELIVERY", "stub"), "strict": strict.enabled()}

    # --- production signals ---
    def bearer_ok(request: Request) -> None:

        from ..secret_ring import bearer_ok as ring_bearer_ok, values

        if not values("VALXEL_SIGNALS_TOKEN"):
            raise HTTPException(404, "Signal webhooks are not configured")
        if not ring_bearer_ok("VALXEL_SIGNALS_TOKEN", request.headers.get("authorization", "")):
            raise HTTPException(401, "Bad token")

    # Webhooks answer within seconds (EventBridge gives up after 5): the signal is recorded and
    # on-call paged before the response, off the event loop; triage runs in the background.
    async def ingest(signals: list[dict]) -> dict:
        touched = await run_in_threadpool(lambda: eng().ingest(signals, background=True))
        return {"signals": len(signals), "incidents": touched}

    async def alarm_changes(signals: list[dict]) -> dict:
        return await run_in_threadpool(lambda: eng().alarm_changes(signals, background=True))

    @api.post("/signals/alertmanager", status_code=202)
    async def alertmanager(request: Request) -> dict:
        """Alertmanager webhook receiver (http_config.authorization: Bearer VALXEL_SIGNALS_TOKEN)."""
        from ..ops.signals import from_alertmanager

        bearer_ok(request)
        try:
            payload = json.loads(await request.body() or b"{}")
        except ValueError as exc:
            raise HTTPException(400, "Body is not JSON") from exc
        return await ingest(from_alertmanager(payload))

    @api.post("/signals/sentry", status_code=202)
    async def sentry(request: Request) -> dict:
        """Sentry internal integration webhook, verified with its client secret (Sentry-Hook-Signature)."""
        import hashlib
        import hmac

        from ..ops.signals import from_sentry

        from ..secret_ring import any_matches, values

        if not values("VALXEL_SENTRY_CLIENT_SECRET"):
            raise HTTPException(404, "Sentry webhooks are not configured")
        body = await request.body()
        got = request.headers.get("sentry-hook-signature", "")
        if not any_matches("VALXEL_SENTRY_CLIENT_SECRET", lambda secret: hmac.compare_digest(
                hmac.new(secret.encode(), body, hashlib.sha256).hexdigest(), got)):
            raise HTTPException(401, "Bad signature")
        try:
            payload = json.loads(body or b"{}")
        except ValueError as exc:
            raise HTTPException(400, "Body is not JSON") from exc
        return await ingest(from_sentry(payload, request.headers.get("sentry-hook-resource", "")))

    @api.post("/signals/ticket", status_code=202)
    async def ticket(request: Request) -> dict:
        """A support tool's webhook: {product_id, title, description, component?, severity?, url?, id?}."""
        from ..ops.signals import from_ticket

        bearer_ok(request)
        try:
            payload = json.loads(await request.body() or b"{}")
        except ValueError as exc:
            raise HTTPException(400, "Body is not JSON") from exc
        return await ingest(from_ticket(payload))

    def oncall(user: User, product_id: str) -> None:
        """Only the product's on-call (who decide incidents) may merge or split them."""
        from ..ops.incident import oncall_approvers

        member_of(user, product_id)
        if not can_decide(user, {"approvers": oncall_approvers(eng().services.snapshot(product_id).product)}):
            raise HTTPException(403, "Only the product's on-call can merge or split incidents")

    @api.get("/signals/unmatched")
    def unmatched_signals(limit: int = 100, user: User = Depends(current_user)) -> dict:
        """Signals that matched no product (the last 30 days, newest first), with why: a mislabelled
        alert, an unregistered component. They may come from any product, so only members of the
        platform's org see them."""
        if not user.member_of(eng().services.settings.platform.org):
            raise HTTPException(403, "Only members of the platform's organization see unmatched signals")
        rows = eng().unmatched_signals(max(1, min(limit, 500)))
        keep = ("source", "title", "component", "product_id", "severity", "env", "status", "fingerprint", "url",
                "labels", "reason", "received_at", "at")
        return {"count": len(rows), "signals": [{k: r.get(k) for k in keep if k in r} for r in rows]}

    @api.post("/incidents/{incident_id}/merge")
    def merge_incident(incident_id: str, body: dict, user: User = Depends(current_user)) -> dict:
        """On-call: this incident is the same failure as another; its signals move there."""
        into = str(body.get("into") or "")
        product = eng()._state(incident_id).get("product_id", "") if incident_id else ""
        if not product or eng()._state(into).get("product_id") != product:
            raise HTTPException(400, "Merge into an open incident of the same product")
        oncall(user, product)
        return eng().merge_incidents(incident_id, into, approver=user.login, reason=str(body.get("reason") or ""))

    @api.post("/incidents/{incident_id}/split")
    def split_incident(incident_id: str, body: dict, user: User = Depends(current_user)) -> dict:
        """On-call: a signal was joined to this incident wrongly; it opens its own."""
        product = eng()._state(incident_id).get("product_id", "")
        if not product:
            raise HTTPException(404, f"Unknown incident {incident_id}")
        oncall(user, product)
        try:
            return eng().split_signal(incident_id, str(body.get("fingerprint") or ""), approver=user.login)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc

    @api.post("/signals/openchoreo", status_code=202)
    async def openchoreo_alert(request: Request) -> dict:
        """An OpenChoreo notification channel (type webhook, no payloadTemplate, header
        Authorization: Bearer VALXEL_SIGNALS_TOKEN). Budget alerts from the FinOps module arrive
        as cost signals; log and metric alerts as ordinary ones."""
        from ..ops.signals import from_openchoreo

        bearer_ok(request)
        try:
            payload = json.loads(await request.body() or b"{}")
        except ValueError as exc:
            raise HTTPException(400, "Body is not JSON") from exc
        # Production alerts open incidents; the rest are kept for G6, like CloudWatch alarms.
        return await alarm_changes(from_openchoreo(payload))

    @api.post("/signals/glitchtip/{product_id}", status_code=202)
    async def glitchtip_alert(product_id: str, request: Request) -> dict:
        """A product's GlitchTip alert webhook recipient, with the product's token in the URL
        (`valxel glitchtip-token <product>`). Production issues open or join incidents; the rest
        are kept for G6. The issue's trace id is read back when the product has an API token."""
        import hmac

        from ..ops import error_tracking as et

        from ..secret_ring import values

        got = request.query_params.get("token", "")
        expected = [et.token_for(product_id, v) for v in values("VALXEL_SIGNALS_TOKEN")]
        if not expected or not any(hmac.compare_digest(got, e) for e in expected):
            raise HTTPException(401, "Bad token")
        try:
            snap = await run_in_threadpool(eng().services.snapshot, product_id)
        except Exception as exc:
            raise HTTPException(404, f"Unknown product {product_id}") from exc
        choice = (snap.product.runtime or {}).get("error_tracking") or {}
        if choice.get("provider") != "glitchtip":
            raise HTTPException(404, f"{product_id} doesn't use GlitchTip")
        try:
            payload = json.loads(await request.body() or b"{}")
        except ValueError as exc:
            raise HTTPException(400, "Body is not JSON") from exc
        token = et.api_token(product_id)
        sigs = await run_in_threadpool(lambda: [et.enrich(s, str(choice.get("url") or ""), token)
                                                for s in et.from_glitchtip(payload, product_id)])
        return await alarm_changes(sigs)

    @api.post("/signals/cloudwatch", status_code=202)
    async def cloudwatch_alarm(request: Request) -> dict:
        """CloudWatch alarm state changes from each data plane's AlarmRouting (an EventBridge API
        destination sending Authorization: Bearer VALXEL_SIGNALS_TOKEN). Production alarms open and
        resolve incidents; every alarm's state is kept for G6."""
        from ..ops.signals import from_cloudwatch

        bearer_ok(request)
        try:
            event = json.loads(await request.body() or b"{}")
        except ValueError as exc:
            raise HTTPException(400, "Body is not JSON") from exc
        return await alarm_changes(from_cloudwatch(event))

    @api.post("/signals/inspector", status_code=202)
    async def inspector_finding(request: Request) -> dict:
        """Amazon Inspector findings on running images (ECR enhanced scanning, continuous), from an
        EventBridge rule on 'Inspector2 Finding' through an API destination sending Authorization:
        Bearer VALXEL_SIGNALS_TOKEN. High and critical vulnerabilities open incidents for the component."""
        from ..ops.signals import from_inspector

        bearer_ok(request)
        try:
            event = json.loads(await request.body() or b"{}")
        except ValueError as exc:
            raise HTTPException(400, "Body is not JSON") from exc
        return await ingest(from_inspector(event))

    @api.post("/signals/aws-budgets", status_code=202)
    async def aws_budgets(request: Request) -> JSONResponse:
        """AWS Budgets through an SNS topic with an HTTPS subscription to
        https://valxel:<VALXEL_SIGNALS_TOKEN>@<host>/api/signals/aws-budgets. Only topics listed in
        VALXEL_AWS_BUDGET_TOPICS are accepted; their subscription is confirmed here."""
        import base64
        import hmac

        from ..ops.signals import from_aws_budget

        from ..secret_ring import any_matches, values

        topics = {t.strip() for t in os.environ.get("VALXEL_AWS_BUDGET_TOPICS", "").split(",") if t.strip()}
        if not values("VALXEL_SIGNALS_TOKEN") or not topics:
            raise HTTPException(404, "AWS Budgets signals are not configured")
        auth = request.headers.get("authorization", "")

        def accepted(token: str) -> bool:
            basic = "Basic " + base64.b64encode(f"valxel:{token}".encode()).decode()
            return hmac.compare_digest(auth, f"Bearer {token}") or hmac.compare_digest(auth, basic)

        if not any_matches("VALXEL_SIGNALS_TOKEN", accepted):
            # SNS sends its credentials only after a challenge.
            return JSONResponse({"detail": "Bad credentials"}, status_code=401,
                                headers={"WWW-Authenticate": 'Basic realm="valxel"'})
        try:
            msg = json.loads(await request.body() or b"{}")
        except ValueError as exc:
            raise HTTPException(400, "Body is not JSON") from exc
        topic = next((t for t in sorted(topics) if t == msg.get("TopicArn")), None)
        if topic is None:
            raise HTTPException(403, "Unknown topic")
        if msg.get("Type") == "SubscriptionConfirmation":
            await run_in_threadpool(confirm_sns, str(msg.get("Token") or ""), topic)
            return JSONResponse({"confirmed": topic}, status_code=202)
        if msg.get("Type") != "Notification":
            return JSONResponse({"ignored": msg.get("Type")}, status_code=202)
        reg = list(eng().services.settings.registry)
        return JSONResponse(await ingest(from_aws_budget(str(msg.get("Message") or ""), reg, msg.get("Timestamp", ""))),
                            status_code=202)

    def confirm_sns(token: str, topic: str) -> None:
        """Confirm the subscription with SNS in the allowed topic's own region. The URL is built
        here, from the configured topic, never taken from the request."""
        import re
        from urllib.parse import urlencode
        from urllib.request import urlopen

        parts = topic.split(":")
        region = parts[3] if len(parts) == 6 and parts[2] == "sns" else ""
        if not re.fullmatch(r"[a-z]{2}(-[a-z]+)+-\d", region) or not re.fullmatch(r"[A-Za-z0-9+/=_-]{16,2048}", token):
            raise HTTPException(400, "Not a subscription confirmation for an SNS topic")
        query = urlencode({"Action": "ConfirmSubscription", "TopicArn": topic, "Token": token})
        with urlopen(f"https://sns.{region}.amazonaws.com/?{query}", timeout=15) as resp:  # noqa: S310
            resp.read()

    @api.post("/webhooks/github", status_code=202)
    async def github_webhook(request: Request) -> dict:
        """CI and pull-request events from the product orgs' GitHub App. Verified with the app's
        webhook secret; only triggers a check, so a forged event could do no more than that."""
        import hashlib
        import hmac

        from ..secret_ring import any_matches, values

        if not values("VALXEL_GITHUB_WEBHOOK_SECRET"):
            raise HTTPException(404, "Webhooks are not configured")
        body = await request.body()
        got = request.headers.get("x-hub-signature-256", "")
        if not any_matches("VALXEL_GITHUB_WEBHOOK_SECRET", lambda secret: hmac.compare_digest(
                "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest(), got)):
            raise HTTPException(401, "Bad signature")
        try:
            event = json.loads(body or b"{}")
        except ValueError as exc:
            raise HTTPException(400, "Body is not JSON") from exc
        branches = {
            (event.get("pull_request") or {}).get("head", {}).get("ref"),
            (event.get("check_suite") or {}).get("head_branch"),
            ((event.get("check_run") or {}).get("check_suite") or {}).get("head_branch"),
            *[b.get("name") for b in event.get("branches") or [] if isinstance(b, dict)],
        }
        features = {b.split("/", 1)[1] for b in branches if b and b.startswith("feature/")}
        gate = config_gate()
        pr = event.get("pull_request") or {}
        if (gate is not None and request.headers.get("x-github-event") == "pull_request"
                and (event.get("repository") or {}).get("full_name") == gate.config_repo
                and event.get("action") in ("opened", "synchronize", "reopened") and pr.get("head", {}).get("sha")):
            # A change to the tool's config: replay past documents under it before it merges.
            run = gate.start(pr["head"]["sha"], (pr.get("base") or {}).get("sha", ""),
                             requested_by=(pr.get("user") or {}).get("login", ""))
            return {"event": "pull_request", "eval_run": run["id"]}
        record_review_effort(eng(), request.headers.get("x-github-event", ""), event)
        waiting = set(eng().waiting_on_external())
        poked = [fid for fid in sorted(features & waiting if features else waiting) if eng().poke(fid)]
        return {"event": request.headers.get("x-github-event", ""), "resumed": poked}

    def config_gate():
        if "config_gate" not in stores:
            from ..evals import ConfigGate

            stores["config_gate"] = ConfigGate.from_env(eng().services, eng().services.github)
        return stores["config_gate"]

    @api.post("/evals/runs", status_code=202)
    def start_eval_run(body: EvalRunIn, user: User = Depends(current_user)) -> dict:
        """Replay past documents under the config at a commit of the config repo and compare."""
        gate = config_gate()
        if gate is None:
            raise HTTPException(404, "Evals of config changes are off: set VALXEL_CONFIG_REPO")
        allowed = {t for t in os.environ.get("VALXEL_EVAL_APPROVERS", "").split(",") if t}
        if not allowed & set(user.teams) and user.login not in allowed:
            raise HTTPException(403, "Only the teams in VALXEL_EVAL_APPROVERS can start an eval run")
        return gate.start(body.ref, body.base_ref, requested_by=user.login)

    @api.get("/evals/runs")
    def eval_runs(user: User = Depends(current_user)) -> list[dict]:
        gate = config_gate()
        runs = list(gate.runs.values()) if gate is not None else []
        return [{k: r.get(k) for k in ("id", "repo", "ref", "base_ref", "status", "started_at", "finished_at", "requested_by")}
                for r in sorted(runs, key=lambda r: r["started_at"], reverse=True)]

    @api.get("/evals/runs/{run_id}")
    def eval_run(run_id: str, user: User = Depends(current_user)) -> dict:
        gate = config_gate()
        if gate is None or run_id not in gate.runs:
            raise HTTPException(404, f"No eval run {run_id}")
        return gate.runs[run_id]

    @api.get("/products")
    def products(user: User = Depends(current_user)) -> dict:
        services = eng().services
        out = {}
        for pid, entry in services.settings.registry.items():
            row = {"kind": entry.kind, "governance_repo": entry.governance.full_name,
                   "ref": entry.governance.ref, "data_class": entry.data_class}
            try:
                snap = services.snapshot(pid)
                p = snap.product
                modes = integration_modes(services)
                row |= {"tools": {cap: ({**spec, "mode": modes.get(spec["provider"], "stub")} if "provider" in spec
                                        else {**spec, "sources": {n: {**c, "mode": modes.get(n, "stub")}
                                                                  for n, c in (spec.get("sources") or {}).items()}})
                                  for cap, spec in p.tools.items()}}
                if p.runtime:  # flags: this product's Flipt, in-memory flags, or none
                    mode = flag_modes(services, [pid]).get(pid, "stub")
                    row["tools"]["flags"] = {**row["tools"].get("flags", {"provider": "none"}),
                                             "mode": "live" if mode == "flipt" else mode}
                row |= {"name": p.name, "repos": [r.name for r in p.repos], "budget_usd": p.budget_usd,
                        "depends_on": list(p.depends_on), "runtime": p.runtime,
                        "constitution_version": snap.version, "articles": list(snap.constitution.article_ids),
                        # The workflow profiles its people may start work with, and the default.
                        "profiles": profiles.allowed(p.workflow, p.data_class),
                        "default_profile": profiles.default(p.workflow),
                        "ok": True}
            except GovernanceError as exc:
                row |= {"ok": False, "error": str(exc)}
            out[pid] = row
        return out

    @api.post("/features", status_code=201)
    def create_feature(body: FeatureIn, user: User = Depends(current_user)) -> dict:
        if body.product_id not in lanes_for(user):
            if body.product_id not in eng().services.settings.registry:
                raise HTTPException(404, f"Unknown product {body.product_id}")
            raise HTTPException(403, f"You aren't a member of {body.product_id}'s organization")
        try:
            fid = eng().create(product_id=body.product_id, title=body.title, description=body.description,
                               created_by=user.login, simulate_faults=body.simulate_faults,
                               budget_usd=body.budget_usd, background=True,
                               not_duplicate_reason=body.not_duplicate_reason, initiative_id=body.initiative_id,
                               pilot=body.pilot, profile=body.profile)
        except DuplicateIntent as exc:
            raise HTTPException(409, {"message": str(exc), "similar": exc.matches}) from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        except GovernanceError as exc:
            raise HTTPException(424, f"Product governance could not be loaded: {exc}") from exc
        except PolicyViolation as exc:
            raise HTTPException(403, str(exc)) from exc
        return {"feature_id": fid}

    # --- incidents ---
    @api.post("/incidents", status_code=201)
    def report_problem(body: ProblemIn, user: User = Depends(current_user)) -> dict:
        from ..ops.signals import from_ticket

        member_of(user, body.product_id)
        touched = eng().ingest(from_ticket({**body.model_dump(), "reporter": user.login}), background=True)
        if not touched:
            raise HTTPException(422, "Couldn't tie this problem to the product")
        return {"feature_id": touched[0]}

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
    def member_of(user: User, product_id: str) -> None:
        if product_id not in lanes_for(user):
            if product_id not in eng().services.settings.registry:
                raise HTTPException(404, f"Unknown product {product_id}")
            raise HTTPException(403, f"You aren't a member of {product_id}'s organization")

    @api.get("/costs/{product_id}")
    def product_costs(product_id: str, month: str | None = None, user: User = Depends(current_user)) -> dict:
        """The product's month (YYYY-MM, default this one): run cost, build cost, each feature."""
        member_of(user, product_id)
        check_month(month)
        return eng().product_cost(product_id, month)

    @api.get("/costs/{product_id}/focus.csv")
    def product_focus(product_id: str, month: str | None = None, user: User = Depends(current_user)):
        """The month's cost records in FinOps FOCUS columns, for finance and BI tools."""
        from fastapi.responses import Response

        member_of(user, product_id)
        check_month(month)
        name = f"{product_id}-{month or 'this-month'}-focus.csv"
        return Response(eng().focus_export(product_id, month), media_type="text/csv",
                        headers={"Content-Disposition": f'attachment; filename="{name}"'})

    # --- change management: every production change, and looking back ---
    def check_day(value: str | None, name: str) -> None:
        import re

        if value and not re.fullmatch(r"20\d\d-(0[1-9]|1[0-2])(-[0-3]\d)?", value):
            raise HTTPException(400, f"{name} is YYYY-MM or YYYY-MM-DD")

    def change_rows(user: User, product_id: str | None, since: str | None, until: str | None) -> list[dict]:
        check_day(since, "since")
        check_day(until, "until")
        if product_id:
            member_of(user, product_id)
        mine = set(lanes_for(user))
        log = eng().services.changes
        rows = [r for r in log.list(product_id or None, since, until) if r["product_id"] in mine]
        caused: dict[str, list[str]] = {}
        for x in log.links(product_id or None):
            caused.setdefault(x["change_id"], []).append(x["incident_id"])
        receipts = eng().services.change_receipts.get([r["id"] for r in rows])
        return [{**r, "caused_incidents": caused.get(r["id"], []), "receipt": receipts.get(r["id"])} for r in rows]

    @api.get("/changes")
    def list_changes(user: User = Depends(current_user), product_id: str | None = None, since: str | None = None,
                     until: str | None = None) -> list[dict]:
        """Production changes, newest first: who asked, who approved, the evidence, what they caused."""
        return list(reversed(change_rows(user, product_id, since, until)))

    @api.get("/changes/export.csv")
    def export_changes(user: User = Depends(current_user), product_id: str | None = None, since: str | None = None,
                       until: str | None = None):
        """The change log for auditors: one row per change, with its hash and the one before it."""
        import csv
        import io

        from fastapi.responses import Response

        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["id", "at", "product", "environment", "kind", "type", "action", "summary", "targets", "feature",
                    "release", "incident", "requested_by", "approved_by", "approvals", "pull_requests", "outcome",
                    "caused_incidents", "servicenow", "prev_hash", "hash"])
        for r in change_rows(user, product_id, since, until):
            w.writerow([r["id"], r["at"], r["product_id"], r["env"], r["kind"], r["type"], r["action"], r["summary"],
                        "; ".join(f"{x.get('kind')}:{x.get('name')}" for x in r["targets"]), r["feature_id"],
                        r["release_id"], r["incident_id"], r["requested_by"], "; ".join(r["approved_by"]),
                        "; ".join(f"{a['gate']}:{a['approver']}" for a in r["approvals"]),
                        "; ".join((r.get("evidence") or {}).get("pull_requests") or []), r["outcome"],
                        "; ".join(r["caused_incidents"]), (r.get("receipt") or {}).get("number", ""),
                        r["prev_hash"], r["hash"]])
        name = f"changes-{product_id or 'all'}.csv"
        return Response(buf.getvalue(), media_type="text/csv",
                        headers={"Content-Disposition": f'attachment; filename="{name}"'})

    @api.get("/changes/verify")
    def verify_changes(user: User = Depends(current_user)) -> dict:
        """Whether the change log is intact: every record's hash chains to the one before it."""
        from ..ops.changes import verify

        return verify(eng().services.changes.all())

    @api.get("/reports/lookback")
    def lookback(product_id: str, months: int = 6, user: User = Depends(current_user)) -> dict:
        """Incidents and changes per month: how they ended, time to recover, change failure rate."""
        member_of(user, product_id)
        if not 1 <= months <= 24:
            raise HTTPException(400, "months is 1 to 24")
        return eng().lookback(product_id, months)

    @api.get("/reports/pilot")
    def pilot_report(product_id: str, pilot_only: bool = True, since: str = "", format: str = "json",
                     user: User = Depends(current_user)):
        """The pilot's numbers: cost, people's and agents' time against the 15% target, first-time
        approvals, loops, and what the shadow modes would have decided."""
        from ..pilot import markdown, report

        member_of(user, product_id)
        rep = report(eng(), product_id, pilot_only=pilot_only, since=since)
        if format == "markdown":
            from fastapi.responses import PlainTextResponse

            return PlainTextResponse(markdown(rep), media_type="text/markdown")
        return rep

    @api.get("/products/{product_id}/lessons")
    def product_lessons(product_id: str, user: User = Depends(current_user)) -> dict:
        """What the tool learned from people's corrections: lessons approved in the product's
        governance repo, and lessons proposed there (pull requests) waiting for review."""
        from ..lessons import overview

        member_of(user, product_id)
        try:
            return overview(eng().services, product_id)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc

    # --- resource inventory ---
    @api.get("/inventory/{product_id}")
    def inventory(product_id: str, user: User = Depends(current_user)) -> dict:
        """What the product runs in each environment, with the identifiers to find it directly."""
        member_of(user, product_id)
        try:
            return eng().inventory(product_id)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc

    @api.get("/inventory/{product_id}/history")
    def inventory_history(product_id: str, days: int = 90, user: User = Depends(current_user)) -> list[dict]:
        member_of(user, product_id)
        return eng().services.inventory_snapshots.history(product_id, max(1, min(days, 400)))

    @api.get("/inventory/{product_id}/export.csv")
    def inventory_csv(product_id: str, user: User = Depends(current_user)):
        import csv
        import io

        from fastapi.responses import Response

        member_of(user, product_id)
        inv = eng().inventory(product_id)
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["environment", "kind", "name", "type", "state", "release", "size", "feature", "openchoreo",
                    "binding", "aws_account", "aws_region", "arn", "outputs", "secrets", "cost_30d_usd",
                    "planned_monthly_usd", "kubectl"])
        for r in inv["items"]:
            ids = r.get("ids") or {}
            w.writerow([r["env"], r["kind"], r["name"], r.get("type", ""), r["state"], r.get("release", ""),
                        r.get("size", ""), r.get("feature_id", ""), ids.get("openchoreo", ""), ids.get("binding", ""),
                        ids.get("aws_account", ""), ids.get("aws_region", ""), ids.get("arn", ""),
                        "; ".join(f"{o['name']}={o['value']}" for o in r.get("outputs") or []),
                        "; ".join(f"{s['output']}: secret {s['name']}/{s['key']}" for s in r.get("secrets") or []),
                        r.get("cost_30d_usd", ""), r.get("planned_monthly_usd", "") or "", ids.get("kubectl", "")])
        return Response(buf.getvalue(), media_type="text/csv",
                        headers={"Content-Disposition": f'attachment; filename="inventory-{product_id}.csv"'})

    def check_month(month: str | None) -> None:
        import re

        if month and not re.fullmatch(r"20\d\d-(0[1-9]|1[0-2])", month):
            raise HTTPException(400, "month is YYYY-MM")

    @api.get("/features/{feature_id}/cost")
    def feature_costs(feature_id: str, user: User = Depends(current_user)) -> dict:
        """What the feature cost to build, what G4 approved it to cost to run, and what it costs."""
        try:
            c = eng().feature_cost(feature_id)
        except KeyError as exc:
            raise HTTPException(404, f"Unknown feature {feature_id}") from exc
        member_of(user, c["product_id"])
        return c

    @api.get("/roadmap/{product_id}")
    def roadmap(product_id: str, user: User = Depends(current_user)) -> dict:
        from ..roadmap.views import product_roadmap

        member_of(user, product_id)
        return product_roadmap(eng(), product_id)

    @api.get("/features/{feature_id}/outcome")
    def feature_outcome(feature_id: str, user: User = Depends(current_user)) -> dict:
        """The goals a feature moves, its events in staging and its readouts after release."""
        from ..outcomes.readouts import feature_outcome as outcome

        try:
            v = eng().view(feature_id)
        except KeyError as exc:
            raise HTTPException(404, f"Unknown feature {feature_id}") from exc
        member_of(user, v["product_id"])
        return outcome(eng(), feature_id)

    @api.post("/products/{product_id}/goals/{goal_id}/readings", status_code=201)
    def record_reading(product_id: str, goal_id: str, body: ReadingIn, user: User = Depends(current_user)) -> dict:
        """A goal's number typed in by one of the product's owners (goals whose source is manual)."""
        from ..outcomes import read as outcomes
        from ..outcomes import readouts

        member_of(user, product_id)
        product = eng().services.snapshot(product_id).product
        owners = list(product.approvers.get("g1") or []) + list(product.approvers.get("g0") or [])
        if not any(user.matches(a) for a in owners):
            raise HTTPException(403, f"Only {product_id}'s product owners enter its numbers")
        try:
            r = outcomes.record_manual(eng().services, product_id, goal_id, value=body.value, n=body.n, k=body.k,
                                       start=body.start, end=body.end, entered_by=user.login)
        except KeyError as exc:
            raise HTTPException(404, str(exc).strip("'\"")) from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        retaken = readouts.retake_waiting(eng(), product_id, goal_id)
        return {"reading": r.as_dict(), "readouts": retaken}

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
        from ..integrations.github import GitHubError

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
        from ..inception import Vision

        settings = eng().services.settings
        starters = list((settings.inception or {}).get("starters") or [])
        if not can_decide(user, {"approvers": starters}):
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
                   existing=existing, research_depth=body.research)
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

    @api.get("/features")
    def board(user: User = Depends(current_user), product_id: str | None = None) -> list[dict]:
        rows = [r for r in eng().index.all() if (not product_id or r["product_id"] == product_id)
                and r.get("kind", "feature") == "feature"]
        out = []
        for r in rows:
            try:
                s = eng().summary(r["feature_id"])
            except KeyError:  # created, first step not yet checkpointed
                s = {"feature_id": r["feature_id"], "product_id": r["product_id"], "title": r["title"],
                     "status": "running", "running": True, "rail": [], "pending": []}
            s["pending"] = [decorate(r["feature_id"], {**p, "product_id": s["product_id"]}, user)
                            for p in s.get("pending", [])]
            out.append(s)
        return out

    @api.get("/features/{feature_id}")
    def get_feature(feature_id: str, user: User = Depends(current_user)) -> dict:
        try:
            v = eng().view(feature_id)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        v["pending"] = [decorate(feature_id, {**p, "product_id": v["product_id"]}, user) for p in v["pending"]]
        try:  # which of the gates ahead are this person's to decide
            lane = eng().services.snapshot(v["product_id"]).product
            for st in (v.get("path") or {}).get("stations") or []:
                if st["kind"] == "gate":
                    st["mine"] = can_decide(user, {"approvers": lane.approvers.get(st["key"], [])})
        except Exception:
            pass
        n = getattr(eng().services, "notifier", None)
        v["watching"] = bool(n and feature_id in n.get(user.login).watch)
        return v

    @api.get("/features/{feature_id}/trace")
    def feature_trace(feature_id: str, user: User = Depends(current_user)) -> dict:
        """From the feature's metrics and criteria to its PRs, tests, deployments and incidents."""
        try:
            return eng().trace(feature_id)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc

    @api.get("/features/{feature_id}/trace.csv", response_class=PlainTextResponse)
    def feature_trace_csv(feature_id: str, user: User = Depends(current_user)) -> PlainTextResponse:
        """The traceability matrix as a table: one row per test case, for auditors and spreadsheets."""
        from .. import traceability

        try:
            t = eng().trace(feature_id)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        return PlainTextResponse(traceability.to_csv(t), media_type="text/csv", headers={
            "Content-Disposition": f'attachment; filename="{feature_id}-traceability.csv"'})

    @api.get("/products/{product_id}/trace")
    def product_trace(product_id: str, repo: str = "", pr: str = "", user: User = Depends(current_user)) -> dict:
        """Back from code: the features (and their criteria) that changed a repo or a pull request."""
        if product_id not in eng().services.settings.registry:
            raise HTTPException(404, f"Unknown product {product_id}")
        if not repo and not pr:
            raise HTTPException(422, "Give a repo (org/name) or a pull request (org/name#12)")
        return {"product_id": product_id, "repo": repo, "pr": pr,
                "features": eng().trace_search(product_id, repo=repo, pr=pr)}

    @api.get("/features/{feature_id}/artifacts/{kind}")
    def artifact(feature_id: str, kind: str, user: User = Depends(current_user)) -> dict:
        try:
            return eng().artifact(feature_id, kind)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc

    @api.get("/features/{feature_id}/usage")
    def usage(feature_id: str, user: User = Depends(current_user)) -> list[dict]:
        return [asdict(r) for r in eng().services.ledger.records(feature_id)]

    @api.get("/inbox")
    def inbox(user: User = Depends(current_user), mine: bool = False, product_id: str | None = None) -> list[dict]:
        from ..decisions import low_risk

        items, products = [], {}
        for p in eng().inbox(product_id):
            fid = p["feature_id"]
            item = decorate(fid, {**p, "product_id": p.get("product_id") or fid.rsplit("-FEAT-", 1)[0]}, user)
            if not mine or item["can_decide"]:
                pid = item["product_id"]
                if pid not in products:
                    try:
                        prod = eng().services.snapshot(pid).product
                        products[pid] = (prod.name, prod.data_class)
                    except Exception:
                        products[pid] = (pid, "phi")
                item["product_name"], dc = products[pid]
                try:
                    item["initiative_id"] = eng()._state(fid).get("initiative_id", "") if "-FEAT-" in fid else ""
                except Exception:
                    item["initiative_id"] = ""
                item["low_risk"], item["low_risk_why"] = low_risk(item, dc)
                items.append(item)
        return items

    @api.post("/inbox/approve")
    def approve_batch(body: BatchIn, user: User = Depends(current_user)) -> dict:
        """Approve several low-risk decisions at once. Each is checked again here; any that isn't low risk
        (any more), or isn't this person's, is left for one-at-a-time."""
        from ..decisions import low_risk

        approved, skipped = [], []
        for ref in body.items:
            try:
                item = eng().item(ref.feature_id, ref.interrupt_id)
                pid = eng().view(ref.feature_id)["product_id"]
                ok, why = low_risk(item, eng().services.snapshot(pid).product.data_class)
                if ok and not can_decide(user, {"approvers": item_approvers(ref.feature_id, {**item, "product_id": pid})}):
                    ok, why = False, "not yours to decide"
                if not ok:
                    skipped.append({"feature_id": ref.feature_id, "why": why})
                    continue
                eng().decide(ref.feature_id, ref.interrupt_id,
                             {"decision": "approve", "approver": user.login, "active_minutes": body.active_minutes / max(1, len(body.items)),
                              "feedback": f"Approved with {len(body.items)} low-risk decisions"}, background=True)
                approved.append(ref.feature_id)
            except (KeyError, RuntimeError, InvalidDecision) as exc:
                skipped.append({"feature_id": ref.feature_id, "why": str(exc)[:200]})
        return {"approved": approved, "skipped": skipped}

    @api.post("/features/{feature_id}/decisions/{interrupt_id}", status_code=202)
    def decide(feature_id: str, interrupt_id: str, body: DecisionIn, user: User = Depends(current_user)) -> dict:
        try:
            item = eng().item(feature_id, interrupt_id)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        approvers = item_approvers(feature_id, {**item, "product_id": eng().view(feature_id)["product_id"]})
        if not can_decide(user, {"approvers": approvers}):
            raise HTTPException(403, f"Only {', '.join(approvers) or 'the listed approvers'} can decide this")
        trace = item.get("traceability") or {}
        if body.override_reason and not trace.get("blocks"):
            raise HTTPException(422, "Nothing to override: the traceability matrix is complete")
        if body.decision == "approve" and trace.get("blocks"):
            if not (body.override_reason or "").strip():
                raise HTTPException(409, "The traceability matrix isn't complete: "
                                    + "; ".join((trace.get("gaps") or [])[:5])
                                    + ". Release only with an override and its reason.")
            who = trace.get("override_approvers") or []
            if not can_decide(user, {"approvers": who}):
                raise HTTPException(403, f"Only {', '.join(who) or 'the traceability approvers'} can release "
                                    "with traceability gaps")
        decision = body.model_dump(exclude_none=True) | {"approver": user.login}
        if decision.get("comments"):  # line comments go to the agents as part of the feedback
            from ..decisions import with_comments

            decision["feedback"] = with_comments(decision.get("feedback", ""), decision.pop("comments"))
        try:
            return eng().decide(feature_id, interrupt_id, decision, background=True)
        except InvalidDecision as exc:
            raise HTTPException(422, str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc

    @api.get("/features/{feature_id}/mockups/{sid}", response_class=HTMLResponse)
    def mockup(feature_id: str, sid: str, user: User = Depends(current_user)) -> HTMLResponse:
        """One screen's mockup, for a sandboxed frame: no scripts, nothing loaded from elsewhere."""
        from .. import mockups

        try:
            state = eng()._state(feature_id)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        if state.get("product_id") not in lanes_for(user):
            raise HTTPException(403, f"You aren't a member of {state.get('product_id')}'s organization")
        page = mockups.page(eng().services, state, sid)
        if page is None:
            raise HTTPException(404, f"No mockup {sid}")
        return HTMLResponse(page, headers={
            "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; img-src data:; sandbox",
            "X-Content-Type-Options": "nosniff", "Cache-Control": "no-store"})

    @api.get("/features/{feature_id}/screens/{sid}/{width}")
    def screen_shot(feature_id: str, sid: str, width: str, user: User = Depends(current_user)) -> Response:
        """A screenshot of a built screen from the staging scan, for the people at G6."""
        from .. import screens

        if width not in ("desktop", "phone"):
            raise HTTPException(404, "desktop or phone")
        try:
            state = eng()._state(feature_id)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        if state.get("product_id") not in lanes_for(user):
            raise HTTPException(403, f"You aren't a member of {state.get('product_id')}'s organization")
        data = screens.screenshot(eng().services, state, sid, width)
        if not data:
            raise HTTPException(404, f"No {width} screenshot of {sid}")
        return Response(data, media_type="image/png", headers={"Cache-Control": "private, max-age=300",
                                                                "X-Content-Type-Options": "nosniff"})

    @api.get("/features/{feature_id}/decisions/{interrupt_id}/brief")
    def decision_brief(feature_id: str, interrupt_id: str, user: User = Depends(current_user)) -> dict:
        """The top of a gate: a short summary, and what changed since the version sent back."""
        from .. import decisions

        try:
            item = eng().item(feature_id, interrupt_id)
            v = eng().view(feature_id)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        if v["product_id"] not in lanes_for(user) and v["product_id"] != "INCEPTION":
            raise HTTPException(403, f"You aren't a member of {v['product_id']}'s organization")
        ref = item.get("artifact")
        if ref:
            dc = "phi"
            try:
                dc = eng().services.snapshot(v["product_id"]).product.data_class
            except Exception:
                pass
            return decisions.brief(eng().services, feature_id, ref, history=item.get("history"),
                                   product_id=v["product_id"], data_class=dc)
        if item.get("preview"):
            return {"summary": {"lines": decisions._skim(item["preview"]), "by": "document"}}
        return {"summary": {"lines": [], "by": "document"}}

    @api.post("/features/{feature_id}/sync")
    def sync(feature_id: str, user: User = Depends(current_user)) -> dict:
        """Retry the work-tracker sync now, e.g. after a tracker outage."""
        try:
            pid = eng().view(feature_id)["product_id"]
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        if pid not in lanes_for(user):
            raise HTTPException(403, f"Only members of {pid}'s organization can sync its tracker")
        result = eng().sync_work(feature_id)
        eng().bus.publish({"type": "feature.updated", "feature_id": feature_id})
        return {**result, "work_tracking": eng().view(feature_id)["work_tracking"]}

    # --- intake chat ---
    def intake_session(sid: str, user: User):
        try:
            return stores["intakes"].get(sid, user.login)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc

    @api.post("/intake", status_code=201)
    def intake_start(user: User = Depends(current_user)) -> dict:
        lanes = lanes_for(user)
        if not lanes:
            raise HTTPException(403, "You aren't a member of any product organization")
        starters = list((eng().services.settings.inception or {}).get("starters") or [])
        s = stores["intakes"].create(user.login, lanes, starter=can_decide(user, {"approvers": starters}))
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
        from ..attachments import AttachmentError, max_bytes

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

    def submit_sized(s, user: User) -> dict:
        """An initiative (to G0) or a new product (to inception) from the front door."""
        from ..intake import AGENT_PROPOSES

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
                from ..inception import Vision

                starters = list((eng().services.settings.inception or {}).get("starters") or [])
                if not can_decide(user, {"approvers": starters}):
                    raise HTTPException(403, f"Only {', '.join(starters) or 'the inception starters'} can start a product")
                v = Vision(name=d.name or d.title, org=d.org, vision=d.problem,
                           goals=[measure] if measure else [],
                           users=[] if d.users == AGENT_PROPOSES else [d.users], budget_usd=d.budget_usd,
                           attachments_text="", research_depth=d.research or "quick")
                s.feature_id = eng().create_inception(v, created_by=user.login, background=True)
                s.kind, what = "inception", "a new product: research first, then the brief for the product council"
        except GovernanceError as exc:
            raise HTTPException(424, f"Product governance could not be loaded: {exc}") from exc
        s.messages.append({"role": "agent", "text": f"Started {s.feature_id}, {what}.", "at": s.messages[-1]["at"]})
        stores["intakes"].save(s)
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
        return s.to_dict()

    # --- live updates ---
    @api.get("/events")
    async def events(request: Request, user: User = Depends(current_user)) -> StreamingResponse:
        q = eng().bus.subscribe()

        async def stream():
            try:
                yield "retry: 3000\n\n"
                while not await request.is_disconnected():
                    try:
                        ev = await asyncio.wait_for(q.get(), timeout=15)
                        yield f"data: {json.dumps(ev)}\n\n"
                    except asyncio.TimeoutError:
                        yield ": keep-alive\n\n"
            finally:
                eng().bus.unsubscribe(q)

        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    app.include_router(api)

    # --- web app ---
    if web_dist and (web_dist / "index.html").is_file():
        app.mount("/assets", StaticFiles(directory=web_dist / "assets"), name="assets")

        @app.get("/{path:path}", include_in_schema=False)
        def spa(path: str) -> FileResponse:
            candidate = (web_dist / path).resolve()
            if path and candidate.is_file() and web_dist.resolve() in candidate.parents:
                return FileResponse(candidate)
            return FileResponse(web_dist / "index.html")

    return app


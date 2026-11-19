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

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from ..config import load_settings
from ..engine import FeatureEngine, InMemoryIndex, PostgresIndex
from ..gateway import InMemoryLedger, PostgresLedger
from ..services import build_services
from ..worksync import PostgresLinkStore, WorkSync
from ..web.auth import AuthConfig, GitHubOAuth
from .context import make_context
from .routers import auth as auth_routes, evals, events, features, findings, intake, reports, signals, threads, workspaces
from .routers.signals import record_review_effort  # noqa: F401 - importable from here as before


WEB_DIST = Path(
    os.environ.get("VALXEL_WEB_DIST")
    or Path(os.environ.get("VALXEL_ROOT") or Path(__file__).resolve().parents[3]) / "web" / "dist"
)


log = logging.getLogger(__name__)


def create_engine_from_env() -> tuple[FeatureEngine, Any]:
    from ..tenancy import PLATFORM, bound

    with bound(PLATFORM):  # setting up is the platform's work (valxel_sdlc.tenancy)
        return _create_engine_from_env()


def _create_engine_from_env() -> tuple[FeatureEngine, Any]:
    settings = load_settings()
    if settings.database_url:
        from langgraph.checkpoint.postgres import PostgresSaver
        from psycopg.rows import dict_row

        from ..tenancy import make_pool

        # Each connection says whose rows it may see, from the scope it is checked out in.
        pool = make_pool(settings.database_url, max_size=10,
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
        from ..upkeep.register import PostgresFindingRegister

        services.findings = PostgresFindingRegister(pool)
        from ..workspaces import PostgresWorkspaceStore, Workspaces

        services.workspaces = Workspaces(PostgresWorkspaceStore(pool), services.settings.registry)
        from ..services import make_connections

        services.connections = make_connections(services)
        work = WorkSync(services, PostgresLinkStore(pool),
                        os.environ.get("VALXEL_PUBLIC_URL") or "http://localhost:8080")
        from ..cluster import PostgresCluster
        from ..events import PostgresBus

        from ..run_queue import PostgresRunQueue, mode

        # VALXEL_RUN_QUEUE=postgres: work is queued for `valxel worker` processes (run_queue.py).
        queue = PostgresRunQueue(pool) if mode() == "postgres" else None
        return FeatureEngine(services, saver, PostgresIndex(pool), work=work,
                             bus=PostgresBus(pool, settings.database_url),
                             cluster=PostgresCluster(pool, settings.database_url), queue=queue), pool
    from langgraph.checkpoint.memory import InMemorySaver

    services = build_services(settings, ledger=InMemoryLedger())
    return FeatureEngine(services, InMemorySaver(), InMemoryIndex()), None


def prepare(engine: FeatureEngine, pool: Any, *, auth_mode: str) -> None:
    """What a server or a worker checks and sets up before it takes any work: strict mode's checks,
    and row-level security on every table (VALXEL_TENANCY)."""
    from .. import strict, tenancy

    if strict.enabled():  # refuse to serve while anything would quietly run as a stand-in
        strict.check(engine.services, auth_mode=auth_mode)
    if pool is None:
        return
    with tenancy.bound(tenancy.PLATFORM):
        from ..intake import PostgresIntakeStore

        PostgresIntakeStore(pool)  # its table, so the policies below cover it
        # Every table exists now: turn row-level security on (VALXEL_TENANCY=rls) or off.
        tenancy.apply(pool, engine.services.settings.registry)
        if tenancy.enforced():
            with pool.connection() as conn:
                why = tenancy.bypasses(conn)
            if why:  # Postgres never holds such a login to row-level security
                msg = (f"VALXEL_TENANCY is rls, but the database login is {why}: row-level security "
                       "doesn't apply to it. Connect as a role without SUPERUSER and BYPASSRLS.")
                if strict.enabled():
                    raise strict.StrictModeError(msg)
                log.warning(msg)


def create_app(engine: FeatureEngine | None = None, auth: AuthConfig | None = None,
               oauth: GitHubOAuth | None = None, web_dist: Path | None = WEB_DIST) -> FastAPI:
    ctx = make_context(engine, auth, oauth)
    state, stores, auth = ctx.state, ctx.stores, ctx.auth

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        if state["engine"] is None:
            state["engine"], state["pool"] = create_engine_from_env()
            prepare(state["engine"], state["pool"], auth_mode=auth.mode)
            if state["pool"] is not None:
                from ..intake import PostgresIntakeStore
                from ..tenancy import PLATFORM, bound

                with bound(PLATFORM):
                    stores["intakes"] = PostgresIntakeStore(state["pool"])
            # Polls CI and OpenChoreo for features waiting on them; webhooks make it immediate. With a
            # run queue, API processes leave that to the workers (valxel worker): they run no agents.
            if os.environ.get("VALXEL_PROCESS", "").lower() != "api":
                state["engine"].start_watcher(float(os.environ.get("VALXEL_WATCH_INTERVAL_S", "30")))
        if mcp_server is not None:  # the MCP sessions live as long as the app
            async with mcp_server.session_manager.run():
                yield
        else:
            yield
        state["engine"].stop_watcher()
        if state["pool"] is not None:
            state["pool"].close()

    mcp_server = None
    app = FastAPI(title="Valxel SDLC", version="0.2.0", lifespan=lifespan)

    @app.middleware("http")
    async def tenant_scope(request: Request, call_next):
        """Each request sees the rows of the signed-in person's workspaces (valxel_sdlc.tenancy).
        A request nobody signed in to (sign-in, health, a webhook that checks its signature) does the
        platform's work, as it always has."""
        from .. import tenancy

        with tenancy.bound(tenancy.PLATFORM):  # who it is: memberships span workspaces
            who = ctx.signed_in(request) if state["engine"] is not None else None
            scope = (tenancy.workspaces_of(state["engine"].services.settings.registry, ctx.lanes_for(who))
                     if who is not None else tenancy.PLATFORM)
        with tenancy.bound(scope, who.login if who is not None else ""):
            return await call_next(request)
    api = APIRouter(prefix="/api")
    for group in (auth_routes, signals, evals, features, findings, workspaces, reports, threads, intake, events):
        api.include_router(group.build(ctx))
    app.include_router(api)

    # --- MCP (api/mcp.py): a person's assistant, acting as them through the API above ---
    from . import mcp as mcp_api

    mcp_settings = mcp_api.settings_from_env(auth)
    if mcp_settings is not None:
        from starlette.routing import Route

        verifier = mcp_api.PersonVerifier(mcp_settings["issuer"], mcp_settings["domain"], mcp_settings["client_ids"])
        mcp_server = mcp_api.build(app, ctx.signer, verifier, mcp_settings["resource"])
        mcp_app = mcp_server.streamable_http_app(streamable_http_path="/mcp", stateless_http=True, json_response=True,
                                                 host=urlparse(auth.public_url).hostname or "127.0.0.1")
        # Its own routes (with its bearer-token middleware), ahead of the web app's catch-all.
        app.router.routes[:0] = [Route("/mcp", endpoint=mcp_app),
                                 Route("/.well-known/oauth-protected-resource/mcp", endpoint=mcp_app)]

    # --- A2A (api/a2a.py): the intake and incident agents, for other agents acting for a person ---
    a2a_settings = mcp_api.settings_from_env(auth, "VALXEL_A2A_CLIENT_IDS", "/a2a")
    if a2a_settings is not None:
        from . import a2a

        a2a_verifier = mcp_api.PersonVerifier(a2a_settings["issuer"], a2a_settings["domain"], a2a_settings["client_ids"])
        app.router.routes[:0] = a2a.build(app, ctx.signer, a2a_verifier, auth.public_url, app.version)

    # --- web app ---
    if web_dist and (web_dist / "index.html").is_file():
        app.mount("/assets", StaticFiles(directory=web_dist / "assets"), name="assets")

        root = os.path.realpath(web_dist)

        @app.get("/{path:path}", include_in_schema=False)
        def spa(path: str) -> FileResponse:
            # Inside web/dist only: the containment check comes before anything touches the path.
            candidate = os.path.realpath(os.path.join(root, path))
            if path and candidate.startswith(root + os.sep) and os.path.isfile(candidate):
                return FileResponse(candidate)
            return FileResponse(os.path.join(root, "index.html"))

    return app


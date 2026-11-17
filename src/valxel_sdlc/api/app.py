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

from fastapi import APIRouter, FastAPI
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

        return FeatureEngine(services, saver, PostgresIndex(pool), work=work,
                             bus=PostgresBus(pool, settings.database_url),
                             cluster=PostgresCluster(pool, settings.database_url)), pool
    from langgraph.checkpoint.memory import InMemorySaver

    services = build_services(settings, ledger=InMemoryLedger())
    return FeatureEngine(services, InMemorySaver(), InMemoryIndex()), None


def create_app(engine: FeatureEngine | None = None, auth: AuthConfig | None = None,
               oauth: GitHubOAuth | None = None, web_dist: Path | None = WEB_DIST) -> FastAPI:
    ctx = make_context(engine, auth, oauth)
    state, stores, auth = ctx.state, ctx.stores, ctx.auth

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
    for group in (auth_routes, signals, evals, features, findings, workspaces, reports, threads, intake, events):
        api.include_router(group.build(ctx))
    app.include_router(api)

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


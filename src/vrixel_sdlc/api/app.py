"""HTTP API: feature intake, the gate inbox and human decisions.

With DATABASE_URL set, threads, the usage ledger and the feature index persist in Postgres;
without it, everything is in memory (useful for tests and quick demos).
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from ..config import load_settings
from ..engine import FeatureEngine, InMemoryIndex, PostgresIndex
from ..gateway import InMemoryLedger, PostgresLedger, PolicyViolation
from ..governance import GovernanceError
from ..graph import InvalidDecision
from ..services import build_services


class FeatureIn(BaseModel):
    product_id: str = Field(examples=["EMR"])
    title: str = Field(min_length=3, max_length=200)
    description: str = ""
    budget_usd: float | None = Field(default=None, gt=0)
    simulate_faults: dict[str, int] = Field(default_factory=dict,
                                            description="Stub mode only: phase -> defective attempts")


class DecisionIn(BaseModel):
    decision: str = Field(examples=["approve", "revise", "takeover", "handback", "resolved", "retry", "abort"])
    approver: str = Field(min_length=1)
    feedback: str = ""
    active_minutes: float = Field(default=0.0, ge=0)
    budget_usd: float | None = Field(default=None, gt=0, description="Escalations only: raise the feature budget")
    artifact_content: str | None = Field(default=None, description="Escalations only: the human's finished artifact")


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
        services = build_services(settings, ledger=PostgresLedger(pool))
        return FeatureEngine(services, saver, PostgresIndex(pool)), pool
    from langgraph.checkpoint.memory import InMemorySaver

    services = build_services(settings, ledger=InMemoryLedger())
    return FeatureEngine(services, InMemorySaver(), InMemoryIndex()), None


def create_app(engine: FeatureEngine | None = None) -> FastAPI:
    state: dict[str, Any] = {"engine": engine, "pool": None}

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        if state["engine"] is None:
            state["engine"], state["pool"] = create_engine_from_env()
        yield
        if state["pool"] is not None:
            state["pool"].close()

    app = FastAPI(title="Vrixel agentic SDLC", version="0.1.0", lifespan=lifespan)

    def eng() -> FeatureEngine:
        return state["engine"]

    @app.get("/healthz")
    def healthz() -> dict:
        return {"ok": True, "mode": eng().services.settings.mode}

    @app.get("/products")
    def products() -> dict:
        """Each registered product with its governance as currently read from its own org."""
        services = eng().services
        out = {}
        for pid, entry in services.settings.registry.items():
            row = {"governance_repo": entry.governance.full_name, "ref": entry.governance.ref,
                   "data_class": entry.data_class}
            try:
                snap = services.snapshot(pid)
                p = snap.product
                row |= {"name": p.name, "repos": [r.name for r in p.repos], "budget_usd": p.budget_usd,
                        "constitution_version": snap.version, "articles": list(snap.constitution.article_ids),
                        "ok": True}
            except GovernanceError as exc:
                row |= {"ok": False, "error": str(exc)}
            out[pid] = row
        return out

    @app.post("/features", status_code=201)
    def create_feature(body: FeatureIn) -> dict:
        try:
            return eng().start(product_id=body.product_id, title=body.title, description=body.description,
                               simulate_faults=body.simulate_faults, budget_usd=body.budget_usd)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        except GovernanceError as exc:
            raise HTTPException(424, f"Product governance could not be loaded: {exc}") from exc
        except PolicyViolation as exc:
            raise HTTPException(403, str(exc)) from exc

    @app.get("/features")
    def list_features() -> list[dict]:
        return eng().index.all()

    @app.get("/features/{feature_id}")
    def get_feature(feature_id: str) -> dict:
        try:
            return eng().view(feature_id)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc

    @app.get("/inbox")
    def inbox(product_id: str | None = None) -> list[dict]:
        return eng().inbox(product_id)

    @app.post("/features/{feature_id}/decisions/{interrupt_id}")
    def decide(feature_id: str, interrupt_id: str, body: DecisionIn) -> dict:
        try:
            return eng().decide(feature_id, interrupt_id, body.model_dump(exclude_none=True))
        except InvalidDecision as exc:
            raise HTTPException(422, str(exc)) from exc
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        except PermissionError as exc:
            raise HTTPException(403, str(exc)) from exc

    @app.get("/features/{feature_id}/usage")
    def usage(feature_id: str) -> list[dict]:
        from dataclasses import asdict

        return [asdict(r) for r in eng().services.ledger.records(feature_id)]

    return app


app = create_app()

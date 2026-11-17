"""Evals of the tool's own config."""

from __future__ import annotations

import os
from types import SimpleNamespace

from fastapi import APIRouter, Depends, HTTPException

from ...web.auth import (
    User,
)
from ..models import EvalRunIn


def build(ctx: SimpleNamespace) -> APIRouter:
    api = APIRouter()
    current_user = ctx.current_user
    config_gate = ctx.config_gate

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

    return api

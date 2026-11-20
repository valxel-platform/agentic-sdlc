"""Evals of the tool's own config."""

from __future__ import annotations

import os
from types import SimpleNamespace

from fastapi import APIRouter, Depends, HTTPException

from ...web.auth import (
    User,
)
from ..models import EvalRunIn, SafeModeIn


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

    def owners_only(user: User) -> None:
        allowed = {t for t in os.environ.get("VALXEL_EVAL_APPROVERS", "").split(",") if t}
        if not allowed & set(user.teams) and user.login not in allowed:
            raise HTTPException(403, "Only the tool's owners (VALXEL_EVAL_APPROVERS) see or change the drift evals and safe mode")

    @api.get("/evals/drift")
    def drift_latest(user: User = Depends(current_user)) -> dict:
        """The weekly drift evals (valxel_sdlc.drift): the latest run, and the last few."""
        from ... import drift

        owners_only(user)
        cluster = ctx.eng().cluster
        return {"latest": cluster.get(drift.LATEST), "runs": cluster.get(drift.RUNS) or [],
                "anchor": {k: v for k, v in (cluster.get(drift.ANCHOR) or {}).items() if k != "baseline"}}

    @api.post("/evals/drift", status_code=202)
    def drift_now(user: User = Depends(current_user)) -> dict:
        """Run the drift evals now, in the background, whatever the schedule."""
        import threading

        from ... import drift
        from ...tenancy import PLATFORM, bound

        owners_only(user)
        eng = ctx.eng()

        def go() -> None:
            with bound(PLATFORM):  # cases from every product, as the change gate reads them
                drift.run(eng.services, eng.cluster, force=True)

        threading.Thread(target=go, name="valxel-drift-now", daemon=True).start()
        return {"started": True}

    # ------------------------------------------------------------------ safe mode (valxel_sdlc.safe_mode)

    def known(pid: str) -> None:
        if pid not in ctx.eng().services.settings.registry:
            raise HTTPException(404, f"Unknown product {pid}")

    @api.get("/safe-mode")
    def safe_mode_list(user: User = Depends(current_user)) -> dict:
        """Every product in safe mode: why, how long, the checks so far, approvals to look at again;
        and the scoped replays run lately."""
        from ... import drift, safe_mode

        owners_only(user)
        eng = ctx.eng()
        return {"products": safe_mode.overview(eng.services, eng.cluster, eng.services.settings.registry),
                "needed": safe_mode.NEEDED, "replays": eng.cluster.get(drift.REPLAYS) or []}

    @api.post("/safe-mode/{product_id}/enter")
    def safe_mode_enter(product_id: str, body: SafeModeIn, user: User = Depends(current_user)) -> dict:
        """Put a product in safe mode by hand (support suspects something the rules didn't catch)."""
        from ... import safe_mode

        owners_only(user)
        known(product_id)
        if not body.note.strip():
            raise HTTPException(422, "Say why")
        causes = [{"source": "manual", "rule": "manual", "scope": p, "says": f"{user.login}: {body.note.strip()}"}
                  for p in body.phases] or [{"source": "manual", "rule": "manual", "scope": "",
                                             "says": f"{user.login}: {body.note.strip()}"}]
        safe_mode.enter(ctx.eng().cluster, [product_id], causes)
        return safe_mode.get(ctx.eng().cluster, product_id)

    @api.post("/safe-mode/{product_id}/fixed")
    def safe_mode_fixed(product_id: str, body: SafeModeIn, user: User = Depends(current_user)) -> dict:
        """Say what was changed. Checks from now on judge the work since the fix."""
        from ... import safe_mode

        owners_only(user)
        if not body.note.strip():
            raise HTTPException(422, "Say what was changed")
        try:
            return safe_mode.mark_fixed(ctx.eng().cluster, product_id, by=user.login, note=body.note.strip())
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc

    @api.post("/safe-mode/{product_id}/replay", status_code=202)
    def safe_mode_replay(product_id: str, body: SafeModeIn, user: User = Depends(current_user)) -> dict:
        """Replay the tripped phases (or the ones named) for this product now, in the background: the
        pinned cases and its own recent ones. In safe mode, the result is a check."""
        import threading

        from ... import drift
        from ...tenancy import PLATFORM, bound

        owners_only(user)
        known(product_id)
        eng = ctx.eng()

        def go() -> None:
            with bound(PLATFORM):
                drift.replay_scoped(eng.services, eng.cluster, product_id, body.phases or None)

        threading.Thread(target=go, name="valxel-replay", daemon=True).start()
        return {"started": True, "product_id": product_id, "phases": body.phases or "the tripped ones"}

    @api.post("/safe-mode/{product_id}/leave")
    def safe_mode_leave(product_id: str, user: User = Depends(current_user)) -> dict:
        """An owner ends safe mode, once two checks in a row were normal. Returns the approvals made
        while things were going wrong, to look at again."""
        from ... import safe_mode

        owners_only(user)
        eng = ctx.eng()
        s = safe_mode.get(eng.cluster, product_id)
        if s is None:
            raise HTTPException(404, f"{product_id} isn't in safe mode")
        try:
            return safe_mode.leave(eng.cluster, product_id, by=user.login,
                                   approvals=safe_mode.approvals_in_window(eng.services, product_id, s["bad_since"]))
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

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

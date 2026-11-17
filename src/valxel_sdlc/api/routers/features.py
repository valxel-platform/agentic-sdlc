"""Products, features, the inbox and gate decisions."""

from __future__ import annotations

import logging
from dataclasses import asdict
from types import SimpleNamespace

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse, Response, PlainTextResponse

from ... import profiles
from ...duplicates import DuplicateIntent
from ...gateway import PolicyViolation
from ...governance import GovernanceError
from ...graph import InvalidDecision
from ...services import flag_modes, integration_modes
from ...web.auth import (
    PASSKEY_NEEDED,
    User,
    can_decide,
    needs_passkey,
)
from ..models import FeatureIn, ProblemIn, BatchIn, DecisionIn


log = logging.getLogger(__name__)


def build(ctx: SimpleNamespace) -> APIRouter:
    api = APIRouter()
    eng = ctx.eng
    current_user = ctx.current_user
    lanes_for = ctx.lanes_for
    may_start = ctx.may_start
    item_approvers = ctx.item_approvers
    decorate = ctx.decorate
    member_of = ctx.member_of

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
        may_start(user, body.product_id)
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
        from ...ops.signals import from_ticket

        member_of(user, body.product_id)
        touched = eng().ingest(from_ticket({**body.model_dump(), "reporter": user.login}), background=True)
        if not touched:
            raise HTTPException(422, "Couldn't tie this problem to the product")
        return {"feature_id": touched[0]}

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
        from ... import traceability

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

    @api.get("/usage/context")
    def usage_context(product_id: str, weeks: int = 4, user: User = Depends(current_user)) -> list[dict]:
        """How full agents' context gets and where it goes, by week, role and model (valxel_sdlc.context)."""
        from datetime import UTC, datetime, timedelta

        from ...context import report

        member_of(user, product_id)
        weeks = max(1, min(int(weeks), 26))
        since = (datetime.now(UTC) - timedelta(weeks=weeks)).isoformat()
        return report(eng().services.ledger.records(product_id=product_id, since=since), weeks=weeks)

    @api.get("/features/{feature_id}/usage")
    def usage(feature_id: str, user: User = Depends(current_user)) -> list[dict]:
        return [asdict(r) for r in eng().services.ledger.records(feature_id)]

    @api.get("/inbox")
    def inbox(user: User = Depends(current_user), mine: bool = False, product_id: str | None = None) -> list[dict]:
        from ...decisions import low_risk

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
        from ...decisions import low_risk

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
                # A fixed reason per kind of failure: the exception's text stays in the log.
                log.info("batch approval skipped %s: %s", ref.feature_id, exc)
                why = ("no longer waiting for a decision" if isinstance(exc, KeyError)
                       else "doesn't take this decision" if isinstance(exc, InvalidDecision)
                       else "running right now: try again in a moment")
                skipped.append({"feature_id": ref.feature_id, "why": why})
        return {"approved": approved, "skipped": skipped}

    @api.post("/features/{feature_id}/decisions/{interrupt_id}", status_code=202)
    def decide(feature_id: str, interrupt_id: str, body: DecisionIn, user: User = Depends(current_user)) -> dict:
        try:
            item = eng().item(feature_id, interrupt_id)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        approvers = item_approvers(feature_id, {**item, "product_id": eng().view(feature_id)["product_id"]})
        if not can_decide(user, {"approvers": approvers}):
            raise HTTPException(403, PASSKEY_NEEDED if needs_passkey(user, approvers)
                                else f"Only {', '.join(approvers) or 'the listed approvers'} can decide this")
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
            from ...decisions import with_comments

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
        from ... import mockups

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
        from ... import screens

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
        from ... import decisions

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

    @api.post("/features/{feature_id}/resume", status_code=202)
    def resume(feature_id: str, user: User = Depends(current_user)) -> dict:
        """Carry a stopped feature (or incident, release, initiative) on from its last checkpoint:
        one that stopped on an error, or that a replica dropped mid-step. It can't pass a gate:
        gates wait for their decision as before."""
        try:
            pid = eng().view(feature_id)["product_id"]
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc
        if pid not in lanes_for(user):
            raise HTTPException(403, f"Only members of {pid}'s organization can resume its work")
        try:
            return eng().resume(feature_id, by=user.login)
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc

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

    return api

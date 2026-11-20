"""Costs, changes, pilot reports, inventory and outcomes."""

from __future__ import annotations

import os

from types import SimpleNamespace

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response

from ...web.auth import (
    User,
)
from ..models import ReadingIn


def build(ctx: SimpleNamespace) -> APIRouter:
    api = APIRouter()
    eng = ctx.eng
    current_user = ctx.current_user
    lanes_for = ctx.lanes_for

    member_of = ctx.member_of

    @api.get("/costs/{product_id}")
    def product_costs(product_id: str, month: str | None = None, user: User = Depends(current_user)) -> dict:
        """The product's month (YYYY-MM, default this one): run cost, build cost, each feature."""
        member_of(user, product_id)
        check_month(month)
        return eng().product_cost(product_id, month)

    @api.get("/costs/{product_id}/focus.csv")
    def product_focus(product_id: str, month: str | None = None, user: User = Depends(current_user)):
        """The month's cost records in FinOps FOCUS columns, for finance and BI tools."""

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
        from ...ops.changes import verify

        return verify(eng().services.changes.all())

    @api.get("/reports/lookback")
    def lookback(product_id: str, months: int = 6, user: User = Depends(current_user)) -> dict:
        """Incidents and changes per month: how they ended, time to recover, change failure rate."""
        member_of(user, product_id)
        if not 1 <= months <= 24:
            raise HTTPException(400, "months is 1 to 24")
        return eng().lookback(product_id, months)

    @api.get("/reports/health")
    def tool_health(fresh: bool = False, product_id: str = "", user: User = Depends(current_user)) -> dict:
        """The tool's own health (valxel_sdlc.health): its agents on real work, against their
        baseline, and what's alerting. With product_id: that product's agents on its work, for its
        members. Without: the latest hourly measurement across every product (or a fresh one), for
        the tool's owners (VALXEL_EVAL_APPROVERS) only."""
        from ... import health
        from .auth import tool_owner

        if product_id:
            member_of(user, product_id)
            return health.for_product(eng(), product_id)
        if not tool_owner(user):
            raise HTTPException(403, "Only the tool's owners (VALXEL_EVAL_APPROVERS) see its health across products")
        latest = None if fresh else eng().cluster.get(health.NOTE)
        return latest or health.run(eng(), alert=lambda _text: None)

    @api.get("/reports/gate-confidence")
    def gate_confidence(product_id: str, user: User = Depends(current_user)) -> dict:
        """How far each gate's approval can be predicted from the product's own history, and how
        often each predicted level held up (valxel_sdlc.confidence)."""
        from ... import confidence
        from ...pilot import config_version

        member_of(user, product_id)
        svc = eng().services
        if svc.gate_outcomes is None:
            raise HTTPException(404, "Gate confidence isn't recorded here")
        return confidence.report(svc.gate_outcomes, product_id, confidence.Settings.load(svc.settings.config_dir),
                                 config_version(svc.settings))

    @api.get("/reports/pilot")
    def pilot_report(product_id: str, pilot_only: bool = True, since: str = "", format: str = "json",
                     user: User = Depends(current_user)):
        """The pilot's numbers: cost, people's and agents' time against the 15% target, first-time
        approvals, loops, and what the shadow modes would have decided."""
        from ...pilot import markdown, report

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
        from ...lessons import overview

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
        from ...roadmap.views import product_roadmap

        member_of(user, product_id)
        return product_roadmap(eng(), product_id)

    @api.get("/features/{feature_id}/outcome")
    def feature_outcome(feature_id: str, user: User = Depends(current_user)) -> dict:
        """The goals a feature moves, its events in staging and its readouts after release."""
        from ...outcomes.readouts import feature_outcome as outcome

        try:
            v = eng().view(feature_id)
        except KeyError as exc:
            raise HTTPException(404, f"Unknown feature {feature_id}") from exc
        member_of(user, v["product_id"])
        return outcome(eng(), feature_id)

    @api.post("/products/{product_id}/goals/{goal_id}/readings", status_code=201)
    def record_reading(product_id: str, goal_id: str, body: ReadingIn, user: User = Depends(current_user)) -> dict:
        """A goal's number typed in by one of the product's owners (goals whose source is manual)."""
        from ...outcomes import read as outcomes
        from ...outcomes import readouts

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

    return api

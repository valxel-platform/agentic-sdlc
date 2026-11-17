"""Production signals: alerts, errors, AWS events and GitHub webhooks."""

from __future__ import annotations

import json
import os
from types import SimpleNamespace

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from ...web.auth import (
    User,
    can_decide,
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
    from ...pilot import ESTIMATED_MINUTES

    what = "pr_review" if kind == "pull_request_review" else "pr_comment"
    item = event.get("review") or event.get("comment") or {}
    engine.services.effort.add({"feature_id": fid, "product_id": fid.split("-FEAT-", 1)[0], "kind": what,
                                "who": login, "ref": str(item.get("id") or ""), "minutes": ESTIMATED_MINUTES[what],
                                "detail": {"state": item.get("state", ""), "pr": (event.get("pull_request") or {}).get("html_url", "")}})


def build(ctx: SimpleNamespace) -> APIRouter:
    api = APIRouter()
    eng = ctx.eng
    current_user = ctx.current_user
    config_gate = ctx.config_gate
    member_of = ctx.member_of

    # --- production signals ---
    def bearer_ok(request: Request) -> None:

        from ...secret_ring import bearer_ok as ring_bearer_ok, values

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
        from ...ops.signals import from_alertmanager

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

        from ...ops.signals import from_sentry

        from ...secret_ring import any_matches, values

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
        from ...ops.signals import from_ticket

        bearer_ok(request)
        try:
            payload = json.loads(await request.body() or b"{}")
        except ValueError as exc:
            raise HTTPException(400, "Body is not JSON") from exc
        return await ingest(from_ticket(payload))

    def oncall(user: User, product_id: str) -> None:
        """Only the product's on-call (who decide incidents) may merge or split them."""
        from ...ops.incident import oncall_approvers

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
        from ...ops.signals import from_openchoreo

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

        from ...ops import error_tracking as et

        from ...secret_ring import values

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
        from ...ops.signals import from_cloudwatch

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
        from ...ops.signals import from_inspector

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

        from ...ops.signals import from_aws_budget

        from ...secret_ring import any_matches, values

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

        from ...secret_ring import any_matches, values

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
        kind = request.headers.get("x-github-event", "")
        from ...upkeep.alerts import EVENTS as ALERT_EVENTS

        if kind in ALERT_EVENTS:  # a security alert on a default branch (valxel_sdlc.upkeep)
            rec = await run_in_threadpool(eng().upkeep.receive, kind, event)
            return {"event": kind, "finding": (rec or {}).get("id"), "status": (rec or {}).get("status")}
        if any(b and b.startswith("dependabot/") for b in branches) and kind in ("pull_request", "check_suite", "status"):
            repo = (event.get("repository") or {}).get("full_name", "")
            pid = eng().upkeep.repos().get(repo)
            if pid:  # Dependabot's update may be green now: merge it if the policy allows
                merged = await run_in_threadpool(eng().upkeep.dependabot, pid, repo)
                return {"event": kind, "merged": merged}
        record_review_effort(eng(), kind, event)
        waiting = set(eng().waiting_on_external())
        poked = [fid for fid in sorted(features & waiting if features else waiting) if eng().poke(fid)]
        return {"event": request.headers.get("x-github-event", ""), "resumed": poked}

    return api

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
    independent_review = ctx.independent_review
    member_of = ctx.member_of
    confirm_decider = ctx.confirm_decider

    # --- production signals ---

    # Webhooks answer within seconds (EventBridge gives up after 5): the signal is recorded and
    # on-call paged before the response, off the event loop; triage runs in the background.
    async def ingest(signals: list[dict]) -> dict:
        touched = await run_in_threadpool(lambda: eng().ingest(signals, background=True))
        return {"signals": len(signals), "incidents": touched}

    async def alarm_changes(signals: list[dict]) -> dict:
        return await run_in_threadpool(lambda: eng().alarm_changes(signals, background=True))

    def sender(request: Request, *kinds: str):
        """Who sent this (ops/signal_tokens.py): the platform's token, or a product's or an
        account's. An account token is only a candidate until the event's account is known."""
        from ...ops.signal_tokens import scope_of
        from ...secret_ring import values

        if not values("VALXEL_SIGNALS_TOKEN"):
            raise HTTPException(404, "Signal webhooks are not configured")
        scope = scope_of(request.headers.get("authorization", ""), kinds, list(eng().services.settings.registry))
        if scope is None:
            raise HTTPException(401, "Bad token")
        return scope

    async def within(scope, signals: list[dict]) -> list[dict]:
        """The signals, if every one is the sender's to raise; 403 otherwise."""
        from ...ops.signals import resolve_product

        if scope.kind == "platform":
            return signals

        def product_of(sig: dict) -> str:
            return sig.get("product_id") or resolve_product(eng().services, sig)[0]

        if scope.kind == "product":
            pids = await run_in_threadpool(lambda: [product_of(s) for s in signals])
            out = [{**s, "product_id": p} for s, p in zip(signals, pids, strict=True) if p == scope.name]
        else:
            out = [s for s in signals if scope.allows(s)]
        if len(out) != len(signals):
            raise HTTPException(403, f"This token is for {scope.kind} {scope.name}: "
                                     f"{len(signals) - len(out)} of these signals are for another")
        return out

    @api.post("/signals/alertmanager", status_code=202)
    async def alertmanager(request: Request) -> dict:
        """Alertmanager webhook receiver (http_config.authorization: Bearer <the product's signals
        token>, `valxel signals-token product <id>`)."""
        from ...ops.signals import from_alertmanager

        scope = sender(request, "product")
        try:
            payload = json.loads(await request.body() or b"{}")
        except ValueError as exc:
            raise HTTPException(400, "Body is not JSON") from exc
        return await ingest(await within(scope, from_alertmanager(payload)))

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

        scope = sender(request, "product")
        try:
            payload = json.loads(await request.body() or b"{}")
        except ValueError as exc:
            raise HTTPException(400, "Body is not JSON") from exc
        return await ingest(await within(scope, from_ticket(payload)))

    def oncall(user: User, product_id: str) -> None:
        """Only the product's on-call (who decide incidents) may merge or split them."""
        from ...ops.incident import oncall_approvers

        member_of(user, product_id)
        who = oncall_approvers(eng().services.snapshot(product_id).product)
        if not can_decide(user, {"approvers": who}):
            raise HTTPException(403, "Only the product's on-call can merge or split incidents")
        confirm_decider(user, who)

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
        Authorization: Bearer <the product's signals token>). Budget alerts from the FinOps module
        arrive as cost signals; log and metric alerts as ordinary ones."""
        from ...ops.signals import from_openchoreo

        scope = sender(request, "product")
        try:
            payload = json.loads(await request.body() or b"{}")
        except ValueError as exc:
            raise HTTPException(400, "Body is not JSON") from exc
        # Production alerts open incidents; the rest are kept for G6, like CloudWatch alarms.
        return await alarm_changes(await within(scope, from_openchoreo(payload)))

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
        resolve incidents; every alarm's state is kept for G6. The connection's token is the data
        plane account's (`valxel signals-token account <id>`): it sends that account's events only."""
        from ...ops.signals import from_cloudwatch

        scope = sender(request, "account")
        try:
            event = json.loads(await request.body() or b"{}")
        except ValueError as exc:
            raise HTTPException(400, "Body is not JSON") from exc
        scope = for_account(scope, event)
        return await alarm_changes(await within(scope, from_cloudwatch(event)))

    @api.post("/signals/inspector", status_code=202)
    async def inspector_finding(request: Request) -> dict:
        """Amazon Inspector findings on running images (ECR enhanced scanning, continuous), from an
        EventBridge rule on 'Inspector2 Finding' through an API destination sending Authorization:
        Bearer <the account's signals token>. High and critical vulnerabilities open incidents for the
        component."""
        from ...ops.signals import from_inspector

        scope = sender(request, "account")
        try:
            event = json.loads(await request.body() or b"{}")
        except ValueError as exc:
            raise HTTPException(400, "Body is not JSON") from exc
        scope = for_account(scope, event)
        return await ingest(await within(scope, from_inspector(event)))

    def url_sender(request: Request, *kinds: str):
        """Google Cloud's and Azure's senders (a Cloud Monitoring webhook channel, a Pub/Sub push
        subscription, an action group) put no Bearer header of ours on their requests: the token
        is in the URL (?token=), or the
        password of a webhook_basicauth channel."""
        from ...ops.signal_tokens import scope_of
        from ...secret_ring import values

        if not values("VALXEL_SIGNALS_TOKEN"):
            raise HTTPException(404, "Signal webhooks are not configured")
        token = request.query_params.get("token", "")
        auth = request.headers.get("authorization", "")
        if not token and auth.lower().startswith("basic "):
            # A Cloud Monitoring webhook_basicauth channel: the token is the password, kept in the
            # channel's sensitive labels rather than its URL (platform-resources gcp/alarm-routing).
            import base64
            import binascii

            try:
                token = base64.b64decode(auth[6:].strip()).decode().partition(":")[2]
            except (binascii.Error, UnicodeDecodeError):
                token = ""
        scope = scope_of(f"Bearer {token}" if token else "", kinds, list(eng().services.settings.registry))
        if scope is None:
            raise HTTPException(401, "Bad token")
        return scope

    def for_project(scope, project: str):
        from ...ops.signal_tokens import project_scope

        checked = project_scope(scope, project)
        if checked is None:
            raise HTTPException(401, "Bad token for this project")
        return checked

    async def body_json(request: Request) -> dict:
        try:
            got = json.loads(await request.body() or b"{}")
        except ValueError as exc:
            raise HTTPException(400, "Body is not JSON") from exc
        return got if isinstance(got, dict) else {}

    @api.post("/signals/cloud-monitoring", status_code=202)
    async def cloud_monitoring(request: Request) -> dict:
        """Google Cloud Monitoring incidents from Valxel's alert policies, through a webhook channel
        to https://<host>/api/signals/cloud-monitoring?token=<the project's signals token>
        (`valxel signals-token project <id>`). Production incidents open and resolve Valxel's."""
        from ...ops.signals import from_cloud_monitoring

        scope = url_sender(request, "project")
        payload = await body_json(request)
        scope = for_project(scope, str((payload.get("incident") or {}).get("scoping_project_id") or ""))
        return await alarm_changes(await within(scope, from_cloud_monitoring(payload)))

    @api.post("/signals/scc", status_code=202)
    async def scc_finding(request: Request) -> dict:
        """Security Command Center findings, from a notification config's Pub/Sub topic with a push
        subscription to https://<host>/api/signals/scc?token=<the project's signals token>. High and
        critical image vulnerabilities open incidents for the component, like Inspector's."""
        from ...ops.signals import from_scc

        scope = url_sender(request, "project")
        sigs = from_scc(await body_json(request))
        if sigs:
            scope = for_project(scope, sigs[0]["labels"]["project"])
        return await ingest(await within(scope, sigs))

    @api.post("/signals/gcp-budgets", status_code=202)
    async def gcp_budgets(request: Request) -> dict:
        """Cloud Billing budget notifications, from the budget's Pub/Sub topic with a push
        subscription to https://<host>/api/signals/gcp-budgets?token=<VALXEL_SIGNALS_TOKEN>. Budgets
        belong to the billing account, not a project, so they take the platform's token, as AWS
        Budgets do."""
        from ...ops.signals import from_gcp_budget

        scope = url_sender(request)
        if scope.kind != "platform":
            raise HTTPException(401, "Budgets take the platform's signals token")
        reg = list(eng().services.settings.registry)
        return await ingest(from_gcp_budget(await body_json(request), reg))

    def for_subscription(scope, subscription: str):
        from ...ops.signal_tokens import subscription_scope

        checked = subscription_scope(scope, subscription)
        if checked is None:
            raise HTTPException(401, "Bad token for this subscription")
        return checked

    @api.post("/signals/azure-monitor", status_code=202)
    async def azure_monitor(request: Request) -> dict:
        """Azure Monitor alerts from Valxel's alert rules (common alert schema), through an action
        group's webhook to https://<host>/api/signals/azure-monitor?token=<the subscription's signals
        token> (`valxel signals-token subscription <id>`). Production alerts open and resolve Valxel's."""
        from ...ops.signals import from_azure_monitor

        scope = url_sender(request, "subscription")
        sigs = from_azure_monitor(await body_json(request))
        if sigs:
            scope = for_subscription(scope, sigs[0]["labels"]["subscription"])
        return await alarm_changes(await within(scope, sigs))

    @api.post("/signals/defender", status_code=202)
    async def defender_finding(request: Request) -> dict:
        """Defender for Cloud container image vulnerabilities, from workflow automation posting to
        https://<host>/api/signals/defender?token=<the subscription's signals token>. High and
        critical findings open incidents for the component, like Inspector's."""
        from ...ops.signals import from_defender

        scope = url_sender(request, "subscription")
        sigs = from_defender(await body_json(request))
        if sigs:
            scope = for_subscription(scope, sigs[0]["labels"]["subscription"])
        return await ingest(await within(scope, sigs))

    @api.post("/signals/azure-budgets", status_code=202)
    async def azure_budgets(request: Request) -> dict:
        """Cost Management budget alerts, from an action group's webhook to
        https://<host>/api/signals/azure-budgets?token=<VALXEL_SIGNALS_TOKEN>. Budgets take the
        platform's token, as AWS Budgets and Cloud Billing budgets do."""
        from ...ops.signals import from_azure_budget

        scope = url_sender(request)
        if scope.kind != "platform":
            raise HTTPException(401, "Budgets take the platform's signals token")
        reg = list(eng().services.settings.registry)
        return await ingest(from_azure_budget(await body_json(request), reg))

    def for_account(scope, event: dict):
        from ...ops.signal_tokens import account_scope

        checked = account_scope(scope, str(event.get("account") or ""))
        if checked is None:
            raise HTTPException(401, "Bad token for this account")
        return checked

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

    def _on_pull_request(event: dict) -> str | None:
        """Every PR in a product's repos, whoever opened it, gets the independent review (T-144); the
        event only names the PR, and what's reviewed is read back with the tool's own App. Closing
        one labels its review's eval case (T-145)."""
        repo, number = (event.get("repository") or {}).get("full_name", ""), int(event["pull_request"]["number"])
        pid = eng().upkeep.repos().get(repo)
        if not pid:
            return None
        if event.get("action") in ("opened", "synchronize", "reopened", "ready_for_review"):
            from ...independent_review import enabled

            if not enabled():  # switched off (VALXEL_INDEPENDENT_REVIEW): no review, no status
                return "off"
            return independent_review().start(repo, number, pid)
        if event.get("action") == "closed":
            return _review_outcome(repo, number)
        return None

    def _review_outcome(repo: str, number: int) -> str:
        from ...independent_review import closed, reverted_by, went_wrong

        services = eng().services
        got = services.github.pull_request(repo=repo, number=number) or {}
        out = closed(services, repo, number, merged=bool(got.get("merged")))
        if got.get("merged"):
            for r, n in reverted_by(got.get("body", "")):
                if eng().upkeep.repos().get(r):
                    went_wrong(services, r, n, f"{repo}#{number} reverted it")
        return out or "noted"

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
        if kind == "status" and event.get("context") == "valxel/image":
            # A publish workflow signed a new image: propose its release (valxel_sdlc.release_proposals).
            from ...release_proposals import Proposer

            proposer = Proposer.from_env(eng().services.github, eng().cluster)
            if proposer is None:
                return {"event": kind, "release": "off"}
            return {"event": kind, "release": await run_in_threadpool(proposer.on_status, event)}
        reviewed = (await run_in_threadpool(_on_pull_request, event) if kind == "pull_request" and pr.get("number")
                    else None)
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
        return {"event": request.headers.get("x-github-event", ""), "resumed": poked,
                **({"independent_review": reviewed} if reviewed else {})}

    return api

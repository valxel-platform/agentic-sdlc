"""Valxel's intake and incident agents over A2A (Agent2Agent v1.0, JSON-RPC binding).

Another agent finds them in the card at /.well-known/agent-card.json and talks to them at /a2a,
on behalf of a person: on when VALXEL_A2A_CLIENT_IDS names the Cognito app clients allowed in. As
with MCP (api/mcp.py), every call needs a person's access token; a client-credentials token, an
agent with no person behind it, is refused. What the person may do is the API's to decide: each
call runs through it, in-process, as them.

Skills:
- `intent`: describe a change in plain words. Each text message goes to the intake agent; the task
  waits for input (TASK_STATE_INPUT_REQUIRED) with the agent's question until the draft is ready.
  Send a data part {"submit": true} on the same context to submit it: the task completes with the
  new feature (or initiative) id. The task, and its context, is the intake session.
- `report-incident`: a data part {"skill": "report-incident", "product_id", "title", "description",
  "severity" (sev1-sev4), "component"} opens or joins an incident; the task completes with its id.

SendMessage and GetTask are offered; no streaming or push notifications.
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse

from .mcp import ApiError, NotAPerson, PersonVerifier, call_api

VERSION = "1.0"
# JSON-RPC and A2A error codes
PARSE, INVALID, NO_METHOD, BAD_PARAMS = -32700, -32600, -32601, -32602
TASK_NOT_FOUND, UNSUPPORTED, VERSION_NOT_SUPPORTED = -32001, -32004, -32009


def card(public_url: str, issuer_domain: str, version: str) -> dict:
    """The public agent card: who these agents are, where to reach them, how to sign in."""
    login = f"https://{issuer_domain}/oauth2"
    return {
        "name": "Valxel",
        "description": "Valxel's software delivery agents: turn a plain-words request into a feature in a "
                       "product's pipeline, or report a problem with a running product. Acts for the person "
                       "who signed in, with their rights.",
        "supportedInterfaces": [{"url": f"{public_url}/a2a", "protocolBinding": "JSONRPC", "protocolVersion": VERSION}],
        "provider": {"organization": "Valxel", "url": public_url},
        "version": version,
        "capabilities": {"streaming": False, "pushNotifications": False},
        "securitySchemes": {
            # A person signs in (authorization code with PKCE); there is no client-credentials flow.
            "person": {"oauth2SecurityScheme": {"flows": {"authorizationCode": {
                "authorizationUrl": f"{login}/authorize", "tokenUrl": f"{login}/token",
                "scopes": {"openid": "Who you are", "email": "Your verified email", "profile": "Your name"}}}}},
        },
        "securityRequirements": [{"schemes": {"person": {"list": ["openid", "email"]}}}],
        "defaultInputModes": ["text/plain", "application/json"],
        "defaultOutputModes": ["text/plain", "application/json"],
        "skills": [
            {"id": "intent", "name": "Describe a change",
             "description": "Describe what you want built in your own words. The intake agent asks what it needs "
                            "(product, problem, users, how success is measured) and drafts a feature; send "
                            '{"submit": true} as a data part to start it in the product\'s pipeline.',
             "tags": ["intake", "requirements", "feature"],
             "examples": ["Patients should confirm consent before their first visit is booked"]},
            {"id": "report-incident", "name": "Report a problem",
             "description": "Report a problem with a running product: send a data part with skill "
                            '"report-incident", product_id, title, description, severity (sev1-sev4) and '
                            "component. It opens an incident, or joins one already open for the same thing.",
             "tags": ["incident", "operations"],
             "examples": [('{"skill": "report-incident", "product_id": "CLN", "title": "Intake form times out", '
                           '"severity": "sev2"}')]},
        ],
    }


def _error(rid: Any, code: int, message: str, status: int = 200, headers: dict | None = None) -> JSONResponse:
    return JSONResponse({"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}},
                        status_code=status, headers=headers)


def _ok(rid: Any, result: dict) -> JSONResponse:
    return JSONResponse({"jsonrpc": "2.0", "id": rid, "result": result}, headers={"A2A-Version": VERSION})


def _text(role: str, text: str, context_id: str = "", task_id: str = "") -> dict:
    m = {"messageId": uuid.uuid4().hex, "role": role, "parts": [{"text": text}]}
    return m | ({"contextId": context_id} if context_id else {}) | ({"taskId": task_id} if task_id else {})


def intent_task(session: dict) -> dict:
    """An intake session as an A2A task: waiting for the person until it is submitted."""
    sid = session["id"]
    history = [_text("ROLE_AGENT" if m["role"] == "agent" else "ROLE_USER", m["text"], sid, sid)
               for m in session.get("messages") or []]
    done = bool(session.get("feature_id") or session.get("kind"))
    task: dict = {"id": sid, "contextId": sid, "history": history, "metadata": {"skill": "intent"},
                  "status": {"state": "TASK_STATE_COMPLETED" if done else "TASK_STATE_INPUT_REQUIRED",
                             **({"message": history[-1]} if history else {})}}
    if done:
        task["artifacts"] = [{"artifactId": f"{sid}-submitted", "name": "submitted",
                              "parts": [{"data": {"feature_id": session.get("feature_id", ""),
                                                  "kind": session.get("kind", "")}}]}]
    else:
        task["metadata"] |= {"ready": bool(session.get("ready")), "missing": session.get("missing") or []}
    return task


def build(app: Any, signer: Any, verifier: PersonVerifier, public_url: str, version: str) -> list:
    """The card's route and /a2a's, for the app's router."""
    import anyio
    from starlette.routing import Route

    the_card = card(public_url, verifier.domain, version)

    async def agent_card(_request: Request) -> JSONResponse:
        return JSONResponse(the_card, headers={"Cache-Control": "public, max-age=300"})

    async def rpc(request: Request) -> JSONResponse:
        challenge = {"WWW-Authenticate": f'Bearer realm="valxel", resource="{public_url}/a2a"'}
        auth = request.headers.get("authorization", "")
        if not auth.lower().startswith("bearer "):
            return _error(None, INVALID, "Sign in: a person's bearer token is required", 401, challenge)
        try:
            person = await anyio.to_thread.run_sync(verifier.person, auth.split(None, 1)[1].strip())
        except NotAPerson as exc:
            return _error(None, INVALID, f"Not signed in as a person: {exc}", 401, challenge)
        except Exception:  # noqa: BLE001 - the user pool unreachable: no access
            return _error(None, INVALID, "Couldn't check the sign-in; try again", 503)
        if request.headers.get("A2A-Version", VERSION).split(".")[0] != "1":
            return _error(None, VERSION_NOT_SUPPORTED, f"This agent speaks A2A {VERSION}")
        try:
            body = await request.json()
        except ValueError:
            return _error(None, PARSE, "Not JSON")
        if not isinstance(body, dict) or body.get("jsonrpc") != "2.0" or not isinstance(body.get("method"), str):
            return _error(body.get("id") if isinstance(body, dict) else None, INVALID, "Not a JSON-RPC 2.0 request")
        rid, params = body.get("id"), body.get("params") or {}

        async def api(method: str, path: str, data: dict | None = None) -> Any:
            return await call_api(app, signer, person, method, path, data)

        try:
            if body["method"] == "SendMessage":
                return _ok(rid, {"task": await send(api, params)})
            if body["method"] == "GetTask":
                task_id = str(params.get("id") or "")
                if task_id.startswith("incident:"):
                    fid = task_id.removeprefix("incident:")
                    return _ok(rid, incident_task(fid, await api("GET", f"/features/{fid}")))
                return _ok(rid, intent_task(await api("GET", f"/intake/{task_id}")))
            if body["method"] in ("SendStreamingMessage", "SubscribeToTask", "CancelTask", "ListTasks"):
                return _error(rid, UNSUPPORTED, f"{body['method']} isn't offered: use SendMessage and GetTask")
            return _error(rid, NO_METHOD, f"No method {body['method']}")
        except BadParams as exc:
            return _error(rid, BAD_PARAMS, str(exc))
        except ApiError as exc:
            if exc.status == 404 and body["method"] == "GetTask":
                return _error(rid, TASK_NOT_FOUND, "No such task")
            # The API's answer, refusals included (403: not theirs to do), as the task's failure.
            return _error(rid, INVALID if exc.status < 500 else -32603, str(exc))

    return [Route("/.well-known/agent-card.json", agent_card, methods=["GET"]),
            Route("/a2a", rpc, methods=["POST"])]


class BadParams(Exception):
    pass


def incident_task(fid: str, view: dict) -> dict:
    return {"id": f"incident:{fid}", "contextId": f"incident:{fid}", "metadata": {"skill": "report-incident"},
            "status": {"state": "TASK_STATE_COMPLETED",
                       "message": _text("ROLE_AGENT", f"Reported as {fid}: {view.get('title', '')}".strip())},
            "artifacts": [{"artifactId": f"{fid}-incident", "name": "incident",
                           "parts": [{"data": {"feature_id": fid, "status": view.get("status", "")}}]}]}


async def send(api, params: dict) -> dict:
    message = params.get("message")
    if not isinstance(message, dict) or not isinstance(message.get("parts"), list) or not message["parts"]:
        raise BadParams("params.message with parts is required")
    texts = [str(p["text"]) for p in message["parts"] if isinstance(p, dict) and isinstance(p.get("text"), str)]
    data: dict = {}
    for p in message["parts"]:
        if isinstance(p, dict) and isinstance(p.get("data"), dict):
            data |= p["data"]
    skill = str(data.get("skill") or (message.get("metadata") or {}).get("skill") or "intent")

    if skill == "report-incident":
        report = {k: data[k] for k in ("product_id", "title", "description", "component", "severity") if k in data}
        if not report.get("product_id") or not report.get("title"):
            raise BadParams("report-incident needs product_id and title")
        fid = (await api("POST", "/incidents", report))["feature_id"]
        return incident_task(fid, {"title": report["title"]})
    if skill != "intent":
        raise BadParams(f"No skill {skill}: intent or report-incident")

    sid = str(message.get("contextId") or message.get("taskId") or "")
    if not sid:
        if not texts:
            raise BadParams("Describe what you want in a text part")
        sid = (await api("POST", "/intake", {}))["id"]
    session = None
    for text in texts:
        session = await api("POST", f"/intake/{sid}/messages", {"text": text[:4000]})
    if data.get("submit"):
        session = await api("POST", f"/intake/{sid}/submit", {})
    return intent_task(session or await api("GET", f"/intake/{sid}"))

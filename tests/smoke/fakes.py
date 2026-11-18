"""The services' documented responses, for seeding the first cassettes (VALXEL_SMOKE_SEED=1) only.
Replays never use these: they use what the cassettes hold."""

import json
import re
from email.message import Message
from io import BytesIO
from urllib.error import HTTPError
from urllib.request import BaseHandler, OpenerDirector

import httpx

SHA_BASE, SHA_HEAD, SHA_MERGE = "1" * 40, "2" * 40, "3" * 40


class _Resp(BytesIO):
    def __init__(self, body, status):
        super().__init__(body)
        self.status = self.code = status

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


def github() -> OpenerDirector:
    """GitHub's REST API for one repository with no branch `valxel/smoke` yet."""
    state = {"branch": False, "file": False, "pr": None, "merged": False}

    def pr_json(repo):
        return {"number": 7, "state": "closed" if state["merged"] else "open", "title": "Smoke: hello",
                "body": "The tool's live smoke test.", "html_url": f"https://github.com/{repo}/pull/7",
                "merged": state["merged"], "merged_at": "2026-11-18T17:00:00Z" if state["merged"] else None,
                "merge_commit_sha": SHA_MERGE if state["merged"] else None,
                "head": {"ref": "valxel/smoke", "sha": SHA_HEAD}, "base": {"ref": "main"},
                "labels": [{"name": "valxel"}] if state["pr"] == "labelled" else [], "user": {"login": "valxel[bot]"}}

    def route(method, url, body):
        m = re.match(r"https://api\.github\.com/repos/([^/]+/[^/]+)(.*)", url)
        repo, path = m.group(1), m.group(2)
        if method == "GET" and path == "/git/ref/heads/valxel%2Fsmoke":
            return (200, {"ref": "refs/heads/valxel/smoke", "object": {"sha": SHA_BASE}}) if state["branch"] else (404, {"message": "Not Found"})
        if method == "GET" and path == "":
            return 200, {"full_name": repo, "default_branch": "main"}
        if method == "GET" and path == "/git/ref/heads/main":
            return 200, {"ref": "refs/heads/main", "object": {"sha": SHA_BASE}}
        if method == "POST" and path == "/git/refs":
            state["branch"] = True
            return 201, {"ref": body["ref"], "object": {"sha": body["sha"]}}
        if method == "GET" and path.startswith("/contents/smoke/hello.md"):
            return 404, {"message": "Not Found"}
        if method == "PUT" and path == "/contents/smoke/hello.md":
            state["file"] = True
            return 201, {"content": {"path": "smoke/hello.md", "sha": "4" * 40}, "commit": {"sha": SHA_HEAD}}
        if method == "GET" and path.startswith("/pulls?state=all"):
            return 200, []
        if method == "POST" and path == "/pulls":
            state["pr"] = "open"
            return 201, pr_json(repo)
        if method == "POST" and path == "/issues/7/labels":
            state["pr"] = "labelled"
            return 200, [{"name": n} for n in body["labels"]]
        if method == "GET" and path.startswith(f"/commits/{SHA_HEAD}/check-runs"):
            return 200, {"total_count": 1, "check_runs": [{"name": "ci", "status": "completed", "conclusion": "success",
                                                           "html_url": f"https://github.com/{repo}/actions/runs/1"}]}
        if method == "GET" and path == f"/commits/{SHA_HEAD}/status":
            return 200, {"state": "success", "statuses": []}
        if method == "GET" and path == "/pulls/7":
            return 200, pr_json(repo)
        if method == "PUT" and path == "/pulls/7/merge":
            state["merged"] = True
            return 200, {"sha": SHA_MERGE, "merged": True, "message": "Pull Request successfully merged"}
        raise AssertionError(f"the GitHub fake has no {method} {path}")

    class Handler(BaseHandler):
        def https_open(self, req):
            body = json.loads(req.data) if req.data else None
            status, out = route(req.get_method(), req.full_url, body)
            raw = json.dumps(out).encode()
            if status >= 400:
                raise HTTPError(req.full_url, status, "fake", Message(), BytesIO(raw))
            return _Resp(raw, status)

    o = OpenerDirector()
    o.add_handler(Handler())
    return o


def gateway(module=httpx):
    """ss-llm-gateway: session tokens and Anthropic's Messages API behind it."""
    return _gateway_class(module)()


def _gateway_class(httpx):
    class Gateway(httpx.BaseTransport):
        def handle_request(self, request):
            body = json.loads(request.read() or b"{}")
            path = request.url.path
            if request.method == "POST" and path == "/v1/sessions":
                return httpx.Response(201, json={"id": "gs_smoke1", "token": "vxs_smoke", "expires_at": "2026-11-18T18:00:00Z",
                                                 "budget_usd": body["budget_usd"], "models": body["models"]})
            if request.method == "DELETE" and path == "/v1/sessions/gs_smoke1":
                return httpx.Response(200, json={"id": "gs_smoke1", "spent_usd": 0.0123, "calls": 1})
            if request.method == "POST" and path == "/v1/messages":
                return httpx.Response(200, json={
                    "id": "msg_smoke1", "type": "message", "role": "assistant", "model": body["model"],
                    "content": [{"type": "text", "text": "# PRD: Harvest export\n\n## Problem\nGrowers copy yields by hand."}],
                    "stop_reason": "end_turn", "stop_sequence": None,
                    "usage": {"input_tokens": 1830, "output_tokens": 42, "cache_creation_input_tokens": 1500,
                              "cache_read_input_tokens": 0}})
            raise AssertionError(f"the gateway fake has no {request.method} {path}")

    return Gateway

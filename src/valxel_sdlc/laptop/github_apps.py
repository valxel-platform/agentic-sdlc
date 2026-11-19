"""`valxel dev github-apps`: register this laptop's own Delivery and Provisioning GitHub Apps, so
the sign-up wizard's GitHub step runs for real (connections.py): Install, GitHub's consent
screen, the OAuth code during installation, Valxel checking the installation is the installer's,
then the next step.

It uses GitHub's App manifest flow. A page on 127.0.0.1 posts each App's manifest to GitHub; you
name the App and confirm; GitHub sends you back with a code, valid for an hour, that's exchanged
once for the App's id, OAuth client, private key and webhook secret. Those go into .env (owner-only)
and are never printed. The Apps are private to the account or org that registers them: register
them under the org you'll install them on (`--org`).

Webhooks: GitHub can't reach a laptop, so each App's webhook is registered inactive. The tool
notices CI and pull request changes on its watcher's timer instead, a little later than a webhook
would.
"""

from __future__ import annotations

import html
import http.server
import json
import secrets
import threading
import urllib.request
import webbrowser
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from ..integrations.github import DELIVERY_PERMISSIONS, PROVISIONING_PERMISSIONS
from . import TOOL_PORT, home, read_env, tool_port, write_env

API = "https://api.github.com"
WEB = "https://github.com"
APPS = {
    # app: (.env prefix for slug/id/client, private key setting, permissions, what it's for)
    "delivery": ("VALXEL_GITHUB_APP", "VALXEL_GITHUB_APP_PRIVATE_KEY", DELIVERY_PERMISSIONS,
                 "Code, pull requests, checks and security alerts on the repos you choose"),
    "provisioner": ("VALXEL_GITHUB_PROVISIONER_APP", "VALXEL_GITHUB_PROVISIONER_PRIVATE_KEY", PROVISIONING_PERMISSIONS,
                    "Creating repos and setting their rules, after a gate approves"),
}
EVENTS = {"delivery": ["pull_request", "check_suite", "check_run", "status", "push"], "provisioner": []}


def callback_urls(port: int) -> list[str]:
    """Where GitHub returns the installer: the tool on this machine, on either laptop port (8080, or
    8090 beside a local OpenChoreo), so switching modes doesn't need new Apps."""
    ports = [port] + [p for p in (TOOL_PORT, 8090) if p != port]
    return [f"http://localhost:{p}/api/connections/github/callback" for p in ports]


def manifest(app: str, *, name: str, port: int, redirect: str) -> dict:
    _, _, perms, what = APPS[app]
    urls = callback_urls(port)
    return {
        "name": name,
        "url": f"http://localhost:{port}",
        "description": f"Valxel on a laptop ({app}): {what}.",
        "hook_attributes": {"url": f"http://localhost:{port}/api/webhooks/github", "active": False},
        "redirect_url": redirect,
        "callback_urls": urls,
        "setup_url": urls[0],
        "setup_on_update": True,
        "request_oauth_on_install": True,  # the code Valxel uses to check the install is the installer's
        "public": False,
        "default_permissions": dict(perms),
        "default_events": EVENTS[app],
    }


def settings_from(app: str, created: dict) -> dict[str, str]:
    """The conversion's answer as .env settings. The PEM is kept on one line with \\n escapes
    (signing.py reads either form)."""
    prefix, key, _, _ = APPS[app]
    out = {f"{prefix}_ID": str(created["id"]), f"{prefix}_SLUG": created["slug"],
           f"{prefix}_CLIENT_ID": created["client_id"], f"{prefix}_CLIENT_SECRET": created["client_secret"],
           key: created["pem"].strip().replace("\n", "\\n")}
    if app == "delivery" and created.get("webhook_secret"):
        out["VALXEL_GITHUB_WEBHOOK_SECRET"] = created["webhook_secret"]
    return out


def convert(code: str, *, opener=urllib.request.urlopen, api: str = API) -> dict:
    """Exchange the manifest flow's code (one use, an hour) for the new App's credentials."""
    req = urllib.request.Request(f"{api}/app-manifests/{code}/conversions", data=b"", method="POST",
                                 headers={"Accept": "application/vnd.github+json", "User-Agent": "valxel-sdlc",
                                          "X-GitHub-Api-Version": "2022-11-28"})
    with opener(req, timeout=30) as resp:
        return json.loads(resp.read().decode())


def new_app_url(org: str, state: str) -> str:
    base = f"{WEB}/organizations/{org}/settings/apps/new" if org else f"{WEB}/settings/apps/new"
    return f"{base}?state={state}"


def form_page(action: str, data: dict) -> bytes:
    """A page that posts the manifest to GitHub as soon as it loads (and a button, without script)."""
    return (f"""<!doctype html><meta charset="utf-8"><title>Registering a GitHub App</title>
<body style="font:16px system-ui;margin:3rem">
<p>Sending you to GitHub to register <b>{html.escape(data['name'])}</b>. Name it, then confirm.</p>
<form id="f" method="post" action="{html.escape(action)}">
<input type="hidden" name="manifest" value="{html.escape(json.dumps(data))}">
<button>Continue to GitHub</button></form>
<script>document.getElementById("f").submit()</script>""").encode()


class _Flow:
    """One App at a time through the manifest flow, served on 127.0.0.1."""

    def __init__(self, apps: list[str], *, org: str, port: int, prefix: str, convert_fn=convert) -> None:
        self.apps, self.org, self.port, self.prefix, self.convert = apps, org, port, prefix, convert_fn
        self.state = {a: secrets.token_urlsafe(24) for a in apps}
        self.results: dict[str, dict] = {}
        self.error = ""
        self.done = threading.Event()
        self.server: http.server.HTTPServer | None = None

    def next_app(self) -> str | None:
        return next((a for a in self.apps if a not in self.results), None)

    def handle(self, path: str) -> tuple[int, bytes, dict]:
        url = urlparse(path)
        local = f"http://127.0.0.1:{self.server.server_address[1]}"  # type: ignore[union-attr]
        if url.path == "/":
            app = self.next_app()
            if app is None:
                return 200, b"Both Apps are registered. You can close this tab.", {}
            data = manifest(app, name=f"{self.prefix} {app}", port=self.port, redirect=f"{local}/created")
            return 200, form_page(new_app_url(self.org, self.state[app]), data), {"Content-Type": "text/html"}
        if url.path == "/created":
            q = {k: v[0] for k, v in parse_qs(url.query).items()}
            app = next((a for a, s in self.state.items() if secrets.compare_digest(s, q.get("state", ""))), None)
            if app is None or not q.get("code"):
                self.error = "GitHub came back without this flow's state or a code; nothing was saved"
                self.done.set()
                return 400, self.error.encode(), {}
            try:
                self.results[app] = self.convert(q["code"])
            except Exception as exc:  # noqa: BLE001 - the code is single-use: say so, and stop
                self.error = f"Couldn't fetch the {app} App's credentials from GitHub: {exc}"
                self.done.set()
                return 502, self.error.encode(), {}
            if self.next_app() is None:
                self.done.set()
            return 302, b"", {"Location": "/"}
        return 404, b"", {}


def register(target: Path | None = None, *, org: str = "", apps: list[str] | None = None, prefix: str = "",
             open_browser=webbrowser.open, convert_fn=convert, timeout: float = 1800) -> dict[str, dict]:
    """Run the manifest flow for each App and save their settings to .env. Returns the conversions."""
    target = target or home()
    apps = apps or list(APPS)
    prefix = prefix or f"Valxel laptop {secrets.token_hex(2)}"  # App names are unique across GitHub
    flow = _Flow(apps, org=org, port=tool_port(target), prefix=prefix, convert_fn=convert_fn)

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # the standard library's name
            status, body, headers = flow.handle(self.path)
            self.send_response(status)
            for k, v in headers.items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):  # quiet: the URL carries the one-time code
            pass

    flow.server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=flow.server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{flow.server.server_address[1]}/"
    print(f"Opening {url} to register {', '.join(apps)} on GitHub{' for ' + org if org else ''}.")
    open_browser(url)
    try:
        if not flow.done.wait(timeout):
            raise TimeoutError("No answer from GitHub in time; run it again")
    finally:
        flow.server.shutdown()
    if flow.error:
        raise RuntimeError(flow.error)
    env = read_env(target / ".env")
    for app, created in flow.results.items():
        env.update(settings_from(app, created))
    write_env(target / ".env", env)
    return flow.results


def main(args) -> int:
    target = Path(args.home) if getattr(args, "home", None) else home()
    if not (target / ".env").exists():
        print("Run `valxel dev init` first.")
        return 1
    try:
        made = register(target, org=args.org or "")
    except (RuntimeError, TimeoutError) as exc:
        print(str(exc))
        return 1
    for app, created in made.items():
        print(f"{app}: {WEB}/apps/{created['slug']} (saved to .env)")
    print("Restart the tool (valxel dev up), sign up, and use the wizard's Install buttons.")
    return 0


def installed_on(env: dict[str, str], *, api: str = API, opener=None) -> dict[str, list[str] | str]:
    """For each App set up in `env`: the accounts it's installed on (signed as the App, so this also
    proves the key and id), or why it couldn't tell. Apps not set up are left out."""
    from ..governance.sources import GitHubAppTokenProvider
    from ..signing import github_app_configured, github_app_signer

    out: dict[str, list[str] | str] = {}
    for app in APPS:
        if not github_app_configured(env, app=app):
            continue
        prefix = APPS[app][0]
        try:
            p = GitHubAppTokenProvider(env[f"{prefix}_ID"], github_app_signer(env, app=app), api=api, opener=opener)
            got = p._call("GET", "/app/installations", p._jwt())
            out[app] = sorted(str((i.get("account") or {}).get("login", "")) for i in (got if isinstance(got, list) else []))
        except Exception as exc:  # noqa: BLE001 - a bad key, a deleted App: say it plainly
            out[app] = f"GitHub refused: {exc}"
    return out

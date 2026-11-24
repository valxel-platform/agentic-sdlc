"""A workspace's connections to its tools (the sign-in design, A5 and A6).

Each tool connects through the vendor's own consent screen, when a step first needs it, and only
an admin connects. A connection records what Valxel can reach, who connected it and when it last
worked; the connections page shows every tool, connected or not.

GitHub connects by installing Valxel's two GitHub Apps, one after the other: Delivery (code, pull
requests, checks, security alerts; no admin rights) and Provisioning (creating repos and setting
their rules, signed only by the provisioning worker). Several installations per workspace are fine
(an org and a founder's personal account). Valxel stores no GitHub secret: the Apps sign through
KMS or a key file, and a connection is just the installation's record.

An install is verified as the person who made it: GitHub hands back a code during installation
(the App's "Request user authorization (OAuth) during installation"), Valxel exchanges it for that
person's short-lived token, and the installation must be among the ones GitHub says they can see.
Otherwise anyone could pass another customer's installation id and attach it to their workspace.
The token is used for those two reads and dropped.

Health: every hour, one cheap read per connection (the Delivery App's view of its installation).
A failure turns the row to Needs attention and emails the workspace's admins once, in plain words.
A self-serve workspace's feature waits for its GitHub connection before work that needs the repos
("Waiting for: connect GitHub"), and carries on by itself once it's there.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import time
from dataclasses import dataclass
from typing import Callable
from urllib.error import HTTPError
from urllib.parse import quote, urlencode
from urllib.request import Request, build_opener

log = logging.getLogger(__name__)

STATUSES = ("connected", "needs_attention", "not_connected")


@dataclass(frozen=True)
class Tool:
    id: str
    name: str
    asked_when: str
    grants: str
    revoke: str
    available: bool = False  # can be connected from the page today


TOOLS: tuple[Tool, ...] = (
    Tool("github", "GitHub", "Sign-up, before the first product",
         "Code, pull requests, checks and security alerts on the repos you choose; no admin rights",
         "Uninstall the Valxel App in GitHub (Settings, GitHub Apps), or Disconnect here", available=True),
    Tool("github-provisioning", "GitHub, repo creation", "Sign-up, right after GitHub",
         "Creating repos and setting their rules; used only by the provisioning worker, after a gate approves",
         "Uninstall it in GitHub; Valxel then lists the repos for you to create", available=True),
    Tool("aws", "AWS", "Go-live, in lite or full mode",
         "A role Valxel assumes with your workspace's external ID, scoped to the mode, and a read-only role for agents",
         "Delete the CloudFormation stack", available=True),
    Tool("jira", "Jira", "Only if you pick Jira as the tracker",
         "Read and write issues on the Jira site you choose, as the person who connected it",
         "Remove Valxel under Connected apps in your Atlassian account, or Disconnect here", available=True),
    Tool("lucid", "Lucid", "Only if you want diagrams in Lucid", "Create and update diagrams, as the person who connected it",
         "Revoke Valxel under App integrations in Lucid, or Disconnect here", available=True),
    Tool("sentry", "Sentry", "First production release, if you use it", "Read errors in the organization you choose",
         "Uninstall Valxel under Settings, Integrations in Sentry, or Disconnect here", available=True),
    Tool("pagerduty", "PagerDuty", "First production release, if you use it", "Open and resolve incidents; read services",
         "Revoke Valxel under Integrations, OAuth Apps in PagerDuty, or Disconnect here", available=True),
)
TOOL = {t.id: t for t in TOOLS}
OAUTH_TOOLS = ("jira", "lucid", "sentry", "pagerduty")  # valxel_sdlc.oauth_connections
# The Apps behind each GitHub tool (valxel_sdlc.signing names their settings).
GITHUB_APPS = {"github": "delivery", "github-provisioning": "provisioner"}
APP_ENV = {"delivery": "VALXEL_GITHUB_APP", "provisioner": "VALXEL_GITHUB_PROVISIONER_APP"}


class ConnectionError_(Exception):  # noqa: N801 - not the builtin ConnectionError
    """A connection couldn't be made or checked: the message says why in plain words."""


@dataclass
class GitHubAppSettings:
    """What the install flow needs of one App: its slug (for the install page), id, and OAuth client
    (for the code GitHub returns during installation). The private key isn't needed here."""

    slug: str
    app_id: str
    client_id: str
    client_secret: str

    @classmethod
    def from_env(cls, app: str, env=None) -> GitHubAppSettings | None:
        from .secret_ring import current

        env = env if env is not None else os.environ
        base = APP_ENV[app]
        got = cls(slug=env.get(f"{base}_SLUG", ""), app_id=env.get(f"{base}_ID", ""),
                  client_id=env.get(f"{base}_CLIENT_ID", ""), client_secret=current(f"{base}_CLIENT_SECRET", env) or "")
        return got if all((got.slug, got.app_id, got.client_id, got.client_secret)) else None


class GitHubInstalls:
    """GitHub's side of an install: the install page, and the installing person's view of it."""

    def __init__(self, apps: dict[str, GitHubAppSettings | None], *, api: str = "https://api.github.com",
                 web: str = "https://github.com", opener=None) -> None:
        self.apps, self.api, self.web = apps, api.rstrip("/"), web.rstrip("/")
        self.opener = opener or build_opener()

    def install_url(self, tool: str, state: str) -> str:
        app = self.apps.get(GITHUB_APPS[tool])
        if app is None:
            raise ConnectionError_(f"{TOOL[tool].name} isn't set up on this Valxel: its GitHub App settings are missing")
        return f"{self.web}/apps/{quote(app.slug)}/installations/new?{urlencode({'state': state})}"

    def _json(self, req: Request):
        try:
            with self.opener.open(req, timeout=15) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except HTTPError as exc:
            raise ConnectionError_(f"GitHub said {exc.code} to {req.get_method()} {req.full_url.split('?')[0]}") from exc

    def _get(self, path: str, token: str):
        return self._json(Request(f"{self.api}{path}", headers={
            "Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "valxel-sdlc"}))

    def verify(self, tool: str, *, code: str, installation_id: int) -> dict:
        """The installation, as the person who made it sees it: account, repos and permissions.
        Raises when they can't see it (it isn't theirs) or it belongs to another App."""
        app = self.apps.get(GITHUB_APPS[tool])
        if app is None:
            raise ConnectionError_(f"{TOOL[tool].name} isn't set up on this Valxel")
        if not code:
            raise ConnectionError_("GitHub didn't confirm who installed the App. Turn on \"Request user authorization "
                                   "(OAuth) during installation\" in the App's settings, then install again")
        body = urlencode({"client_id": app.client_id, "client_secret": app.client_secret, "code": code}).encode()
        tok = self._json(Request(f"{self.web}/login/oauth/access_token", data=body, method="POST",
                                 headers={"Accept": "application/json", "User-Agent": "valxel-sdlc"}))
        user_token = tok.get("access_token") or ""
        if not user_token:
            raise ConnectionError_("GitHub didn't accept the install's confirmation; install again")
        inst = None
        for page in range(1, 11):
            got = self._get(f"/user/installations?per_page=100&page={page}", user_token)
            items = got.get("installations") or []
            inst = next((i for i in items if int(i.get("id", 0)) == int(installation_id)), None)
            if inst or len(items) < 100:
                break
        if inst is None:
            raise ConnectionError_("That installation isn't one you can see in GitHub, so it can't join this workspace")
        if str(inst.get("app_id")) != str(app.app_id):
            raise ConnectionError_(f"That installation is of another GitHub App, not {TOOL[tool].name}'s")
        repos: list[str] = []
        if inst.get("repository_selection") != "all":
            for page in range(1, 11):
                got = self._get(f"/user/installations/{int(installation_id)}/repositories?per_page=100&page={page}",
                                user_token)
                items = got.get("repositories") or []
                repos += [r["full_name"] for r in items]
                if len(items) < 100:
                    break
        account = inst.get("account") or {}
        return {"installation_id": int(installation_id), "account": account.get("login", ""),
                "account_type": account.get("type", ""), "selection": inst.get("repository_selection", ""),
                "repos": sorted(repos), "permissions": inst.get("permissions") or {},
                "suspended": bool(inst.get("suspended_at"))}


class AwsConnect:
    """AWS's side: the console's quick-create page for the customer stack (deploy/customer-stack.yaml,
    hosted at `template_url`), and assuming its roles with the workspace's external ID. Valxel's
    account is the only one the roles trust; a session lasts 15 minutes."""

    def __init__(self, *, valxel_account: str, template_url: str, assume: Callable[..., dict] | None = None) -> None:
        self.valxel_account, self.template_url = valxel_account, template_url
        if assume is None:
            from .cloud.aws.accounts import sts_assume

            assume = sts_assume
        self._assume: Callable[..., dict] = assume

    @classmethod
    def from_env(cls, env=None) -> AwsConnect | None:
        env = env if env is not None else os.environ
        acct, url = env.get("VALXEL_AWS_ACCOUNT_ID", ""), env.get("VALXEL_AWS_STACK_TEMPLATE_URL", "")
        return cls(valxel_account=acct, template_url=url) if acct and url.startswith("https://") else None

    @staticmethod
    def roles(workspace: str, account: str) -> dict[str, str]:
        base = f"arn:aws:iam::{account}:role/valxel/valxel-{workspace}"
        return {"deploy": f"{base}-deploy", "agents": f"{base}-agents"}

    def launch_url(self, workspace: str, *, external_id: str, mode: str, region: str) -> str:
        q = urlencode({"templateURL": self.template_url, "stackName": f"valxel-{workspace}",
                       "param_ValxelAccountId": self.valxel_account, "param_ExternalId": external_id,
                       "param_WorkspaceId": workspace, "param_Mode": mode})
        return f"https://console.aws.amazon.com/cloudformation/home?region={quote(region)}#/stacks/quickcreate?{q}"

    def check(self, workspace: str, *, account: str, external_id: str) -> dict:
        roles = self.roles(workspace, account)
        for job, arn in roles.items():
            try:
                who = self._assume(arn, external_id=external_id, session=f"valxel-{workspace}-check")
            except Exception as exc:
                raise ConnectionError_(aws_reason(exc, job, account)) from exc
            if str(who.get("Account")) != account:
                raise ConnectionError_(f"The {job} role answered from account {who.get('Account')}, not {account}")
        return {"account_id": account, "deploy_role": roles["deploy"], "agents_role": roles["agents"]}


def aws_reason(exc: Exception, job: str, account: str) -> str:
    text = str(exc)
    if "AccessDenied" in text or "not authorized" in text:
        return (f"Valxel can't assume the {job} role in {account}: the stack isn't there (still creating, failed or "
                "deleted), or it was made for another workspace. Open the stack in CloudFormation to see why")
    return f"Checking the {job} role in {account} failed: {text[:200]}"


def aws_health(conns: Connections) -> Callable[[dict], str]:
    """The stack's roles still assume: a deleted stack shows within the hour."""

    def check(c: dict) -> str:
        if conns.aws is None:
            return ""
        try:
            conns.aws.check(c["workspace"], account=c["account"], external_id=conns.external_id(c["workspace"]))
        except ConnectionError_ as exc:
            return str(exc)
        return ""

    return check


class Connections:
    """A workspace's connections, kept beside its members and groups (valxel_sdlc.workspaces), with
    their changes in its audit log."""

    def __init__(self, workspaces, *, github: GitHubInstalls | None = None, aws: AwsConnect | None = None, notify=None,
                 health: dict[str, Callable[[dict], str]] | None = None, clock: Callable[[], float] = time.time,
                 oauth=None) -> None:
        self.workspaces, self.github, self.aws, self.notify, self.clock = workspaces, github, aws, notify, clock
        self.oauth = oauth  # valxel_sdlc.oauth_connections.OAuthTools: Jira, Lucid, Sentry, PagerDuty
        self.billing = None  # valxel_sdlc.billing.BillingProvider: going live may wait for a card
        # tool -> check(record): "" when it works, otherwise what's wrong in plain words
        self.health_checks = dict(health or {})

    @property
    def store(self):
        return self.workspaces.store

    # ------------------------------------------------------------------ records
    def list(self, workspace: str) -> list[dict]:
        return sorted(self.store.list("connection", workspace), key=lambda c: (c["tool"], c["account"]))

    def of(self, workspace: str, tool: str) -> list[dict]:
        return [c for c in self.list(workspace) if c["tool"] == tool]

    def page(self, workspace: str) -> list[dict]:
        """Every tool, connected or not: what the connections page shows."""
        out = []
        for t in TOOLS:
            rows = self.of(workspace, t.id)
            status = ("needs_attention" if any(c["status"] == "needs_attention" for c in rows)
                      else "connected" if rows else "not_connected")
            out.append({"tool": t.id, "name": t.name, "asked_when": t.asked_when, "grants": t.grants,
                        "revoke": t.revoke, "available": t.available and self._can_connect(t.id),
                        "status": status, "connections": rows})
        return out

    def _can_connect(self, tool: str) -> bool:
        if tool == "aws":
            return self.aws is not None
        if self.oauth is not None and self.oauth.available(tool):
            return True
        return tool in GITHUB_APPS and bool(self.github and self.github.apps.get(GITHUB_APPS[tool]))

    def record(self, workspace: str, tool: str, account: str, *, actor: str, reach: dict) -> dict:
        key = f"{tool}:{account.lower()}"
        old = self.store.get("connection", workspace, key)
        now = self.clock()
        rec = {"workspace": workspace, "tool": tool, "account": account, "status": "connected",
               "connected_by": actor, "connected_at": now, "last_ok": now, "last_error": "", "reach": reach}
        self.store.put("connection", workspace, key, rec)
        self.store.audit(workspace, actor, "connection.reconnect" if old else "connection.add",
                         {"tool": tool, "account": account})
        return rec

    def disconnect(self, workspace: str, tool: str, account: str, *, actor: str) -> dict:
        key = f"{tool}:{account.lower()}"
        rec = self.store.get("connection", workspace, key)
        if not rec:
            raise KeyError(f"No {tool} connection for {account}")
        self.store.delete("connection", workspace, key)
        if self.oauth is not None and tool in OAUTH_TOOLS:
            try:
                self.oauth.forget(workspace, tool, rec["account"])
            except Exception:
                log.exception("Couldn't delete the %s tokens for %s", tool, workspace)
        self.store.audit(workspace, actor, "connection.remove", {"tool": tool, "account": account})
        return {"removed": rec, "revoke": TOOL[tool].revoke}

    # ------------------------------------------------------------------ GitHub
    def github_state(self, signer, *, workspace: str, tool: str, actor: str, back: str = "") -> str:
        """The signed state an install carries through GitHub and back: which workspace, which
        App, who started it, where to return (a local page, e.g. the sign-up wizard), for 30 minutes."""
        if tool not in GITHUB_APPS:
            raise ConnectionError_(f"{tool} isn't a GitHub App")
        return signer.dumps({"ws": workspace, "tool": tool, "by": actor, "n": secrets.token_urlsafe(8),
                             "exp": int(self.clock()) + 1800, **({"back": back} if back else {})})

    def github_installed(self, signer, *, state: str, code: str, installation_id: str, actor: str) -> dict:
        """GitHub's redirect after an install: check it, then record the installation."""
        st = signer.loads(state)
        if not st or st.get("tool") not in GITHUB_APPS:
            raise ConnectionError_("This install link expired or was started elsewhere; start it again from the "
                                   "connections page")
        if st.get("by") != actor:
            raise ConnectionError_("Someone else started this install; start your own from the connections page")
        if self.github is None:
            raise ConnectionError_("GitHub installs aren't set up on this Valxel")
        try:
            iid = int(installation_id)
        except (TypeError, ValueError):
            raise ConnectionError_("GitHub didn't say which installation was made; install again") from None
        got = self.github.verify(st["tool"], code=code, installation_id=iid)
        if got["suspended"]:
            raise ConnectionError_(f"The installation on {got['account']} is suspended in GitHub; unsuspend it first")
        rec = self.record(st["ws"], st["tool"], got["account"], actor=actor, reach=got)
        return {**rec, "workspace": st["ws"]}

    def github_missing(self, workspace: str, orgs: list[str], repos: list[str] = ()) -> list[str]:
        """What GitHub access a product lacks, in plain words: an org with no Delivery installation,
        or a repo the installation was given no access to."""
        installs = {c["account"].lower(): c for c in self.of(workspace, "github") if c["status"] == "connected"}
        out = []
        for org in sorted({o for o in orgs if o}):
            inst = installs.get(org.lower())
            if inst is None:
                out.append(f"the Valxel GitHub App isn't installed on {org}")
                continue
            reach = inst.get("reach") or {}
            if reach.get("selection") != "all":
                given = {r.lower() for r in reach.get("repos") or []}
                out += [f"{r} isn't in the GitHub App's repos" for r in repos
                        if r.split("/", 1)[0].lower() == org.lower() and r.lower() not in given]
        return out

    # ------------------------------------------------------------------ OAuth tools
    def oauth_start(self, signer, *, workspace: str, tool: str, actor: str) -> tuple[str, str]:
        """(the vendor's consent screen, the signed state for the cookie)."""
        if self.oauth is None or not self.oauth.available(tool):
            raise ConnectionError_(f"{TOOL[tool].name if tool in TOOL else tool} isn't set up on this Valxel")
        from .oauth_connections import OAuthError

        try:
            return self.oauth.start(signer, workspace=workspace, tool=tool, actor=actor)
        except OAuthError as exc:
            raise ConnectionError_(str(exc)) from exc

    def oauth_finished(self, signer, *, state: str, cookie_state: str, code: str, actor: str,
                       installation: str = "", org: str = "") -> dict:
        if self.oauth is None:
            raise ConnectionError_("OAuth connections aren't set up on this Valxel")
        from .oauth_connections import OAuthError

        try:
            got = self.oauth.finish(signer, state=state, cookie_state=cookie_state, code=code, actor=actor,
                                    installation=installation, org=org)
        except OAuthError as exc:
            raise ConnectionError_(str(exc)) from exc
        rec = self.record(got["workspace"], got["tool"], got["account"], actor=actor, reach=got["reach"])
        return {**rec, "workspace": got["workspace"]}

    # ------------------------------------------------------------------ AWS
    def external_id(self, workspace: str, actor: str = "valxel") -> str:
        """The workspace's external ID: made once, unguessable, and sent with every role assumption, so
        a request about one customer can't use another's roles (the confused deputy)."""
        ws = self.workspaces.get(workspace) or self.workspaces.ensure(workspace, actor) or self.workspaces.get(workspace)
        if not ws.get("aws_external_id"):
            ws = {**ws, "aws_external_id": f"valxel-{workspace}-{secrets.token_urlsafe(24)}"}
            self.store.put("workspace", workspace, "", ws)
        return ws["aws_external_id"]

    def aws_launch(self, workspace: str, *, mode: str, region: str, actor: str) -> str:
        if self.aws is None:
            raise ConnectionError_("AWS connections aren't set up on this Valxel")
        if self.billing is not None:
            from .billing import blocks_go_live

            why = blocks_go_live(self.workspaces.get(workspace) or {"id": workspace}, self.billing)
            if why:
                raise ConnectionError_(why)
        if mode not in ("lite", "full"):
            raise ConnectionError_("Choose lite or full mode")
        url = self.aws.launch_url(workspace, external_id=self.external_id(workspace, actor), mode=mode, region=region)
        self.store.audit(workspace, actor, "connection.aws_stack_opened", {"mode": mode, "region": region})
        return url

    def aws_check(self, workspace: str, *, account: str, mode: str, actor: str) -> dict:
        """Assume the stack's roles with the workspace's external ID; record the account when both work."""
        if self.aws is None:
            raise ConnectionError_("AWS connections aren't set up on this Valxel")
        account = account.strip().replace("-", "")
        if not (account.isdigit() and len(account) == 12):
            raise ConnectionError_("An AWS account ID is 12 digits")
        reach = self.aws.check(workspace, account=account, external_id=self.external_id(workspace, actor))
        return self.record(workspace, "aws", account, actor=actor, reach={**reach, "mode": mode})

    # ------------------------------------------------------------------ health
    def check(self, workspace: str) -> list[dict]:
        """One cheap read per connection; a failure turns it to Needs attention and emails the
        admins once. Returns the connections that changed."""
        changed = []
        for c in self.list(workspace):
            fn = self.health_checks.get(c["tool"])
            if fn is None:
                continue
            try:
                problem = fn(c)
            except Exception as exc:  # the check itself failing is a problem with the connection
                problem = f"couldn't be checked ({type(exc).__name__})"
            key = f"{c['tool']}:{c['account'].lower()}"
            if problem:
                if c["status"] != "needs_attention" or c.get("last_error") != problem:
                    c.update(status="needs_attention", last_error=problem, failed_at=self.clock())
                    self.store.put("connection", workspace, key, c)
                    self.store.audit(workspace, "valxel", "connection.failed", {"tool": c["tool"], "account": c["account"],
                                                                                "why": problem})
                    self._tell_admins(workspace, c)
                    changed.append(c)
            else:
                was = c["status"]
                c.update(status="connected", last_error="", last_ok=self.clock())
                self.store.put("connection", workspace, key, c)
                if was != "connected":
                    self.store.audit(workspace, "valxel", "connection.recovered", {"tool": c["tool"], "account": c["account"]})
                    changed.append(c)
        return changed

    def check_all(self) -> int:
        n = 0
        for ws in {w["id"] for w in self.store.list("workspace")}:
            n += len(self.check(ws))
        return n

    def _tell_admins(self, workspace: str, c: dict) -> None:
        if self.notify is None:
            return
        admins = [m["email"] for m in self.workspaces.members(workspace)
                  if m.get("status") == "active" and m.get("role") in ("owner", "admin") and m.get("email")]
        name = TOOL.get(c["tool"]).name if c["tool"] in TOOL else c["tool"]
        for to in admins:
            try:
                self.notify(to, f"{name} needs attention in Valxel",
                            f"Valxel's {name} connection for {c['account']} stopped working: {c['last_error']}.\n\n"
                            "Features that need it will wait until it's fixed. Reconnect it from the workspace's "
                            "connections page.")
            except Exception:
                log.exception("Couldn't email %s about %s", to, c["tool"])


def delivery_health(tokens) -> Callable[[dict], str]:
    """The Delivery App's own view of an installation: gone or suspended means Needs attention."""

    def check(c: dict) -> str:
        iid = (c.get("reach") or {}).get("installation_id")
        if not iid:
            return ""
        try:
            inst = tokens._call("GET", f"/app/installations/{int(iid)}", tokens._jwt())
        except Exception as exc:
            if "404" in str(exc):
                return f"the GitHub App was uninstalled from {c['account']}"
            raise
        if inst.get("suspended_at"):
            return f"the GitHub App is suspended on {c['account']}"
        return ""

    return check


def connection_wait(services, state: dict) -> dict | None:
    """What a self-serve workspace's feature is missing before it touches the repos, as an
    external wait's packet, or None. Workspaces set up before connections (an existing deployment,
    with its Apps installed by hand) never wait."""
    conns = getattr(services, "connections", None)
    wss = getattr(services, "workspaces", None)
    if conns is None or wss is None:
        return None
    snap = services.snapshot(state["product_id"], state.get("governance"))
    ws = wss.get(snap.product.workspace) or {}
    if not ws.get("self_serve"):
        return None
    repos = [r.name for r in snap.product.repos]
    missing = conns.github_missing(snap.product.workspace, [snap.product.org], repos)
    if not missing:
        return None
    return {"type": "external", "wait": "connection", "tool": "github", "workspace": snap.product.workspace,
            "product_id": state["product_id"], "feature_id": state["feature_id"],
            "orgs": [snap.product.org], "repos": repos,
            "label": "Waiting for: connect GitHub (" + "; ".join(missing) + ")"}

"""Work tracking: the feature's epic, stories (one per acceptance criterion) and tasks (one per
planned repo change) in the product's Jira or OpenProject project, kept in step with the
pipeline by vrixel_sdlc.worksync.

Every item carries a marker (feature id plus item key) so a retried sync finds what it created
instead of creating it twice: a label in Jira, a subject prefix in OpenProject.

Tool names and argument names are resolved against the server's published tool schemas, so an
adapter keeps working when a server renames a parameter or an admin renames a tool.
"""

from __future__ import annotations

import itertools
import re
import threading
from typing import Any, Protocol

from .mcp_client import McpError, McpTools, fit_args, pick_tool

STATUS_ORDER = {"todo": 0, "in_progress": 1, "done": 2, "cancelled": 2}


class WorkTracker(Protocol):
    provider: str

    def find(self, cfg: dict, marker: str) -> dict | None: ...
    def create(self, cfg: dict, *, kind: str, summary: str, description: str, marker: str,
               parent: dict | None) -> dict: ...
    def set_status(self, cfg: dict, item: dict, status: str) -> None: ...  # status: in_progress|done|cancelled
    def comment(self, cfg: dict, item: dict, text: str) -> None: ...
    # The item as the tracker has it now (for reconciling): {"status": todo|in_progress|done|cancelled,
    # "summary": ...}, or None when it no longer exists.
    def get(self, cfg: dict, item: dict) -> dict | None: ...


def status_from(cfg: dict, name: str) -> str:
    """A tracker status name read back as ours (done wins where done and cancelled share a name)."""
    n = (name or "").strip().lower()
    names = {k: str(v).lower() for k, v in (cfg.get("statuses") or {}).items()}
    for ours in ("done", "cancelled", "in_progress"):
        if names.get(ours) == n:
            return ours
    return "todo"


def marker_label(marker: str) -> str:
    """Jira labels cannot contain spaces: 'FARM-FEAT-1 AC-1' -> 'vrixel-FARM-FEAT-1-AC-1'."""
    return "vrixel-" + re.sub(r"[^A-Za-z0-9_-]+", "-", marker).strip("-")


def _items(res: Any, *keys: str) -> list[dict]:
    if isinstance(res, list):
        return [r for r in res if isinstance(r, dict)]
    if isinstance(res, dict):
        for k in keys:
            if isinstance(res.get(k), list):
                return [r for r in res[k] if isinstance(r, dict)]
        emb = res.get("_embedded")
        if isinstance(emb, dict):
            return _items(emb, *keys)
        if "id" in res:  # a one-element list can arrive as the element itself
            return [res]
    return []


# --------------------------------------------------------------------------- Jira


class JiraTracker:
    """Atlassian Rovo MCP server. Needs read_jira, write_jira and search_jira."""

    provider = "jira"

    def __init__(self, server: McpTools, cloud_id: str = "") -> None:
        self.server = server
        self._cloud_id = cloud_id

    def _cloud(self) -> str:
        if not self._cloud_id:
            tool = pick_tool(self.server, "getAccessibleAtlassianResources")
            sites = _items(self.server.call(tool, {}), "resources", "sites")
            if not sites:
                raise McpError("atlassian: the token can reach no Atlassian site; set VRIXEL_ATLASSIAN_CLOUD_ID")
            self._cloud_id = str(sites[0].get("id") or sites[0].get("cloudId"))
        return self._cloud_id

    def _call(self, candidates: tuple[str, ...], wanted: dict) -> Any:
        tool = pick_tool(self.server, *candidates)
        return self.server.call(tool, fit_args(self.server, tool, {("cloudId",): self._cloud(), **wanted}))

    @staticmethod
    def _ref(issue: dict, cfg: dict) -> dict:
        key = str(issue.get("key") or issue.get("issueKey") or "")
        base = str(cfg.get("site_url") or "").rstrip("/")
        url = issue.get("url") or issue.get("webUrl") or (f"{base}/browse/{key}" if base else "")
        return {"id": str(issue.get("id", key)), "key": key, "url": url}

    def find(self, cfg, marker):
        jql = f'project = "{cfg["project"]}" AND labels = "{marker_label(marker)}"'
        res = self._call(("searchJiraIssuesUsingJql",), {("jql",): jql, ("maxResults", "limit"): 1})
        found = _items(res, "issues", "results")
        return self._ref(found[0], cfg) if found else None

    def create(self, cfg, *, kind, summary, description, marker, parent):
        fields: dict = {"labels": ["vrixel", marker_label(marker)]}
        if parent:
            fields["parent"] = {"key": parent["key"]}
        res = self._call(("createJiraIssue",), {
            ("projectKey", "project_key"): cfg["project"],
            ("issueTypeName", "issue_type", "issueType"): cfg[f"{kind}_type"],
            ("summary",): summary[:250],
            ("description",): description,
            ("additional_fields", "additionalFields", "fields"): fields,
        })
        ref = self._ref(res if isinstance(res, dict) else {}, cfg)
        if not ref["key"]:
            raise McpError(f"atlassian: createJiraIssue returned no key: {str(res)[:200]}")
        return ref

    def set_status(self, cfg, item, status):
        target = cfg["statuses"][status].lower()
        res = self._call(("listJiraIssueTransitions", "getTransitionsForJiraIssue"),
                         {("issueIdOrKey", "issueKey"): item["key"]})
        for t in _items(res, "transitions"):
            names = {str(t.get("name", "")).lower(), str((t.get("to") or {}).get("name", "")).lower()}
            if target in names:
                self._call(("transitionJiraIssue",), {("issueIdOrKey", "issueKey"): item["key"],
                                                      ("transition",): {"id": str(t["id"])}})
                return
        raise McpError(f"jira: {item['key']} has no transition to {cfg['statuses'][status]!r}")

    def comment(self, cfg, item, text):
        self._call(("addOrEditJiraIssueComment", "addCommentToJiraIssue"),
                   {("issueIdOrKey", "issueKey"): item["key"], ("commentBody", "body", "comment"): text})

    def get(self, cfg, item):
        try:
            res = self._call(("getJiraIssue",), {("issueIdOrKey", "issueKey"): item["key"],
                                                 ("fields",): ["summary", "status"]})
        except McpError as exc:
            if "404" in str(exc) or "not exist" in str(exc).lower() or "not found" in str(exc).lower():
                return None
            raise
        fields = (res or {}).get("fields") or res or {}
        status = fields.get("status") or {}
        return {"status": status_from(cfg, status.get("name", "") if isinstance(status, dict) else str(status)),
                "summary": str(fields.get("summary", ""))}


# --------------------------------------------------------------------------- OpenProject


class OpenProjectTracker:
    """OpenProject's built-in MCP server (17.2+). Types and statuses are matched by name."""

    provider = "openproject"

    def __init__(self, server: McpTools, base_url: str = "") -> None:
        self.server = server
        self.base_url = base_url.rstrip("/")
        self._lookups: dict[str, dict[str, str]] = {}
        self._lock = threading.Lock()

    def _by_name(self, tool: str, name: str) -> str:
        with self._lock:
            if tool not in self._lookups:
                rows = _items(self.server.call(tool, {}), "elements", "items", "results", tool.split("_", 1)[-1])
                self._lookups[tool] = {str(r.get("name", "")).lower(): str(r.get("id")) for r in rows}
        found = self._lookups[tool].get(name.lower())
        if not found:
            raise McpError(f"openproject: {tool} has no entry named {name!r}")
        return found

    def _named(self, tool: str, prop_names: tuple[str, ...], lookup: str, name: str) -> dict:
        """Pass a type or status by name when the tool accepts one, else by id."""
        props = set((self.server.tool_schema(tool).get("properties") or {}).keys())
        by_name = next((p for p in prop_names if p in props), None)
        if by_name or not props:
            return {by_name or prop_names[0]: name}
        by_id = next((p for p in (f"{n}_id" for n in prop_names) if p in props), None)
        return {by_id: self._by_name(lookup, name)} if by_id else {}

    def _ref(self, wp: dict) -> dict:
        wid = str(wp.get("id", ""))
        url = wp.get("url") or (f"{self.base_url}/work_packages/{wid}" if self.base_url and wid else "")
        return {"id": wid, "key": f"#{wid}", "url": url}

    def find(self, cfg, marker):
        tool = pick_tool(self.server, "search_work_packages")
        res = self.server.call(tool, fit_args(self.server, tool, {
            ("query", "search", "subject", "filter"): f"[{marker}]",
            ("project", "project_id", "project_identifier"): cfg["project"]}))
        for wp in _items(res, "work_packages", "elements", "results", "items"):
            if str(wp.get("subject", "")).startswith(f"[{marker}]"):
                return self._ref(wp)
        return None

    def create(self, cfg, *, kind, summary, description, marker, parent):
        tool = pick_tool(self.server, "create_work_package")
        args = fit_args(self.server, tool, {
            ("project", "project_id", "project_identifier"): cfg["project"],
            ("subject", "title"): summary[:250],
            ("description",): description,
            ("parent_id", "parent"): int(parent["id"]) if parent and str(parent["id"]).isdigit() else None,
        })
        args |= self._named(tool, ("type",), "list_types", cfg[f"{kind}_type"])
        res = self.server.call(tool, args)
        ref = self._ref(res if isinstance(res, dict) else {})
        if not ref["id"]:
            raise McpError(f"openproject: create_work_package returned no id: {str(res)[:200]}")
        return ref

    def set_status(self, cfg, item, status):
        tool = pick_tool(self.server, "update_work_package")
        args = fit_args(self.server, tool, {("id", "work_package_id"): int(item["id"])})
        args |= self._named(tool, ("status",), "list_statuses", cfg["statuses"][status])
        self.server.call(tool, args)

    def comment(self, cfg, item, text):
        tool = pick_tool(self.server, "create_work_package_comment")
        self.server.call(tool, fit_args(self.server, tool, {
            ("work_package_id", "id"): int(item["id"]), ("comment", "body", "text"): text}))

    def get(self, cfg, item):
        tool = pick_tool(self.server, "get_work_package")
        try:
            wp = self.server.call(tool, fit_args(self.server, tool, {("id", "work_package_id"): int(item["id"])}))
        except McpError as exc:
            if "404" in str(exc) or "not found" in str(exc).lower():
                return None
            raise
        wp = wp if isinstance(wp, dict) else {}
        status = (wp.get("_links") or {}).get("status") or wp.get("status") or {}
        name = status.get("title") or status.get("name") if isinstance(status, dict) else str(status)
        return {"status": status_from(cfg, name or ""), "summary": str(wp.get("subject", ""))}


# --------------------------------------------------------------------------- offline stand-in


class StubTracker:
    """In-memory tracker that behaves like a project: items, statuses and comments."""

    def __init__(self, provider: str) -> None:
        self.provider = provider
        self.items: dict[str, dict] = {}  # id -> item
        self._seq = itertools.count(1)
        self._lock = threading.Lock()
        self.fail_next = 0  # tests: make the next N calls raise

    def _maybe_fail(self) -> None:
        if self.fail_next:
            self.fail_next -= 1
            raise McpError(f"{self.provider}: simulated outage")

    def find(self, cfg, marker):
        self._maybe_fail()
        with self._lock:
            for item in self.items.values():
                if item["marker"] == marker and item["project"] == cfg["project"]:
                    return {k: item[k] for k in ("id", "key", "url")}
        return None

    def create(self, cfg, *, kind, summary, description, marker, parent):
        self._maybe_fail()
        with self._lock:
            n = next(self._seq)
            key = f"{cfg['project'].upper()}-{n}" if self.provider == "jira" else f"#{n}"
            url = (f"https://example.atlassian.net/browse/{key}" if self.provider == "jira"
                   else f"https://openproject.example.com/work_packages/{n}")
            self.items[str(n)] = {"id": str(n), "key": key, "url": url, "kind": kind, "summary": summary,
                                  "description": description, "marker": marker, "project": cfg["project"],
                                  "parent": parent["id"] if parent else None, "status": "todo", "comments": []}
            return {"id": str(n), "key": key, "url": url}

    def set_status(self, cfg, item, status):
        self._maybe_fail()
        with self._lock:
            self.items[item["id"]]["status"] = status

    def comment(self, cfg, item, text):
        self._maybe_fail()
        with self._lock:
            self.items[item["id"]]["comments"].append(text)

    def get(self, cfg, item):
        self._maybe_fail()
        with self._lock:
            it = self.items.get(item["id"])
            return {"status": it["status"], "summary": it["summary"]} if it else None

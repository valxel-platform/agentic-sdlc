"""Diagram publishing (Lucid, draw.io) and work tracking (Jira, OpenProject).

The Lucid, Jira and OpenProject adapters are exercised over the MCP protocol against fake
in-process servers whose tool names and arguments follow each vendor's published tools.
"""

import json
import shutil
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from mcp.server import MCPServer

from vrixel_sdlc.config import load_settings
from vrixel_sdlc.engine import FeatureEngine
from vrixel_sdlc.governance import GovernanceError
from vrixel_sdlc.integrations.diagrams import (
    LucidPublisher, decode_editor_url, drawio_editor_url, parse_flowchart, to_drawio,
)
from vrixel_sdlc.integrations.mcp_client import McpHttp
from vrixel_sdlc.integrations.worktracking import JiraTracker, OpenProjectTracker, StubTracker
from vrixel_sdlc.services import build_services

EXAMPLES = Path(__file__).resolve().parents[1] / "examples" / "governance"

FLOW = """flowchart LR
  subgraph FARM["Farm app"]
    mobile["farm-mobile (consumer)"]
    api["farm-field-api (provider)"]
  end
  db[(Field DB)]
  mobile -->|"versioned API"| api
  api -- writes --> db
"""


@pytest.fixture
def gov_root(tmp_path):
    root = tmp_path / "governance"
    shutil.copytree(EXAMPLES, root)
    return root


def make_engine(tmp_path, gov_root, **kw):
    s = load_settings(mode="stub", database_url="", artifact_dir=tmp_path / "artifacts",
                      governance_source="local", governance_root=gov_root)
    svc = build_services(s, **kw)
    return FeatureEngine(svc, InMemorySaver()), svc


def edit(path: Path, old: str, new: str) -> None:
    text = path.read_text()
    assert old in text
    path.write_text(text.replace(old, new))


# --------------------------------------------------------------------------- diagrams


def test_flowchart_converts_to_a_drawio_file_and_editor_link():
    fc = parse_flowchart(FLOW)
    assert fc.groups == {"FARM": "Farm app"}
    assert fc.nodes["db"]["shape"] == "database"
    assert fc.edges == [("mobile", "api", "versioned API"), ("api", "db", "writes")]
    xml = to_drawio(fc, "FARM-FEAT-1 v1")
    root = ET.fromstring(xml)
    cells = root.findall(".//mxCell")
    assert sum(1 for c in cells if c.get("vertex")) == 4  # one group, three nodes
    assert sum(1 for c in cells if c.get("edge")) == 2
    assert decode_editor_url(drawio_editor_url(xml)) == xml


def test_architecture_publishes_drawio_into_the_feature_branch(tmp_path, gov_root):
    eng, svc = make_engine(tmp_path, gov_root)
    v = eng.start(product_id="CLN", title="Consent check on intake", description="Block intake until consent")
    v = eng.auto_drive(v["feature_id"])
    (diagram,) = v["diagrams"]
    assert diagram["provider"] == "drawio" and diagram["version"] == 1
    path = ("acme-clinic/cln-intake-api", "feature/CLN-FEAT-1", "docs/architecture/CLN-FEAT-1.drawio")
    assert path in svc.github.files
    ET.fromstring(svc.github.files[path])
    hld = eng.artifact("CLN-FEAT-1", "HLD")["content"]
    assert "## Diagrams" in hld and "app.diagrams.net" in hld


def test_diagram_is_withheld_when_the_change_touches_a_restricted_lane(tmp_path, gov_root):
    eng, svc = make_engine(tmp_path, gov_root)
    v = eng.start(product_id="FARM", title="Audit trail for field edits",
                  description="Record who changed a field, with the shared audit framework")
    v = eng.auto_drive(v["feature_id"])
    assert v["diagrams"] == []
    assert any(e["kind"] == "diagram_withheld" and "SS" in e["detail"] for e in v["events"])
    assert svc.diagrams["lucid"].published == []


def test_a_failed_diagram_publish_does_not_fail_the_phase(tmp_path, gov_root):
    class Broken:
        provider = "lucid"

        def publish(self, req):
            raise RuntimeError("Lucid is down")

    eng, svc = make_engine(tmp_path, gov_root)
    svc.diagrams["lucid"] = Broken()
    v = eng.start(product_id="FARM", title="Offline crop log", description="Record observations offline")
    v = eng.auto_drive(v["feature_id"])
    assert v["status"] == "done" and v["diagrams"] == []
    assert any(e["kind"] == "diagram_failed" for e in v["events"])


def fake_lucid():
    server, calls = MCPServer("lucid"), []

    @server.tool()
    def lucid_create_diagram_from_mermaid(title: str, mermaid_markup: str, product: str = "lucidchart") -> str:
        calls.append(("create", title, mermaid_markup, product))
        return json.dumps({"documentId": "d0c-1", "editUrl": "https://lucid.app/lucidchart/d0c-1/edit"})

    @server.tool()
    def lucid_update_document(document_id: str, title: str | None = None, custom_tags: list | None = None) -> str:
        calls.append(("tags", document_id, custom_tags))
        return "{}"

    @server.tool()
    def lucid_create_document_share_link(document_id: str, role: str, restrict_to_account: bool = True,
                                         allow_anonymous: bool = False) -> str:
        calls.append(("share", document_id, role, restrict_to_account))
        return "https://lucid.app/lucidchart/d0c-1/view?invitationId=abc"

    return server, calls


def test_lucid_publisher_imports_mermaid_tags_and_shares_view_only(tmp_path, gov_root):
    server, calls = fake_lucid()
    eng, svc = make_engine(tmp_path, gov_root, diagrams=None)
    svc.diagrams["lucid"] = LucidPublisher(McpHttp("lucid", server))
    v = eng.start(product_id="FARM", title="Offline crop log", description="Record observations offline")
    v = eng.auto_drive(v["feature_id"])
    (d,) = v["diagrams"]
    assert d["url"] == "https://lucid.app/lucidchart/d0c-1/view?invitationId=abc"
    assert d["edit_url"].endswith("/edit") and d["id"] == "d0c-1"
    create = calls[0]
    assert create[1] == "FARM-FEAT-1 Offline crop log: architecture v1" and create[3] == "lucidchart"
    assert parse_flowchart(create[2]).nodes  # the HLD's mermaid, as written
    assert ("tags", "d0c-1", ["vrixel", "FARM-FEAT-1"]) in calls
    assert ("share", "d0c-1", "view", True) in calls


# --------------------------------------------------------------------------- work tracking


def test_tracker_follows_the_feature_from_intent_to_release(tmp_path, gov_root):
    eng, svc = make_engine(tmp_path, gov_root)
    jira = svc.trackers["jira"]
    v = eng.start(product_id="FARM", title="Offline crop log", description="Record observations offline")
    fid = v["feature_id"]
    assert v["work_tracking"]["items"] == []  # nothing until G1 is approved

    eng.decide(fid, v["pending"][0]["interrupt_id"], {"decision": "approve", "approver": "maria"})
    items = eng.view(fid)["work_tracking"]["items"]
    assert [i["item"] for i in items] == ["epic"] and items[0]["status"] == "todo"

    v = eng.auto_drive(fid, approver="maria")
    items = v["work_tracking"]["items"]
    assert sorted(i["item"] for i in items) == sorted(
        ["epic", "AC-1", "AC-2", "AC-3", "NFR-1", "NFR-2", "TASK-1", "TASK-2"] + [f"TC-{n}" for n in range(1, 11)])
    assert all(i["status"] == "done" for i in items)
    epic = next(x for x in jira.items.values() if x["kind"] == "epic")
    stories = [x for x in jira.items.values() if x["kind"] in ("story", "task")]
    assert all(x["parent"] == epic["id"] for x in stories)
    by_key = {x["key"]: x for x in jira.items.values()}
    story_of = {x["summary"].split("]")[0].split("/")[-1]: x["id"] for x in jira.items.values() if x["kind"] == "story"}
    subtasks = [x for x in jira.items.values() if x["kind"] == "subtask"]
    assert len(subtasks) == 10 and by_key  # one per test case, each under its requirement's story
    assert all(x["parent"] == story_of[x["summary"].split("(")[1].split(",")[0]] for x in subtasks)
    assert eng.work.reconcile(eng._state(fid)) == {"drift": [], "block": False}
    assert epic["comments"][0] == "G1 Intent approved by maria."
    assert any("Architecture diagram v1 (lucid)" in c for c in epic["comments"])

    before = (len(jira.items), len(epic["comments"]))
    assert eng.sync_work(fid) == {"provider": "jira", "created": 0, "moved": 0, "commented": 0}
    assert (len(jira.items), len(epic["comments"])) == before


def test_stopping_a_feature_cancels_open_items(tmp_path, gov_root):
    eng, svc = make_engine(tmp_path, gov_root)
    v = eng.start(product_id="CLN", title="Consent check", description="Block intake until consent")
    v = eng.auto_drive(v["feature_id"], decisions={"g3": {"decision": "abort", "feedback": "out of scope"}})
    assert v["status"] == "aborted"
    statuses = {i["item"]: i["status"] for i in v["work_tracking"]["items"]}
    assert statuses["epic"] == "cancelled" and statuses["AC-1"] == "cancelled"
    epic = next(x for x in svc.trackers["openproject"].items.values() if x["kind"] == "epic")
    assert any("stopped by auto-approver. Feedback: out of scope" in c for c in epic["comments"])


def test_tracker_outage_never_blocks_the_pipeline_and_catches_up(tmp_path, gov_root):
    eng, svc = make_engine(tmp_path, gov_root)
    jira = svc.trackers["jira"]
    v = eng.start(product_id="FARM", title="Offline crop log", description="Record observations offline")
    jira.fail_next = 1
    eng.decide(v["feature_id"], v["pending"][0]["interrupt_id"], {"decision": "approve", "approver": "maria"})
    v = eng.view(v["feature_id"])
    assert "simulated outage" in v["work_tracking"]["error"]
    assert v["pending"] and v["pending"][0]["gate"] == "g2"  # agents carried on to G2
    eng.sync_work(v["feature_id"])
    v = eng.view(v["feature_id"])
    assert v["work_tracking"]["error"] is None and v["work_tracking"]["items"][0]["item"] == "epic"


def test_tasks_in_a_restricted_lane_carry_no_detail(tmp_path, gov_root):
    eng, svc = make_engine(tmp_path, gov_root)
    v = eng.start(product_id="FARM", title="Audit trail for field edits",
                  description="Record who changed a field, with the shared audit framework")
    v = eng.auto_drive(v["feature_id"])
    ss_task = next(x for x in svc.trackers["jira"].items.values() if "Change in SS" in x["summary"])
    assert "ss-audit-framework" not in ss_task["summary"] + ss_task["description"]


def fake_jira():
    server, store = MCPServer("atlassian"), {"issues": {}, "comments": [], "seq": 0}

    @server.tool()
    def getAccessibleAtlassianResources() -> list:
        return [{"id": "cloud-1", "url": "https://acme.atlassian.net"}]

    @server.tool()
    def searchJiraIssuesUsingJql(cloudId: str, jql: str, maxResults: int = 50) -> dict:
        label = jql.split('labels = "', 1)[1].rstrip('"')
        return {"issues": [{"key": k, "id": i["id"]} for k, i in store["issues"].items() if label in i["labels"]]}

    @server.tool()
    def createJiraIssue(cloudId: str, projectKey: str, issueTypeName: str, summary: str,
                        description: str = "", additional_fields: dict | None = None) -> dict:
        assert cloudId == "cloud-1"
        store["seq"] += 1
        key = f"{projectKey}-{store['seq']}"
        f = additional_fields or {}
        store["issues"][key] = {"id": str(1000 + store["seq"]), "type": issueTypeName, "summary": summary,
                                "labels": f.get("labels", []), "parent": (f.get("parent") or {}).get("key"),
                                "status": "To Do"}
        return {"key": key, "id": str(1000 + store["seq"])}

    @server.tool()
    def getTransitionsForJiraIssue(cloudId: str, issueIdOrKey: str) -> dict:
        return {"transitions": [{"id": "21", "name": "Start", "to": {"name": "In Progress"}},
                                {"id": "31", "name": "Done", "to": {"name": "Done"}}]}

    @server.tool()
    def transitionJiraIssue(cloudId: str, issueIdOrKey: str, transition: dict) -> str:
        store["issues"][issueIdOrKey]["status"] = {"21": "In Progress", "31": "Done"}[transition["id"]]
        return "ok"

    @server.tool()
    def addCommentToJiraIssue(cloudId: str, issueIdOrKey: str, commentBody: str) -> dict:
        store["comments"].append((issueIdOrKey, commentBody))
        return {"id": str(len(store["comments"]))}

    return server, store


def test_jira_over_the_atlassian_mcp_tools(tmp_path, gov_root):
    server, store = fake_jira()
    eng, svc = make_engine(tmp_path, gov_root)
    svc.trackers["jira"] = JiraTracker(McpHttp("atlassian", server))
    v = eng.start(product_id="FARM", title="Offline crop log", description="Record observations offline")
    v = eng.auto_drive(v["feature_id"], approver="maria")
    assert v["work_tracking"]["error"] is None
    issues = store["issues"]
    epic = issues["FARM-1"]
    assert epic["type"] == "Epic" and epic["summary"] == "[FARM-FEAT-1] Offline crop log"
    assert "vrixel-FARM-FEAT-1" in epic["labels"]
    stories = [i for i in issues.values() if i["type"] == "Story"]
    assert len(stories) == 5 and all(s["parent"] == "FARM-1" for s in stories)  # 3 criteria, 2 NFRs
    subtasks = [i for i in issues.values() if i["type"] == "Sub-task"]
    assert len(subtasks) == 10 and all(s["parent"] != "FARM-1" for s in subtasks)  # test cases, under their story
    assert all(i["status"] == "Done" for i in issues.values())
    assert ("FARM-1", "G1 Intent approved by maria.") in store["comments"]

    # Lost link store (e.g. a restore): the markers find the existing issues instead of duplicating.
    eng.work.links = type(eng.work.links)()
    eng.sync_work(v["feature_id"])
    assert len(issues) == 18


def fake_openproject():
    server, store = MCPServer("openproject"), {"wps": {}, "comments": [], "seq": 0}
    types = [{"id": 1, "name": "Task"}, {"id": 4, "name": "User story"}, {"id": 5, "name": "Epic"}]
    statuses = [{"id": 1, "name": "New"}, {"id": 7, "name": "In progress"}, {"id": 12, "name": "Closed"},
                {"id": 14, "name": "Rejected"}]

    @server.tool()
    def list_types() -> dict:
        return {"elements": types}

    @server.tool()
    def list_statuses() -> dict:
        return {"elements": statuses}

    @server.tool()
    def search_work_packages(query: str, project: str | None = None) -> dict:
        return {"elements": [{"id": i, "subject": w["subject"]} for i, w in store["wps"].items()
                             if w["subject"].startswith(query)]}

    @server.tool()
    def create_work_package(project_id: str, subject: str, type_id: int, description: str = "",
                            parent_id: int | None = None) -> dict:
        store["seq"] += 1
        store["wps"][store["seq"]] = {"project": project_id, "subject": subject, "type_id": type_id,
                                      "parent_id": parent_id, "status_id": 1}
        return {"id": store["seq"], "subject": subject}

    @server.tool()
    def update_work_package(id: int, status_id: int | None = None) -> dict:
        store["wps"][id]["status_id"] = status_id
        return {"id": id}

    @server.tool()
    def create_work_package_comment(work_package_id: int, comment: str) -> dict:
        store["comments"].append((work_package_id, comment))
        return {"id": len(store["comments"])}

    return server, store


def test_openproject_over_its_mcp_tools(tmp_path, gov_root):
    server, store = fake_openproject()
    eng, svc = make_engine(tmp_path, gov_root)
    svc.trackers["openproject"] = OpenProjectTracker(McpHttp("openproject", server),
                                                     base_url="https://op.example.com")
    v = eng.start(product_id="CLN", title="Consent check on intake", description="Block intake until consent")
    v = eng.auto_drive(v["feature_id"], approver="li")
    assert v["work_tracking"]["error"] is None
    wps = store["wps"]
    assert wps[1]["subject"] == "[CLN-FEAT-1] Consent check on intake" and wps[1]["type_id"] == 5
    stories = [w for w in wps.values() if w["type_id"] == 4]
    assert len(stories) == 5 and all(w["parent_id"] == 1 for w in stories)  # 3 criteria, 2 NFRs
    assert all(w["status_id"] == 12 for w in wps.values())  # Closed, matched by name
    assert v["work_tracking"]["items"][0]["url"] == "https://op.example.com/work_packages/1"
    assert (1, "G1 Intent approved by li.") in store["comments"]


# --------------------------------------------------------------------------- governance


@pytest.mark.parametrize("old,new,message", [
    ("provider: drawio", "provider: lucid", "lucid is not approved for phi data"),
    ("provider: drawio", "provider: visio", "must be one of"),
    ("    project: cln\n", "", "tools.work_tracking.project is required"),
])
def test_tool_choices_are_validated(tmp_path, gov_root, old, new, message):
    edit(gov_root / "acme-clinic" / "cln-governance" / "product.yaml", old, new)
    eng, svc = make_engine(tmp_path, gov_root)
    with pytest.raises(GovernanceError, match=message):
        svc.snapshot("CLN")


def test_products_without_tools_skip_publishing_and_tracking(tmp_path, gov_root):
    path = gov_root / "acme-farm" / "farm-governance" / "product.yaml"
    text = path.read_text()
    start = text.index("# Where agents publish work")
    path.write_text(text[:start] + text[text.index("budget_usd:"):])
    eng, svc = make_engine(tmp_path, gov_root)
    v = eng.start(product_id="FARM", title="Offline crop log", description="Record observations offline")
    v = eng.auto_drive(v["feature_id"])
    assert v["diagrams"] == [] and v["work_tracking"] is None
    assert "## Diagram\n" in eng.artifact(v["feature_id"], "HLD")["content"]

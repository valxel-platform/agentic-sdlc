"""Workspaces, members, roles and approver groups (the sign-in design, A1 to A3 and A10).

A workspace owns its products; an admin invites people, gives each a role, and maps approver groups
to each product's gates. Groups add to product.yaml's approvers, are read on every request, and a
feature keeps the mapping it started with."""

import time

import pytest
from fastapi.testclient import TestClient

from valxel_sdlc.api.app import create_app
from valxel_sdlc.cli import _workspaces
from valxel_sdlc.web.auth import AuthConfig, User
from valxel_sdlc.workspaces import InMemoryWorkspaceStore, WorkspaceError, Workspaces

WS = "wrkspc-shared"  # FARM and SHOP's workspace in config/registry.yaml


@pytest.fixture
def ws(settings):
    return Workspaces(InMemoryWorkspaceStore(), settings.registry)


def owned(ws):
    return ws.bootstrap_owner(WS, {"name": "Asha", "github_login": "asha", "email": "asha@farm.example"})


def test_an_owner_is_in_every_group_and_a_workspace_keeps_one(ws):
    o = owned(ws)
    assert all(o["id"] in g["members"] for g in ws.groups(WS))
    assert {g["slug"] for g in ws.groups(WS)} == {"product", "design", "architecture", "security", "release", "on-call"}
    assert ws.products(WS) == ["FARM", "SHOP"]
    with pytest.raises(WorkspaceError, match="already has an owner"):
        owned(ws)
    with pytest.raises(WorkspaceError, match="at least one owner"):
        ws.update_member(WS, o["id"], actor="asha", actor_role="owner", role="admin")
    with pytest.raises(WorkspaceError, match="at least one owner"):
        ws.remove_member(WS, o["id"], actor="asha", actor_role="owner")
    assert [a["action"] for a in ws.store.audit_log(WS)][-1] == "workspace.create"


def test_invitations_are_for_one_person_and_used_once(ws):
    owned(ws)
    m, token = ws.invite(WS, {"name": "Ben", "email": "Ben@Farm.example"}, role="approver", actor="asha",
                         products=["FARM"])
    assert m["status"] == "invited" and m["email"] == "ben@farm.example" and "invite_hash" not in m
    with pytest.raises(PermissionError, match="someone else"):
        ws.accept(WS, token, login="mallory", email="mallory@x.example")
    got = ws.accept(WS, token, login="ben-gh", email="ben@farm.example")
    assert got["status"] == "active" and got["github_login"] == "ben-gh"
    with pytest.raises(WorkspaceError, match="isn't valid"):
        ws.accept(WS, token, login="ben-gh", email="ben@farm.example")
    with pytest.raises(WorkspaceError, match="already a member"):
        ws.invite(WS, {"email": "ben@farm.example"}, role="member", actor="asha")
    with pytest.raises(WorkspaceError, match="isn't in this workspace"):
        ws.invite(WS, {"email": "c@x.example"}, role="member", actor="asha", products=["CLN"])
    with pytest.raises(WorkspaceError, match="transferring ownership"):
        ws.invite(WS, {"email": "d@x.example"}, role="owner", actor="asha")
    [mine] = ws.memberships("BEN-GH")
    assert mine["role"] == "approver" and mine["products"] == ["FARM"]


def test_only_an_owner_makes_or_changes_an_owner(ws):
    o = owned(ws)
    a, t = ws.invite(WS, {"email": "ad@x.example"}, role="admin", actor="asha")
    ws.accept(WS, t, login="ad", email="ad@x.example")
    with pytest.raises(PermissionError):
        ws.update_member(WS, a["id"], actor="ad", actor_role="admin", role="owner")
    with pytest.raises(PermissionError):
        ws.update_member(WS, o["id"], actor="ad", actor_role="admin", status="suspended")
    ws.update_member(WS, a["id"], actor="asha", actor_role="owner", status="suspended")
    assert ws.memberships("ad") == []  # suspended: no access, no groups


def test_gate_mappings_are_versioned_and_a_feature_keeps_its_own(ws, services):
    owned(ws)
    services.workspaces = ws
    with pytest.raises(WorkspaceError, match="No such groups"):
        ws.set_mapping("FARM", {"g1": ["nope"]}, actor="asha")
    with pytest.raises(WorkspaceError, match="Unknown gates"):
        ws.set_mapping("FARM", {"g9": ["product"]}, actor="asha")
    v1 = ws.set_mapping("FARM", {"g1": ["product"], "g6": ["release"]}, actor="asha")
    snap = services.snapshot("FARM")
    assert snap.pins["groups"] == v1["version"] == 1
    assert snap.product.approvers["g1"] == ["acme-farm/product-owners", f"group:{WS}/product"]
    ws.set_mapping("FARM", {"g1": ["design"]}, actor="asha")
    assert services.snapshot("FARM").product.approvers["g1"][-1] == f"group:{WS}/design"
    pinned = services.snapshot("FARM", snap.pins)  # a feature that started on version 1
    assert pinned.product.approvers["g1"][-1] == f"group:{WS}/product"
    before = services.snapshot("FARM", {**snap.pins, "groups": 0})  # started before any mapping
    assert before.product.approvers["g1"] == ["acme-farm/product-owners"]
    with pytest.raises(WorkspaceError, match="approves gates"):
        ws.delete_group(WS, "design", actor="asha")


def test_users_match_groups_they_are_in():
    u = User(login="ben", groups=[f"{WS}/release"])
    assert u.matches(f"group:{WS}/release") and u.matches(f"group:{WS.upper()}/Release")
    assert not u.matches(f"group:{WS}/security") and not u.matches("group:other/release")


def test_the_cli_gives_an_existing_workspace_its_first_owner(engine, capsys):
    from types import SimpleNamespace

    args = SimpleNamespace(action="owner", workspace=WS, github="asha", email="", name="Asha")
    assert _workspaces(args, engine=engine) == 0
    assert _workspaces(args, engine=engine) == 1  # it already has one
    assert _workspaces(SimpleNamespace(action="list", workspace="", github="", email="", name=""), engine=engine) == 0
    assert "owners: asha" in capsys.readouterr().out


# --------------------------------------------------------------------------- API


def wait_pending(engine, fid, timeout=10.0):
    end = time.time() + timeout
    while time.time() < end:
        if fid not in engine.running and engine.view(fid)["pending"]:
            return engine.view(fid)
        time.sleep(0.02)
    raise TimeoutError(fid)


@pytest.fixture
def client(engine):
    with TestClient(create_app(engine, auth=AuthConfig(mode="dev", secret="x" * 40), web_dist=None)) as c:
        yield c


def login(client, name, email=""):
    assert client.post("/api/auth/dev-login", json={"login": name, "email": email, "orgs": ["someone-else"]}).status_code == 200


def test_an_admin_invites_people_who_then_approve_through_their_group(client, engine):
    engine.services.workspaces.bootstrap_owner(WS, {"name": "Asha", "github_login": "asha"})
    login(client, "asha")
    assert client.get("/api/workspaces").json()[0]["role"] == "owner"
    assert [lane["id"] for lane in client.get("/api/me").json()["lanes"]] == ["FARM", "SHOP"]

    inv = client.post(f"/api/workspaces/{WS}/members", json={"name": "Ben", "email": "ben@farm.example",
                                                              "role": "approver", "products": ["FARM"]}).json()
    token = inv["invite_url"].rsplit("/", 1)[-1]
    rel = client.put(f"/api/workspaces/{WS}/groups/product", json={"members": [
        m["id"] for m in client.get(f"/api/workspaces/{WS}").json()["members"]]})
    assert rel.status_code == 200
    assert client.put(f"/api/workspaces/{WS}/products/FARM/gates", json={"gates": {"g1": ["product"]}}).json()["version"] == 1
    assert client.put(f"/api/workspaces/{WS}/products/CLN/gates", json={"gates": {"g1": ["product"]}}).status_code == 404

    # Ben isn't in acme-farm's GitHub teams; his group makes him a G1 approver.
    login(client, "ben", "ben@farm.example")
    assert client.get("/api/me").json()["lanes"] == []  # not a member until he accepts
    joined = client.post(f"/api/workspaces/{WS}/join/{token}", json={})
    assert joined.json()["status"] == "active", joined.text
    assert [lane["id"] for lane in client.get("/api/me").json()["lanes"]] == ["FARM"]  # his profile lists FARM only
    r = client.post("/api/features", json={"product_id": "FARM", "title": "Offline crop log"})
    assert r.status_code == 201
    fid = r.json()["feature_id"]
    wait_pending(engine, fid)
    [item] = client.get("/api/inbox?mine=true").json()
    assert item["gate"] == "g1" and f"group:{WS}/product" in item["approvers"]
    assert client.post(f"/api/features/{fid}/decisions/{item['interrupt_id']}", json={"decision": "approve"}).status_code == 202

    # An approver isn't an admin.
    assert client.post(f"/api/workspaces/{WS}/groups", json={"name": "QA"}).status_code == 403
    assert client.get(f"/api/workspaces/{WS}/audit").status_code == 403

    # Suspended: no products, no groups, on the very next request.
    login(client, "asha")
    ben = next(m for m in client.get(f"/api/workspaces/{WS}").json()["members"] if m["email"] == "ben@farm.example")
    assert client.patch(f"/api/workspaces/{WS}/members/{ben['id']}", json={"status": "suspended"}).status_code == 200
    login(client, "ben", "ben@farm.example")
    assert client.get("/api/me").json()["lanes"] == [] and client.get("/api/me").json()["groups"] == []
    login(client, "asha")
    actions = [a["action"] for a in client.get(f"/api/workspaces/{WS}/audit").json()]
    assert {"member.add", "member.accept", "group.members", "gates.set", "member.update"} <= set(actions)


def test_viewers_see_but_dont_start_work(client, engine):
    w = engine.services.workspaces
    w.bootstrap_owner(WS, {"github_login": "asha"})
    _, token = w.invite(WS, {"github_login": "vic"}, role="viewer", actor="asha")
    w.accept(WS, token, login="vic")
    login(client, "vic")
    assert "FARM" in [lane["id"] for lane in client.get("/api/me").json()["lanes"]]
    r = client.post("/api/features", json={"product_id": "FARM", "title": "Harvest forecast view"})
    assert r.status_code == 403 and "Viewers" in r.json()["detail"]


def test_a_new_workspace_makes_its_creator_owner(client):
    login(client, "founder", "f@new.example")
    r = client.post("/api/workspaces", json={"id": "acme-new", "name": "Acme New", "plan": "lite"})
    assert r.status_code == 201
    v = client.get("/api/workspaces/acme-new").json()
    assert v["role"] == "owner" and v["members"][0]["email"] == "f@new.example" and v["products"] == []
    assert client.post("/api/workspaces", json={"id": "acme-new", "name": "again"}).status_code == 400
    assert client.get("/api/workspaces/someone-elses").status_code == 404


@pytest.mark.skipif(not __import__("os").environ.get("VALXEL_TEST_DATABASE_URL"),
                    reason="set VALXEL_TEST_DATABASE_URL to run Postgres tests")
def test_workspaces_persist_in_postgres(settings):
    import os
    import uuid

    from psycopg.rows import dict_row
    from psycopg_pool import ConnectionPool

    from valxel_sdlc.workspaces import PostgresWorkspaceStore

    name = f"wrkspc-{uuid.uuid4().hex[:8]}"
    with ConnectionPool(os.environ["VALXEL_TEST_DATABASE_URL"],
                        kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row}) as pool:
        w = Workspaces(PostgresWorkspaceStore(pool), settings.registry)
        w.bootstrap_owner(name, {"github_login": "asha"})
        _, token = w.invite(name, {"email": "b@x.example"}, role="approver", actor="asha")
        w.accept(name, token, login=f"b-{name}", email="b@x.example")
        again = Workspaces(PostgresWorkspaceStore(pool), settings.registry)  # a restarted replica
        assert len(again.groups(name)) == 6 and len(again.members(name)) == 2
        assert [m["role"] for m in again.memberships(f"b-{name}")] == ["approver"]
        assert again.store.audit_log(name)[0]["action"] == "member.accept"



def test_invitations_are_emailed_when_email_is_set_up(engine):
    from valxel_sdlc.notify import Notifier

    sent = []
    engine.services.notifier = Notifier(engine.services.settings.artifact_dir,
                                        send_email=lambda to, subject, text: sent.append((to, subject, text)))
    engine.services.workspaces.bootstrap_owner(WS, {"name": "Asha", "github_login": "asha"})
    with TestClient(create_app(engine, auth=AuthConfig(mode="dev", secret="x" * 40), web_dist=None)) as client:
        login(client, "asha")
        r = client.post(f"/api/workspaces/{WS}/members", json={"name": "Ben", "email": "ben@farm.example",
                                                                "role": "approver"}).json()
        assert r["emailed"] is True
        [(to, subject, text)] = sent
        assert to == "ben@farm.example" and subject == f"Join {WS} in Valxel"
        assert "asha invited you" in text and "as approver" in text and r["invite_url"] in text
        again = client.post(f"/api/workspaces/{WS}/members/{r['id']}/resend", json={}).json()
        assert again["emailed"] is True and again["invite_url"] != r["invite_url"] and len(sent) == 2
        # A profile with only a GitHub login gets no email: the admin shares the link.
        r = client.post(f"/api/workspaces/{WS}/members", json={"github_login": "vic", "role": "viewer"}).json()
        assert r["emailed"] is False and len(sent) == 2
        assert engine.services.notifier.outbox[-1]["kind"] == "invite"
        assert r["invite_url"] not in str(engine.services.notifier.outbox)  # the link is never kept


def test_without_email_set_up_the_admin_shares_the_link(client, engine, monkeypatch):
    monkeypatch.delenv("VALXEL_SMTP_HOST", raising=False)
    engine.services.workspaces.bootstrap_owner(WS, {"github_login": "asha"})
    login(client, "asha")
    r = client.post(f"/api/workspaces/{WS}/members", json={"email": "ben@farm.example", "role": "member"}).json()
    assert r["emailed"] is False and "/join/" in r["invite_url"]

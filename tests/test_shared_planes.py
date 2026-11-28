"""Products sharing their workspace's clusters (the default, and cheapest), or with accounts and
clusters of their own (cloud/onboarding.py): the workspace's two shared accounts, the front door's
choice, the product gate's placement (PHI and payments never share), and the shared planes."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from valxel_sdlc import dataplanes, environments
from valxel_sdlc.api.app import create_app
from valxel_sdlc.cloud import onboarding
from valxel_sdlc.inception import Vision
from valxel_sdlc.inception.setup import _runtime_cloud, resolve_cloud
from valxel_sdlc.intake import Draft, IntakeAgent, IntakeSession, set_cloud
from valxel_sdlc.web.auth import AuthConfig

WS = "wrkspc-shared"
SHARED = {"non_production": "acme-shared-np", "production": "acme-shared-prod", "region": "us-central1"}


def brief(data_class: str = "general", pid: str = "VET") -> str:
    return (f"# Brief\n\n## Product\n- ID: {pid}\n- Org: acme-vet\n- Repo prefix: vet-\n\n"
            f"## Data class\n- {data_class}: what it holds\n")


class Wsx:
    def __init__(self, ws=None):
        self.ws = ws or {}

    def get(self, workspace):
        return self.ws.get(workspace)


def test_shared_plane_names_fit_every_cloud():
    assert onboarding.shared_plane("wrkspc-shared", "non_production") == "wrkspc-shared-nonprod"
    long = onboarding.shared_plane("wrkspc-" + "x" * 40, "production")
    assert len(long) <= 31 and long.endswith("-prod") and long[0].isalpha()
    assert onboarding.shared_plane("9ws", "production").startswith("w")


def test_only_phi_and_payments_need_their_own():
    assert onboarding.needs_dedicated("PHI") and onboarding.needs_dedicated("payments")
    assert not onboarding.needs_dedicated("general") and not onboarding.needs_dedicated("")


def test_a_workspace_without_both_accounts_and_a_region_has_none():
    assert onboarding.shared_accounts({"cloud_accounts": SHARED})["accounts"]["production"] == "acme-shared-prod"
    assert onboarding.shared_accounts({"cloud_accounts": {**SHARED, "region": ""}}) == {}
    assert onboarding.shared_accounts({}) == {}


# ------------------------------------------------------------------------------ the workspace's accounts

@pytest.fixture
def client(engine, monkeypatch):
    monkeypatch.setenv("VALXEL_CLOUD", "gcp")
    engine.services.workspaces.bootstrap_owner(WS, {"name": "Asha", "github_login": "asha", "email": "a@x.example"})
    with TestClient(create_app(engine, auth=AuthConfig(mode="dev", secret="x" * 40), web_dist=None)) as c:
        c.post("/api/auth/dev-login", json={"login": "asha"})
        yield c


def test_admins_set_the_workspaces_shared_accounts(client, engine):
    empty = client.get(f"/api/workspaces/{WS}/cloud-accounts").json()
    assert empty["noun"] == "project" and "accounts" not in empty
    assert empty["planes"] == {"non_production": "wrkspc-shared-nonprod", "production": "wrkspc-shared-prod"}
    given = "non-production: acme-shared-np, production: acme-shared-prod, region: us-central1"
    r = client.put(f"/api/workspaces/{WS}/cloud-accounts", json={"accounts": given})
    assert r.status_code == 200, r.text
    assert r.json()["accounts"] == {"non_production": "acme-shared-np", "production": "acme-shared-prod"}
    bad = client.put(f"/api/workspaces/{WS}/cloud-accounts", json={"accounts": "production: acme-shared-prod"})
    assert bad.status_code == 400
    assert "accounts" not in client.put(f"/api/workspaces/{WS}/cloud-accounts", json={"accounts": ""}).json()
    assert client.get("/api/workspaces/wrkspc-other/cloud-accounts").status_code == 403


# ------------------------------------------------------------------------------ the front door

def _agent(ws):
    return IntakeAgent(SimpleNamespace(settings=SimpleNamespace(mode="stub"), registry=None, workspaces=Wsx(ws)))


def _session():
    s = IntakeSession(id="s", owner="o", lanes=[], starter=True, workspace=WS)
    s.draft = Draft(size="product", name="VET", org="acme-vet")
    return s


def test_a_workspace_with_shared_accounts_offers_them_first(monkeypatch):
    monkeypatch.setenv("VALXEL_CLOUD", "gcp")
    agent, s = _agent({WS: {"cloud_accounts": SHARED}}), _session()
    q, options = agent._sized_question(s, "cloud")
    assert "share wrkspc-shared's two clusters" in q and "PHI or payments" in q
    assert options == ["Share the workspace's clusters", "Its own accounts", "Set them up later"]
    assert agent._answer_cloud(s, "Share the workspace's clusters") == ""
    assert (s.draft.cloud, s.draft.placement) == ("shared", "shared")


def test_its_own_accounts_go_on_to_the_ids(monkeypatch):
    monkeypatch.setenv("VALXEL_CLOUD", "gcp")
    agent, s = _agent({WS: {"cloud_accounts": SHARED}}), _session()
    asked = agent._answer_cloud(s, "Its own accounts")
    assert asked.startswith("VET will run on Google Cloud") and s.draft.placement == "dedicated"
    assert agent._sized_question(s, "cloud")[1] == ["I have them", "Help me create them", "Set them up later"]
    assert agent._answer_cloud(s, "non-production: vet-np, production: vet-prod, region: us-east1") == ""
    assert (s.draft.cloud, s.draft.placement) == ("given", "dedicated")


def test_without_shared_accounts_it_asks_for_its_own(monkeypatch):
    monkeypatch.setenv("VALXEL_CLOUD", "gcp")
    agent, s = _agent({}), _session()
    assert agent._sized_question(s, "cloud")[1] == ["I have them", "Help me create them", "Set them up later"]


def test_the_draft_editor_chooses_shared_or_dedicated():
    d = Draft(size="product")
    set_cloud(d, "shared")
    assert (d.cloud, d.placement) == ("shared", "shared")
    set_cloud(d, "dedicated")
    assert (d.cloud, d.placement) == ("", "dedicated")


# ------------------------------------------------------------------------------ the product gate

def test_a_sharing_product_gets_the_workspaces_accounts_and_clusters():
    v = Vision(name="VET", org="acme-vet", vision="x", workspace=WS, cloud={"provider": "gcp", "placement": "shared"})
    got, note = resolve_cloud(SimpleNamespace(workspaces=Wsx({WS: {"cloud_accounts": SHARED}})), v, brief())
    assert note is None
    rc = _runtime_cloud(got)
    assert rc["cloud"]["placement"] == "shared" and rc["cloud"]["workspace"] == WS
    assert rc["cloud"]["accounts"] == {"dev": "acme-shared-np", "staging": "acme-shared-np",
                                       "production": "acme-shared-prod"}
    assert rc["data_planes"] == {"non_production": "wrkspc-shared-nonprod", "production": "wrkspc-shared-prod"}


def test_phi_never_shares_and_a_workspace_without_shared_accounts_waits():
    v = Vision(name="VET", org="acme-vet", vision="x", workspace=WS, cloud={"provider": "gcp", "placement": "shared"})
    services = SimpleNamespace(workspaces=Wsx({WS: {"cloud_accounts": SHARED}}))
    got, note = resolve_cloud(services, v, brief("phi"))
    assert got.cloud == {"provider": "gcp", "placement": "dedicated", "later": True} and _runtime_cloud(got) == {}
    assert "phi data never shares clusters" in note["why"] and "two projects of its own" in note["why"]
    got, note = resolve_cloud(SimpleNamespace(workspaces=Wsx({})), v, brief())
    assert got.cloud["later"] and "has no shared projects yet" in note["why"]
    mine = Vision(name="VET", org="acme-vet", vision="x", cloud={"provider": "gcp", "placement": "dedicated"})
    assert resolve_cloud(services, mine, brief()) == (mine, None)


def test_shared_planes_are_the_workspaces_and_carry_its_name():
    cloud = {**onboarding.runtime_cloud("gcp", {"non_production": "acme-shared-np", "production": "acme-shared-prod"},
                                        "us-central1", "shared"), "workspace": WS}
    s = {"domain": "planes.example.com", "control_plane_domain": "cp.example.com", "acme_email": "o@x.example",
         "admin_cidrs": {}, "gitops_branch": "main", "db_logins_image": "img"}
    v = dataplanes.plane_vars("VET", "non_production", cloud, s)
    assert v["name"] == "wrkspc-shared-nonprod" and v["product"] == "wrkspc-shared"
    assert v["project"] == "acme-shared-np" and v["domain"] == "wrkspc-shared-nonprod.planes.example.com"
    assert dataplanes.plane_name("VET", "production", cloud) == "wrkspc-shared-prod"
    assert dataplanes.plane_name("VET", "production", {"placement": "dedicated"}) == "vet-prod"


def test_environments_bind_to_the_shared_planes():
    made = []
    oc = SimpleNamespace(data_plane=lambda name, namespace=None: {"state": "ready"},
                         ensure_project=lambda **kw: "created", ensure_pipeline=lambda **kw: "created",
                         ensure_environment=lambda **kw: made.append((kw["name"], kw["data_plane"])) or "created")
    planes = {"non_production": "wrkspc-shared-nonprod", "production": "wrkspc-shared-prod"}
    out = environments.environments(SimpleNamespace(openchoreo=oc), product_id="VET", approver="maria", planes=planes)
    assert out["approved_by"] == "maria"
    assert made == [("dev", "wrkspc-shared-nonprod"), ("staging", "wrkspc-shared-nonprod"),
                    ("production", "wrkspc-shared-prod")]


def test_on_aws_the_workspaces_shared_clusters_are_proposed_as_its_own():
    planes = {"non_production": "wrkspc-shared-nonprod", "production": "wrkspc-shared-prod"}
    files = environments.cluster_files("VET", 2, environments.DEFAULT_TEMPLATE, 2, owner=WS, planes=planes)
    assert sorted(files) == ["clusters/wrkspc-shared/wrkspc-shared-nonprod.yaml",
                             "clusters/wrkspc-shared/wrkspc-shared-observe.yaml",
                             "clusters/wrkspc-shared/wrkspc-shared-prod.yaml"]
    assert all("vet" not in text.lower() for text in files.values())

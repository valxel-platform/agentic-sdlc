"""Phase 2 on Google Cloud (ADR 0010, CP-201 to CP-207): connecting a customer's project, its
costs from the billing export, its inventory from Cloud Asset Inventory, and its alerts, budgets
and findings as signals. Payloads and query rows are tests/fixtures/gcp's."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

from valxel_sdlc.api.app import create_app
from valxel_sdlc.cloud import conformance, id_from_label, label_key, label_value, tags_for
from valxel_sdlc.cloud.gcp.accounts import GcpConnect, connect_label, description
from valxel_sdlc.cloud.gcp.costs import BillingExportMissing, GcpBillingCosts
from valxel_sdlc.cloud.gcp.inventory import GcpAssets
from valxel_sdlc.connections import AwsConnect, ConnectionError_, Connections, gcp_health
from valxel_sdlc.ops import signals as sig
from valxel_sdlc.ops.signal_tokens import derive, token_for
from valxel_sdlc.web.auth import AuthConfig

FIX = Path(__file__).parent / "fixtures" / "gcp"
WS, PROJECT, VALXEL_SA = "wrkspc-shared", "farm-prod", "valxel@valxel-plane.iam.gserviceaccount.com"
ROOT = Path(__file__).resolve().parents[1]


def fixture(name):
    return json.loads((FIX / name).read_text())


# --------------------------------------------------------------------- labels
def test_tags_become_labels_google_accepts_and_read_back():
    assert label_key("valxel:feature") == "valxel_feature" and label_value("FARM-FEAT-7") == "farm-feat-7"
    assert tags_for("gcp", {"valxel:product": "FARM", "valxel:workspace": "Acme Co"}) == {
        "valxel_product": "farm", "valxel_workspace": "acme_co"}
    assert tags_for("aws", {"valxel:product": "FARM"}) == {"valxel:product": "FARM"}
    assert id_from_label(label_value("CLN-FEAT-12")) == "CLN-FEAT-12"


# --------------------------------------------------------------------- connecting a project
class FakeIam:
    """Impersonation and service-account reads as Google would answer them: only the accounts the
    template made, which trust Valxel's account, with this workspace's description."""

    def __init__(self):
        self.accounts: dict[str, dict] = {}  # email -> {"description", "trusts"}
        self.calls = []

    def apply_template(self, conns, workspace, project, trusts=VALXEL_SA):
        for email in GcpConnect.roles(workspace, project).values():
            self.accounts[email] = {"description": description(workspace, conns.external_id(workspace)),
                                    "trusts": trusts}

    def impersonate(self, source, target):
        self.calls.append((source, target))
        acct = self.accounts.get(target)
        if acct is None:
            raise RuntimeError(f"404 NOT_FOUND: Service account {target} does not exist")
        if acct["trusts"] != source:
            raise RuntimeError("403 PERMISSION_DENIED: Permission 'iam.serviceAccounts.getAccessToken' denied")
        return f"token-for-{target}"

    def read(self, email, token):
        assert token == f"token-for-{email}"  # read with its own token
        return {"email": email, "description": self.accounts[email]["description"]}


@pytest.fixture
def iam():
    return FakeIam()


@pytest.fixture
def gcp_conns(engine, iam, monkeypatch):
    monkeypatch.setenv("VALXEL_CLOUD", "gcp")
    c = Connections(engine.services.workspaces,
                    gcp=GcpConnect(valxel_service_account=VALXEL_SA, impersonate=iam.impersonate, read=iam.read))
    engine.services.connections = c
    engine.services.workspaces.bootstrap_owner(WS, {"name": "Asha", "github_login": "asha",
                                                     "email": "asha@farm.example"})
    return c


@pytest.fixture
def client(engine, gcp_conns):
    with TestClient(create_app(engine, auth=AuthConfig(mode="dev", secret="x" * 40), web_dist=None)) as c:
        c.post("/api/auth/dev-login", json={"login": "asha"})
        yield c


def test_the_launch_opens_cloud_shell_and_gives_this_workspaces_command(client, gcp_conns):
    r = client.get(f"/api/workspaces/{WS}/connections/gcp/launch", params={"mode": "full", "region": "us-east1"})
    assert r.status_code == 200, r.text
    url = urlparse(r.json()["url"])
    q = parse_qs(url.query)
    assert url.netloc == "shell.cloud.google.com" and q["cloudshell_workspace"] == ["deploy/gcp-connect"]
    cmd = r.json()["command"]
    assert f"-var workspace={WS}" in cmd and "-var mode=full" in cmd
    assert f"-var valxel_service_account={VALXEL_SA}" in cmd
    label = re.search(r"-var connect_label=(\w+)", cmd).group(1)
    assert label == connect_label(gcp_conns.external_id(WS)) and gcp_conns.external_id(WS) not in cmd
    # The command's variables are the template's.
    declared = set(re.findall(r'variable "(\w+)"', (ROOT / "deploy/gcp-connect/variables.tf").read_text()))
    assert set(re.findall(r"-var (\w+)=", cmd)) <= declared


def test_checking_acts_as_both_accounts_and_records_the_project(client, gcp_conns, iam):
    iam.apply_template(gcp_conns, WS, PROJECT)
    r = client.post(f"/api/workspaces/{WS}/connections/gcp/check", json={"project": " Farm-Prod ", "mode": "lite"})
    assert r.status_code == 200, r.text
    assert r.json()["reach"] == {"account_id": PROJECT, "mode": "lite",
                                 "deploy_role": f"valxel-deploy@{PROJECT}.iam.gserviceaccount.com",
                                 "agents_role": f"valxel-agents@{PROJECT}.iam.gserviceaccount.com"}
    assert {s for s, _ in iam.calls} == {VALXEL_SA}
    page = {t["tool"]: t for t in client.get(f"/api/workspaces/{WS}/connections").json()}
    assert page["gcp"]["status"] == "connected" and "aws" not in page  # one cloud per installation


@pytest.mark.parametrize("setup, why", [
    (None, "apply the connect template there first"),
    ("other-workspace", "set up for another workspace"),
    ("other-valxel", "hasn't been applied there"),
])
def test_a_check_that_fails_says_why(client, gcp_conns, iam, setup, why):
    if setup == "other-workspace":
        iam.apply_template(gcp_conns, "someone-else", PROJECT)
    elif setup == "other-valxel":
        iam.apply_template(gcp_conns, WS, PROJECT, trusts="another@elsewhere.iam.gserviceaccount.com")
    r = client.post(f"/api/workspaces/{WS}/connections/gcp/check", json={"project": PROJECT})
    assert r.status_code == 422 and why in r.json()["detail"]


def test_gcp_connect_conforms_to_the_cloud_accounts_port(gcp_conns, iam):
    iam.apply_template(gcp_conns, WS, PROJECT)
    assert conformance.cloud_accounts(gcp_conns.gcp, workspace=WS, account=PROJECT,
                                      external_id=gcp_conns.external_id(WS)) == []
    with pytest.raises(ConnectionError_, match="project ID"):
        gcp_conns.gcp.check(WS, account="X", external_id="x")


def test_aws_connect_conforms_too():
    calls = []

    def assume(arn, *, external_id, session):
        calls.append(arn)
        if external_id != "ext":
            raise RuntimeError("AccessDenied")
        return {"Account": arn.split(":")[4]}

    aws = AwsConnect(valxel_account="9" * 12, template_url="https://x/t.yaml", assume=assume)
    assert conformance.cloud_accounts(aws, workspace=WS, account="123456789012", external_id="ext") == []


def test_a_removed_template_shows_in_the_health_check(gcp_conns, iam):
    iam.apply_template(gcp_conns, WS, PROJECT)
    gcp_conns.gcp_check(WS, project=PROJECT, mode="lite", actor="asha")
    gcp_conns.health_checks["gcp"] = gcp_health(gcp_conns)
    assert gcp_conns.check(WS) == []
    iam.accounts.clear()
    [c] = gcp_conns.check(WS)
    assert c["status"] == "needs_attention" and "connect template" in c["last_error"]


# --------------------------------------------------------------------- costs
class FakeBigQuery:
    def __init__(self, rows=None):
        self.rows = rows if rows is not None else fixture("billing-rows.json")
        self.queries = []

    def __call__(self, sql, params):
        self.queries.append((sql, params))
        if "INFORMATION_SCHEMA" in sql:
            return self.rows["tables"]
        return self.rows["daily"] if "GROUP BY day" in sql else self.rows["feature"]


def test_costs_come_from_the_billing_export_by_label():
    bq = FakeBigQuery()
    src = GcpBillingCosts("acme-billing.valxel_billing", run=bq)
    start, end = datetime(2026, 11, 20, tzinfo=UTC), datetime(2026, 11, 21, tzinfo=UTC)
    assert src.cloud_costs(product_id="FARM", feature_id="FARM-FEAT-7", account=PROJECT, start=start, end=end) == {
        "Cloud Pub/Sub": 12.5, "Cloud Firestore": 3.25}
    sql, params = bq.queries[-1]
    assert "acme-billing.valxel_billing.gcp_billing_export_v1_01AB23_45CD67_89EF01" in sql
    assert params == {"project": PROJECT, "start": start, "end": end, "feature": "farm-feat-7"}
    assert "credits" in sql and "'valxel_feature'" in sql
    daily = src.cloud_costs_daily(product_id="FARM", account=PROJECT, start=start, end=end)
    assert daily == [{"day": "2026-11-20", "service": "Cloud Pub/Sub", "feature_id": "FARM-FEAT-7", "usd": 6.0},
                     {"day": "2026-11-20", "service": "Compute Engine", "feature_id": "", "usd": 40.0}]
    assert bq.queries[-1][1]["product"] == "farm"


def test_an_export_that_isnt_on_says_what_to_do():
    src = GcpBillingCosts("acme-billing.valxel_billing", run=FakeBigQuery({"tables": [], "daily": [], "feature": []}))
    with pytest.raises(BillingExportMissing, match="turns it on in the console"):
        src.cloud_costs_daily(product_id="FARM", account=PROJECT, start=datetime.now(UTC), end=datetime.now(UTC))
    with pytest.raises(ValueError, match=r"project\.dataset"):
        GcpBillingCosts("just-a-dataset")


def test_the_ledger_files_gcp_spend_under_gcp():
    from valxel_sdlc.finops.collect import collect_cloud, rates

    src = GcpBillingCosts("acme-billing.valxel_billing", run=FakeBigQuery())
    recs = collect_cloud(src, "FARM", {"production": PROJECT}, {"FARM-FEAT-7": None},
                         datetime(2026, 11, 23, tzinfo=UTC), rates(), provider="gcp")
    assert {(r.category, r.source, r.feature_id, r.stage) for r in recs} == {
        ("gcp", "billing-export", "FARM-FEAT-7", "build"), ("gcp", "billing-export", "", "run")}


# --------------------------------------------------------------------- inventory
def test_inventory_lists_what_carries_the_products_labels():
    seen = []
    assets = GcpAssets(lambda project, query: seen.append((project, query)) or fixture("assets.json"))
    tagged = assets.tagged(product_id="FARM", account=PROJECT)
    assert seen[0] == (PROJECT, "labels.valxel_product=farm")
    assert tagged[0] == {"arn": "//pubsub.googleapis.com/projects/farm-prod/topics/field-events",
                         "resource": "field-events", "feature": "FARM-FEAT-7"}
    rows = assets.resources(account=PROJECT, product_id="FARM")
    assert [r["type"] for r in rows][:2] == ["pubsub.googleapis.com/Topic", "storage.googleapis.com/Bucket"]
    assert rows[0]["feature_id"] == "FARM-FEAT-7"


# --------------------------------------------------------------------- signals
def test_cloud_monitoring_incidents_fire_and_resolve():
    [opened] = sig.from_cloud_monitoring(fixture("monitoring-incident-open.json"))
    [closed] = sig.from_cloud_monitoring(fixture("monitoring-incident-closed.json"))
    assert (opened["status"], closed["status"]) == ("firing", "resolved")
    assert opened["fingerprint"] == closed["fingerprint"]
    assert (opened["product_id"], opened["component"], opened["severity"], opened["labels"]["project"]) == (
        "FARM", "farm-field-worker", "sev2", PROJECT)
    assert opened["title"] == "field-events: queue backlog"
    other = fixture("monitoring-incident-open.json")
    other["incident"]["documentation"]["content"] = "someone else's policy"
    assert sig.from_cloud_monitoring(other) == [] and sig.from_cloud_monitoring({"incident": {"state": "x"}}) == []


def test_budget_notifications_only_when_a_threshold_is_crossed():
    [s] = sig.from_gcp_budget(fixture("budget-notification.json"), ["FARM", "CLN"])
    assert (s["product_id"], s["env"], s["severity"], s["category"]) == ("FARM", "production", "sev2", "cost")
    assert "1,180.50 USD" in s["detail"] and s["labels"]["billing_account"] == "01AB23-45CD67-89EF01"
    import base64

    quiet = fixture("budget-notification.json")
    data = json.loads(base64.b64decode(quiet["message"]["data"]))
    data.pop("alertThresholdExceeded")
    quiet["message"]["data"] = base64.b64encode(json.dumps(data).encode()).decode()
    assert sig.from_gcp_budget(quiet, ["FARM"]) == []  # the routine update, no threshold crossed


def test_scc_vulnerabilities_open_incidents_for_the_component():
    [s] = sig.from_scc(fixture("scc-finding.json"))
    assert (s["component"], s["severity"], s["status"], s["labels"]["project"]) == (
        "farm-field-worker", "sev2", "firing", PROJECT)
    assert "CVE-2026-1234" in s["title"] and "3.5.4-1" in s["detail"]


def test_project_tokens():
    t = derive("s" * 40, "project", PROJECT)
    assert t.startswith("vxs_") and t != derive("s" * 40, "account", "123456789012")
    with pytest.raises(ValueError, match="project ID"):
        derive("s" * 40, "project", "Not_A_Project")


@pytest.fixture
def signals_client(engine, monkeypatch):
    monkeypatch.setenv("VALXEL_SIGNALS_TOKEN", "s" * 40)
    return TestClient(create_app(engine, auth=AuthConfig(mode="dev", secret="x" * 40), web_dist=None))


def test_a_projects_token_sends_that_projects_events_only(signals_client):
    good = token_for("project", PROJECT, "s" * 40)
    incident = fixture("monitoring-incident-open.json")
    r = signals_client.post(f"/api/signals/cloud-monitoring?token={good}", json=incident)
    assert r.status_code == 202, r.text
    other = token_for("project", "farm-staging", "s" * 40)
    assert signals_client.post(f"/api/signals/cloud-monitoring?token={other}",
                               json=fixture("monitoring-incident-open.json")).status_code == 401
    no_token = signals_client.post("/api/signals/cloud-monitoring", json=fixture("monitoring-incident-open.json"))
    assert no_token.status_code == 401
    assert signals_client.post(f"/api/signals/scc?token={good}", json=fixture("scc-finding.json")).status_code == 202


def test_budgets_take_the_platform_token(signals_client):
    assert signals_client.post(f"/api/signals/gcp-budgets?token={'s' * 40}",
                               json=fixture("budget-notification.json")).status_code == 202
    project = token_for("project", PROJECT, "s" * 40)
    assert signals_client.post(f"/api/signals/gcp-budgets?token={project}",
                               json=fixture("budget-notification.json")).status_code == 401

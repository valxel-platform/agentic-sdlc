"""Azure (ADR 0010, phase 4: CP-401 to CP-406): Key Vault secrets and signing, Blob Storage's locked
immutability, Entra ID sign-in, connecting a customer's subscription through federated
credentials, its costs (Cost Management), its inventory (Resource Graph), and its alerts, budgets
and Defender findings as signals. Fakes of the SDK and REST calls, and the same conformance checks
every adapter passes; payloads are tests/fixtures/azure's."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from valxel_sdlc.api.app import create_app
from valxel_sdlc.cloud import conformance, immutable_store, strict_problems, tags_for
from valxel_sdlc.cloud.azure.accounts import AzureConnect, connect_label, identity_path
from valxel_sdlc.cloud.azure.costs import AzureCosts
from valxel_sdlc.cloud.azure.inventory import AzureResources
from valxel_sdlc.cloud.azure.secrets import AzureSecretStore, AzureVault, kv_name
from valxel_sdlc.cloud.azure.signing import AzureKeyVaultSigner
from valxel_sdlc.cloud.azure.storage import AzureBlobImmutable
from valxel_sdlc.connections import ConnectionError_, Connections, azure_health
from valxel_sdlc.ops import signals as sig
from valxel_sdlc.ops.signal_tokens import derive, token_for
from valxel_sdlc.web.auth import AuthConfig

FIX = Path(__file__).parent / "fixtures" / "azure"
ROOT = Path(__file__).resolve().parents[1]
WS = "wrkspc-shared"
SUB = "1b2c3d4e-0000-4000-8000-00000000aa01"
TENANT = "9a8b7c6d-0000-4000-8000-0000000000ff"
DEPLOY, AGENTS = "2b2c3d4e-0000-4000-8000-00000000bb02", "3b2c3d4e-0000-4000-8000-00000000cc03"
ISSUER, SUBJECT = "https://eastus.oic.prod-aks.azure.com/t/c/", "system:serviceaccount:valxel:valxel"


def fixture(name):
    return json.loads((FIX / name).read_text())


class NotFound(Exception):
    status_code = 404


class Exists(Exception):
    status_code = 409


# --------------------------------------------------------------------- Key Vault secrets (CP-401)
class FakeKeyVault:
    """name -> [(created, value, enabled, tags)]"""

    def __init__(self):
        self.secrets: dict[str, list] = {}
        self.deleted: set[str] = set()
        self.t = 0

    def set(self, name, value, tags=None):
        if name in self.deleted:
            raise RuntimeError("(Conflict) ObjectIsDeletedButRecoverable")
        self.t += 1
        self.secrets.setdefault(name, []).append([self.t, value, True, tags or {}])

    def list_properties_of_secret_versions(self, name):
        if name not in self.secrets:
            raise NotFound(name)
        return [SimpleNamespace(version=str(i), created_on=t, enabled=on)
                for i, (t, _, on, _tags) in enumerate(self.secrets[name])]

    def get_secret(self, name, version=None):
        if name not in self.secrets:
            raise NotFound(name)
        rows = self.secrets[name]
        return SimpleNamespace(value=(rows[-1] if version is None else rows[int(version)])[1])

    def set_secret(self, name, value, tags=None, content_type=None):
        self.set(name, value, tags)

    def begin_delete_secret(self, name):
        if name not in self.secrets:
            raise NotFound(name)
        del self.secrets[name]
        self.deleted.add(name)
        return SimpleNamespace(wait=lambda: None)

    def begin_recover_deleted_secret(self, name):
        self.deleted.discard(name)
        self.secrets.setdefault(name, [])
        return SimpleNamespace(wait=lambda: None)


def test_key_vault_store_conforms_and_reads_current_then_previous():
    kv = FakeKeyVault()
    store = AzureSecretStore(vault_url="https://acme.vault.azure.net", client=kv, ttl=0)
    assert conformance.secret_store(store, set_value=lambda n, v: kv.set(kv_name("valxel/platform/" + n), v)) == []
    name = kv_name("valxel/platform/VALXEL_CONFORMANCE_SECRET")
    kv.set(name, "third")
    kv.secrets[name][1][2] = False  # the rotation is finished: the old version is disabled
    assert store.versions("VALXEL_CONFORMANCE_SECRET") == ["third", "first"]


def test_key_vault_names_are_valid():
    assert kv_name("valxel/platform/VALXEL_SESSION_SECRET") == "valxel--platform--VALXEL-SESSION-SECRET"
    assert re.fullmatch(r"[0-9A-Za-z-]{1,127}", kv_name("valxel/workspaces/acme/jira/ana@x.com"))
    assert kv_name("valxel/workspaces/acme/jira/a.b") != kv_name("valxel/workspaces/acme/jira/a-b")


def test_the_vault_tags_reads_deletes_and_recovers_a_soft_deleted_token():
    kv = FakeKeyVault()
    vault = AzureVault(vault_url="https://acme.vault.azure.net", client=kv)
    path = "valxel/workspaces/acme/jira/ana"
    assert vault.get(path) is None
    vault.put(path, {"access_token": "a"})
    vault.put(path, {"access_token": "b"})
    assert vault.get(path) == {"access_token": "b"}
    assert kv.secrets[kv_name(path)][0][3] == {"valxel-kind": "connection-token"}
    vault.delete(path)
    vault.delete(path)  # already gone: fine
    assert vault.get(path) is None
    vault.put(path, {"access_token": "c"})  # soft-deleted under purge protection: recovered, then written
    assert vault.get(path) == {"access_token": "c"}


def test_the_installation_reads_secrets_and_tokens_from_key_vault(monkeypatch):
    from valxel_sdlc import oauth_connections, secret_ring
    from valxel_sdlc.cloud.azure import secrets as az

    monkeypatch.setattr(az, "_client", lambda url, client: ("client-for", url))
    env = {"VALXEL_SECRET_STORE": "azure", "VALXEL_AZURE_KEY_VAULT": "https://acme.vault.azure.net/"}
    store = secret_ring.store_for(env)
    assert isinstance(store, AzureSecretStore) and store.client == ("client-for", "https://acme.vault.azure.net")
    assert isinstance(oauth_connections.vault_for(env), AzureVault)
    with pytest.raises(RuntimeError, match="VALXEL_AZURE_KEY_VAULT"):
        secret_ring.store_for({"VALXEL_SECRET_STORE": "azure", "VALXEL_SECRET_PREFIX": "other"})


# --------------------------------------------------------------------- Key Vault signing (CP-401)
KEY_ID = "https://acme.vault.azure.net/keys/github-app/0123456789abcdef0123456789abcdef"


def test_key_vault_signs_rs256_over_a_sha256_digest():
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding, rsa
    from cryptography.hazmat.primitives.asymmetric.utils import Prehashed

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    class Crypto:
        def sign(self, algorithm, digest):
            assert str(getattr(algorithm, "value", algorithm)) == "RS256" and len(digest) == 32
            return SimpleNamespace(key_id=KEY_ID, signature=key.sign(digest, padding.PKCS1v15(),
                                                                     Prehashed(hashes.SHA256())))

    pem = key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    assert conformance.signer(AzureKeyVaultSigner(KEY_ID, client=Crypto()), public_pem=pem.decode()) == []
    with pytest.raises(ValueError, match="by its version"):
        AzureKeyVaultSigner("https://acme.vault.azure.net/keys/github-app", client=Crypto())


def test_github_app_signing_uses_key_vault_on_azure(monkeypatch):
    from valxel_sdlc import signing
    from valxel_sdlc.cloud.azure import signing as az_signing

    monkeypatch.setattr(az_signing, "AzureKeyVaultSigner", lambda key: ("key-vault", key))
    env = {"VALXEL_CLOUD": "azure", "VALXEL_GITHUB_APP_ID": "1", "VALXEL_GITHUB_APP_KMS_KEY_ID": KEY_ID}
    assert signing.github_app_signer(env) == ("key-vault", KEY_ID)


def test_strict_mode_knows_azure():
    from valxel_sdlc import strict

    assert strict_problems({"VALXEL_CLOUD": "azure"}) == []
    env = {"VALXEL_CLOUD": "azure", "VALXEL_DELIVERY": "live", "VALXEL_GITHUB_APP_KMS_KEY_ID": "alias/valxel-app"}
    assert "Key Vault key version" in strict.app_key_in_kms(env)[0]
    assert strict.app_key_in_kms({**env, "VALXEL_GITHUB_APP_KMS_KEY_ID": KEY_ID}) == []
    assert "AWS KMS key id" in strict.app_key_in_kms({**env, "VALXEL_CLOUD": "aws",
                                                       "VALXEL_GITHUB_APP_KMS_KEY_ID": KEY_ID})[0]
    got = strict.secrets_in_environment({"VALXEL_CLOUD": "azure", "VALXEL_SECRET_STORE": "gcp"}, live=True)
    assert "runs on azure" in got[0]


# --------------------------------------------------------------------- Blob immutability (CP-402)
class FakeContainer:
    def __init__(self):
        self.blobs: dict[str, dict] = {}

    def upload_blob(self, name, data, overwrite, content_settings, metadata, tags, immutability_policy):
        assert overwrite is False
        if name in self.blobs:
            raise Exists(name)
        self.blobs[name] = {"data": data, "settings": content_settings, "meta": metadata, "tags": tags,
                            "policy": immutability_policy}

    def download_blob(self, name):
        if name not in self.blobs:
            raise NotFound(name)
        return SimpleNamespace(readall=lambda: self.blobs[name]["data"])

    def list_blobs(self, name_starts_with=None):
        return [SimpleNamespace(name=k) for k in self.blobs if k.startswith(name_starts_with or "")]


def test_blob_storage_writes_once_and_locks_each_blob():
    box = FakeContainer()
    store = AzureBlobImmutable("anchors", account_url="https://acme.blob.core.windows.net", client=box,
                               clock=lambda: 1_800_000_000.0)
    assert conformance.immutable_store(store) == []
    store.put("checkpoints/acme/CLN/t/x.jsonl.gz", b"data", retain_days=2190, content_type="application/x-ndjson",
              encoding="gzip", meta={"sha256": "abc", "1st": "x"}, scope={"workspace": "acme", "product": "CLN"})
    blob = box.blobs["checkpoints/acme/CLN/t/x.jsonl.gz"]
    assert blob["policy"].policy_mode == "Locked"
    assert (blob["policy"].expiry_time - datetime.fromtimestamp(1_800_000_000, UTC)).days == 2190
    assert blob["settings"].content_encoding == "gzip"
    assert blob["meta"] == {"sha256": "abc", "m_1st": "x"}  # metadata names are identifiers
    assert blob["tags"] == {"valxel-workspace": "acme", "valxel-product": "CLN"}  # index tags ABAC can match


def test_the_installations_cloud_picks_blob_storage_by_container_url():
    store = immutable_store("https://acme.blob.core.windows.net/anchors/", env={"VALXEL_CLOUD": "azure"},
                            client=FakeContainer())
    assert isinstance(store, AzureBlobImmutable) and store.where == "https://acme.blob.core.windows.net/anchors"
    with pytest.raises(ValueError, match=r"blob\.core\.windows\.net"):
        immutable_store("anchors", env={"VALXEL_CLOUD": "azure"})


# --------------------------------------------------------------------- Entra ID sign-in (CP-403)
def test_the_entra_profile_needs_a_single_tenant():
    cfg = AuthConfig(mode="oidc", secret="x" * 40, oidc_profile="entra", oidc_tenant=TENANT)
    cfg.apply_profile()
    assert cfg.oidc_issuer == f"https://login.microsoftonline.com/{TENANT}/v2.0"
    assert (cfg.oidc_email_verified, cfg.oidc_groups_claim) == ("issuer", "groups")
    for issuer in ("https://login.microsoftonline.com/common/v2.0",
                   "https://login.microsoftonline.com/organizations/v2.0"):
        with pytest.raises(RuntimeError, match="VALXEL_OIDC_TENANT"):
            AuthConfig(mode="oidc", secret="x" * 40, oidc_profile="entra", oidc_issuer=issuer).apply_profile(
                {"VALXEL_OIDC_ISSUER"})


def test_entra_signs_in_by_upn_when_it_sends_no_email():
    from valxel_sdlc.web.identity import OIDCProvider

    idp = OIDCProvider(issuer=f"https://login.microsoftonline.com/{TENANT}/v2.0", client_id="c", client_secret="s",
                       redirect_uri="https://x/api/auth/oidc/callback", email_verified="issuer",
                       groups_claim="groups", opener=object())
    idp._token = lambda form: {"id_token": "t"}  # type: ignore[method-assign]
    idp.verify = lambda token, nonce: {"sub": "u1", "preferred_username": "Asha@Farm.example",  # type: ignore
                                       "name": "Asha", "groups": ["g1"], "amr": ["mfa"]}
    who = idp.identity("code", verifier="v", nonce="n")
    assert (who.email, who.groups, who.strong) == ("asha@farm.example", ("g1",), True)


# --------------------------------------------------------------------- connecting a subscription (CP-404)
class FakeEntra:
    """Token exchange and identity reads as Azure would answer them: only identities the template
    made, whose federated credential trusts this Valxel, tagged for this workspace."""

    def __init__(self):
        self.identities: dict[str, dict] = {}  # client id -> {path, tags, tenant, issuer}
        self.calls = []

    def apply_template(self, conns, workspace, sub=SUB, issuer=ISSUER):
        tags = {"valxel-workspace": workspace, "valxel-connect": connect_label(conns.external_id(workspace))}
        for job, cid in (("deploy", DEPLOY), ("agents", AGENTS)):
            self.identities[cid] = {"path": identity_path(sub, job), "tags": tags, "tenant": TENANT, "issuer": issuer}

    def exchange(self, tenant, client_id):
        self.calls.append((tenant, client_id))
        got = self.identities.get(client_id)
        if got is None or got["tenant"] != tenant:
            raise RuntimeError("AADSTS700016: Application with identifier was not found in the directory")
        if got["issuer"] != ISSUER:
            raise RuntimeError("AADSTS70021: No matching federated identity record found for presented assertion")
        return f"token-for-{client_id}"

    def read(self, path, token):
        assert token.removeprefix("token-for-") in self.identities  # a token Azure issued
        got = next(((c, i) for c, i in self.identities.items() if i["path"] == path), None)
        if got is None:
            raise RuntimeError("404 ResourceNotFound")
        return {"id": path, "tags": got[1]["tags"], "properties": {"clientId": got[0]}}


@pytest.fixture
def entra():
    return FakeEntra()


@pytest.fixture
def azure_conns(engine, entra, monkeypatch):
    monkeypatch.setenv("VALXEL_CLOUD", "azure")
    c = Connections(engine.services.workspaces, azure=AzureConnect(
        issuer=ISSUER, subject=SUBJECT, exchange=entra.exchange, read=entra.read, tenant=lambda sub: TENANT))
    c.azure.reach_of = c.azure_reach
    engine.services.connections = c
    engine.services.workspaces.bootstrap_owner(WS, {"name": "Asha", "github_login": "asha",
                                                     "email": "asha@farm.example"})
    return c


@pytest.fixture
def client(engine, azure_conns):
    with TestClient(create_app(engine, auth=AuthConfig(mode="dev", secret="x" * 40), web_dist=None)) as c:
        c.post("/api/auth/dev-login", json={"login": "asha"})
        yield c


def test_the_launch_opens_cloud_shell_and_gives_this_workspaces_command(client, azure_conns):
    r = client.get(f"/api/workspaces/{WS}/connections/azure/launch", params={"mode": "full", "region": "westeurope"})
    assert r.status_code == 200, r.text
    assert r.json()["url"].startswith("https://shell.azure.com")
    cmd = r.json()["command"]
    assert "az deployment sub create" in cmd and "--location westeurope" in cmd and "--template-file main.bicep" in cmd
    params = dict(re.findall(r"(\w+)=(\S+)", cmd.split("--parameters", 1)[1].split(" --query")[0]))
    assert params == {"workspace": WS, "connectLabel": connect_label(azure_conns.external_id(WS)), "mode": "full",
                      "valxelIssuer": ISSUER, "valxelSubject": SUBJECT}
    assert azure_conns.external_id(WS) not in cmd
    # The command's parameters are the template's, and its output is what the page asks for.
    bicep = (ROOT / "deploy/azure-connect/main.bicep").read_text()
    assert set(params) <= set(re.findall(r"^param (\w+)", bicep, re.M))
    assert "output connection object" in bicep and "--query properties.outputs.connection.value" in cmd


def test_checking_signs_in_as_both_identities_and_records_the_subscription(client, azure_conns, entra):
    entra.apply_template(azure_conns, WS)
    r = client.post(f"/api/workspaces/{WS}/connections/azure/check",
                    json={"subscription": SUB.upper(), "deploy_client_id": DEPLOY, "agents_client_id": AGENTS,
                          "mode": "lite"})
    assert r.status_code == 200, r.text
    assert r.json()["reach"] == {"account_id": SUB, "tenant": TENANT, "mode": "lite",
                                 "deploy_role": identity_path(SUB, "deploy"),
                                 "agents_role": identity_path(SUB, "agents"),
                                 "deploy_client_id": DEPLOY, "agents_client_id": AGENTS}
    assert {c for _, c in entra.calls} == {DEPLOY, AGENTS}
    page = {t["tool"]: t for t in client.get(f"/api/workspaces/{WS}/connections").json()}
    assert page["azure"]["status"] == "connected" and "aws" not in page and "gcp" not in page
    # Cost and inventory reads find the connection's IDs by subscription.
    assert azure_conns.azure.token(SUB, "agents") == f"token-for-{AGENTS}"


@pytest.mark.parametrize("setup, why", [
    (None, "isn't an identity in that subscription's tenant"),
    ("other-workspace", "set up for another workspace"),
    ("other-valxel", "doesn't trust this Valxel"),
    ("swapped", "isn't valxel-deploy's"),
])
def test_a_check_that_fails_says_why(client, azure_conns, entra, setup, why):
    ids = {"deploy_client_id": DEPLOY, "agents_client_id": AGENTS}
    if setup == "other-workspace":
        entra.apply_template(azure_conns, "someone-else")
    elif setup == "other-valxel":
        entra.apply_template(azure_conns, WS, issuer="https://elsewhere.example/")
    elif setup == "swapped":
        entra.apply_template(azure_conns, WS)
        entra.identities[DEPLOY], entra.identities[AGENTS] = entra.identities[AGENTS], entra.identities[DEPLOY]
    r = client.post(f"/api/workspaces/{WS}/connections/azure/check", json={"subscription": SUB, **ids})
    assert r.status_code == 422 and why in r.json()["detail"], r.text


def test_azure_connect_conforms_to_the_cloud_accounts_port(azure_conns, entra):
    entra.apply_template(azure_conns, WS)
    azure_conns.azure_check(WS, subscription=SUB, deploy_client_id=DEPLOY, agents_client_id=AGENTS, mode="lite",
                            actor="asha")
    assert conformance.cloud_accounts(azure_conns.azure, workspace=WS, account=SUB,
                                      external_id=azure_conns.external_id(WS)) == []
    with pytest.raises(ConnectionError_, match="GUID"):
        azure_conns.azure.check(WS, account="not-a-sub", external_id="x")


def test_a_removed_template_shows_in_the_health_check(azure_conns, entra):
    entra.apply_template(azure_conns, WS)
    azure_conns.azure_check(WS, subscription=SUB, deploy_client_id=DEPLOY, agents_client_id=AGENTS, mode="lite",
                            actor="asha")
    azure_conns.health_checks["azure"] = azure_health(azure_conns)
    assert azure_conns.check(WS) == []
    entra.identities.clear()
    [c] = azure_conns.check(WS)
    assert c["status"] == "needs_attention" and "client ID" in c["last_error"]


def test_the_product_names_its_subscriptions():
    from valxel_sdlc import resources

    cloud = {"provider": "azure", "region": "eastus",
             "accounts": {"dev": SUB, "production": SUB.replace("aa01", "aa03")}}
    assert resources.check_cloud(cloud, "general", ["dev", "production"], {"cloud": "azure", "regions": {}}) == []
    bad = {**cloud, "accounts": {"dev": "123456789012", "production": "123456789012"}}
    got = resources.check_cloud(bad, "general", ["dev", "production"], {"cloud": "azure", "regions": {}})
    assert any("Azure subscription ID" in p for p in got) and any("its own Azure subscription" in p for p in got)
    assert tags_for("azure", {"valxel:product": "FARM"}) == {"valxel:product": "FARM"}


# --------------------------------------------------------------------- costs and inventory (CP-405)
class FakeCostManagement:
    def __init__(self):
        props = fixture("cost-query.json")["properties"]
        names = [c["name"] for c in props["columns"]]
        self.rows = [dict(zip(names, r, strict=True)) for r in props["rows"]]
        self.bodies = []

    def __call__(self, subscription, body):
        self.bodies.append((subscription, body))
        rows = self.rows
        filt = body["dataset"]["filter"]
        tags = filt.get("and") or [filt]
        feature = next((t["tags"]["values"][0] for t in tags if t["tags"]["name"] == "valxel:feature"), "")
        if feature:
            rows = [r for r in rows if r["TagValue"] == feature]
        if "granularity" not in body["dataset"]:
            rows = [{k: v for k, v in r.items() if k != "UsageDate"} for r in rows]
        return rows


def test_costs_come_from_cost_management_by_tag():
    cm = FakeCostManagement()
    src = AzureCosts(cm)
    assert conformance.cost_source(src, product_id="AG", feature_id="AG-FEAT-7", account=SUB, day="2026-11-23",
                                   expect={"Azure Cosmos DB": 12.5, "Service Bus": 3.25}) == []
    sub, body = cm.bodies[0]
    assert sub == SUB and body["type"] == "ActualCost" and body["timeframe"] == "Custom"
    assert {"type": "TagKey", "name": "valxel:feature"} in body["dataset"]["grouping"]
    daily = src.cloud_costs_daily(product_id="AG", account=SUB, start=datetime(2026, 11, 23, tzinfo=UTC),
                                  end=datetime(2026, 11, 24, tzinfo=UTC))
    assert daily[-1] == {"day": "2026-11-23", "service": "Storage", "feature_id": "", "usd": 7.0}


def test_the_ledger_files_azure_spend_under_azure():
    from valxel_sdlc.finops.collect import collect_cloud, rates

    recs = collect_cloud(AzureCosts(FakeCostManagement()), "AG", {"production": SUB}, {"AG-FEAT-7": None},
                         datetime(2026, 11, 24, tzinfo=UTC), rates(), provider="azure")
    assert {(r.category, r.source, r.feature_id, r.stage) for r in recs} == {
        ("azure", "cost-management", "AG-FEAT-7", "build"), ("azure", "cost-management", "", "run")}


def test_inventory_lists_what_carries_the_products_tags():
    queries = []

    def graph(sub, kql):
        queries.append((sub, kql))
        return fixture("resource-graph.json")["data"]

    inv = AzureResources(graph)
    ids = {r["id"] for r in fixture("resource-graph.json")["data"]}
    assert conformance.resource_inventory(inv, account=SUB, product_id="AG", expect_ids=ids) == []
    assert "tags['valxel:product'] =~ 'AG'" in queries[0][1]
    assert {r["feature"] for r in inv.tagged(product_id="AG", account=SUB)} == {"AG-FEAT-7", "AG-FEAT-9"}
    with pytest.raises(ValueError, match="product id"):
        inv.resources(account=SUB, product_id="AG' or 1==1 //")


# --------------------------------------------------------------------- signals (CP-406)
class Source:
    def __init__(self, kind, parse):
        self.kind, self.parse = kind, parse


def test_azure_monitor_alerts_fire_and_resolve():
    src = Source("azure-monitor", sig.from_azure_monitor)
    assert conformance.signal_source(src, firing=fixture("monitor-fired.json"),
                                     resolved=fixture("monitor-resolved.json"),
                                     unrelated=fixture("budget.json")) == []
    [s] = sig.from_azure_monitor(fixture("monitor-fired.json"))
    assert (s["product_id"], s["component"], s["severity"], s["labels"]["subscription"]) == (
        "AG", "agtech-field-worker", "sev2", SUB)
    assert s["title"] == "field-readings: consumers behind" and "five minutes behind" in s["detail"]
    other = fixture("monitor-fired.json")
    other["data"]["essentials"]["description"] = "someone else's alert"
    assert sig.from_azure_monitor(other) == []  # only Valxel's alert rules


def test_budget_alerts_name_the_product_and_environment():
    [s] = sig.from_azure_budget(fixture("budget.json"), ["AG", "AGX"])
    assert (s["product_id"], s["env"], s["severity"], s["labels"]["subscription"]) == (
        "AG", "production", "sev3", SUB.replace("aa01", "aa03"))
    assert "90%" in s["title"] and "912.40 USD" in s["detail"]
    assert sig.from_azure_budget(fixture("monitor-fired.json"), ["AG"]) == []


def test_defender_vulnerabilities_open_incidents_for_the_component():
    src = Source("defender", sig.from_defender)
    assert conformance.signal_source(src, firing=fixture("defender-vulnerability.json"),
                                     resolved=fixture("defender-fixed.json"),
                                     unrelated=fixture("monitor-fired.json")) == []
    [s] = sig.from_defender(fixture("defender-vulnerability.json"))
    assert (s["component"], s["severity"], s["labels"]["subscription"]) == (
        "agtech-field-api", "sev2", SUB.replace("aa01", "aa03"))
    assert "CVE-2026-1234" in s["title"] and "1.4.2" in s["detail"]


def test_subscription_tokens():
    t = derive("s" * 40, "subscription", SUB)
    assert t.startswith("vxs_") and t != derive("s" * 40, "project", "farm-prod")
    with pytest.raises(ValueError, match="subscription ID"):
        derive("s" * 40, "subscription", "farm-prod")


@pytest.fixture
def signals_client(engine, monkeypatch):
    monkeypatch.setenv("VALXEL_SIGNALS_TOKEN", "s" * 40)
    return TestClient(create_app(engine, auth=AuthConfig(mode="dev", secret="x" * 40), web_dist=None))


def test_a_subscriptions_token_sends_that_subscriptions_events_only(signals_client):
    good = token_for("subscription", SUB, "s" * 40)
    fired = fixture("monitor-fired.json")
    assert signals_client.post(f"/api/signals/azure-monitor?token={good}", json=fired).status_code == 202
    other = token_for("subscription", SUB.replace("aa01", "aa02"), "s" * 40)
    assert signals_client.post(f"/api/signals/azure-monitor?token={other}", json=fired).status_code == 401
    assert signals_client.post("/api/signals/azure-monitor", json=fired).status_code == 401
    prod = token_for("subscription", SUB.replace("aa01", "aa03"), "s" * 40)
    assert signals_client.post(f"/api/signals/defender?token={prod}",
                               json=fixture("defender-vulnerability.json")).status_code == 202


def test_azure_budgets_take_the_platform_token(signals_client):
    assert signals_client.post(f"/api/signals/azure-budgets?token={'s' * 40}",
                               json=fixture("budget.json")).status_code == 202
    sub = token_for("subscription", SUB, "s" * 40)
    refused = signals_client.post(f"/api/signals/azure-budgets?token={sub}", json=fixture("budget.json"))
    assert refused.status_code == 401


# --------------------------------------------------------------------- the catalog on Azure
def test_an_azure_installation_sees_azures_services():
    from valxel_sdlc import resources

    text = (ROOT / "examples/governance/valxel-platform/sdlc-governance/platform/catalog.yaml").read_text()
    az = resources.load_catalog(text, "azure")
    assert az["types"]["key-value-table"]["service"].startswith("Azure Cosmos DB")
    assert az["types"]["queue"]["access"]["write"] == ["role:Azure Service Bus Data Sender"]
    assert az["types"]["event-stream"]["parameters"]["retention_hours"]["max"] == 168
    assert az["types"]["workload-identity"]["outputs"] == {"role": "AZURE_CLIENT_ID"}
    assert az["unavailable"] == ["cache"] and az["regions"]["general"][0] == "eastus"
    hld = ("# HLD\n\n## ADRs\n- ADR-1: Cosmos DB for field-state\n\n## Resources\n"
           "- field-state: key-value-table; partition_key=field_id\n  reads, writes: farm-api\n")
    repos = [{"name": "o/farm-api", "role": "provider"}]
    subs = {"region": "eastus", "provider": "azure",
            "accounts": {"dev": SUB, "staging": SUB.replace("aa01", "aa02"),
                         "production": SUB.replace("aa01", "aa03")}}
    ctx = {"aws_catalog": az, "cloud": subs, "data_class": "general", "pipeline": ["dev", "staging", "production"],
           "repos": repos}
    assert resources.check(hld, repos, ctx) == []
    out = resources.plan(hld, az, lambda comp: ("farm", None), product_id="FARM", feature_id="FARM-FEAT-7",
                         data_class="general")
    r, ident = (next(x for x in out if x["name"] == n) for n in ("field-state", "farm-api-identity"))
    assert r["parameters"]["consumers"][0]["actions"] == ["cosmos:Cosmos DB Built-in Data Contributor",
                                                          "cosmos:Cosmos DB Built-in Data Reader"]
    assert r["parameters"]["tags"]["valxel:feature"] == "FARM-FEAT-7" and ident["type"] == "workload-identity"
    assert "Azure Cosmos DB" in resources.catalog_guide(az, "general")


def test_a_known_subscription_is_selected_and_its_output_comes_back_by_itself(client, azure_conns, entra):
    r = client.get(f"/api/workspaces/{WS}/connections/azure/launch",
                   params={"mode": "full", "region": "eastus2", "account": SUB.upper()})
    assert r.status_code == 200, r.text
    cmd = r.json()["command"]
    assert cmd.startswith(f"az account set --subscription {SUB} && git clone")
    url = re.search(r"--data @- '([^']+)'", cmd).group(1)
    path = "/api" + url.split("/api", 1)[1]
    poll = f"/api/workspaces/{WS}/connections/azure/poll"
    assert client.get(poll, params={"account": SUB}).json()["status"] == "waiting"
    entra.apply_template(azure_conns, WS)
    out = {"subscription": SUB, "tenant": TENANT, "deployClientId": DEPLOY, "agentsClientId": AGENTS}
    with TestClient(client.app) as anon:  # Cloud Shell isn't signed in to Valxel: the code is the credential
        wrong = anon.post(path, json={**out, "subscription": "00000000-0000-4000-8000-00000000000f"})
        assert wrong.status_code == 422 and "az account set" in wrong.json()["detail"]
    r2 = client.get(f"/api/workspaces/{WS}/connections/azure/launch",
                    params={"mode": "full", "region": "eastus2", "account": SUB})
    path = "/api" + re.search(r"--data @- '([^']+)'", r2.json()["command"]).group(1).split("/api", 1)[1]
    with TestClient(client.app) as anon:
        ok = anon.post(path, json=out)
        assert ok.status_code == 200, ok.text
        assert ok.json() == {"connected": SUB, "workspace": WS}
        assert anon.post(path, json=out).status_code == 422  # once only
        assert anon.post("/api/connect/azure/nope", json=out).status_code == 422
        assert anon.post(path, content=b"not json", headers={"Content-Type": "application/json"}).status_code == 400
    page = client.get(poll, params={"account": SUB}).json()
    assert page["status"] == "connected" and page["connection"]["reach"]["mode"] == "full"

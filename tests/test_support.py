"""The Support page and the tool as Grafana's and MLflow's sign-in (valxel_sdlc.support,
web/oidc_provider.py): who may open what, and the authorization-code flow end to end."""

import base64
import hashlib
import json
import secrets
from urllib.parse import parse_qs, urlparse

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient

from valxel_sdlc import support
from valxel_sdlc.api.app import create_app
from valxel_sdlc.web.auth import AuthConfig, User
from valxel_sdlc.web.oidc_provider import Keys, MemoryCodes, OIDCError, Provider, clients_from_env
from valxel_sdlc.workspaces import WorkspaceError

WS = "wrkspc-shared"  # FARM and SHOP
ISSUER = "http://testserver/api/oidc"
GRAFANA_CB = "https://grafana.example.com/login/generic_oauth"
MLFLOW_CB = "https://mlflow.prod.farm.example.com/oauth2/callback"


def pem() -> str:
    key = ec.generate_private_key(ec.SECP256R1())
    return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption()).decode()


def sha(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


CLIENTS = [
    {"id": "grafana", "secret_sha256": sha("g-secret"), "redirect_uri": GRAFANA_CB, "kind": "grafana"},
    {"id": "mlflow-farm-prod", "secret_sha256": sha("m-secret"), "redirect_uri": MLFLOW_CB, "kind": "mlflow",
     "product": "FARM", "plane": "prod"},
]


def pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    return verifier, base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")


# ---------------------------------------------------------------------- who may open what

def test_members_and_the_support_group_view_admins_edit_and_mlflow_needs_a_grant():
    member = User(login="ben", roles={WS: "member"})
    admin = User(login="asha", roles={WS: "admin"})
    helper = User(login="sam", groups=[f"{WS}/support"], roles={WS: "viewer"})
    outsider = User(login="eve")
    assert support.access(member, "FARM", WS, ["FARM"], []) == support.Access("Viewer", False)
    assert support.access(admin, "FARM", WS, ["FARM"], []) == support.Access("Editor", False)
    # The Support group reaches a product even when their own profile doesn't list it.
    assert support.access(helper, "FARM", WS, [], []).grafana == "Viewer"
    assert support.access(outsider, "FARM", WS, [], ["support"]) == support.Access()
    # MLflow only through a granted group; being an admin isn't enough.
    assert not support.access(admin, "FARM", WS, ["FARM"], ["support"]).mlflow
    assert support.access(helper, "FARM", WS, [], ["support"]).mlflow


def test_mlflow_access_is_granted_per_product_by_existing_groups(engine):
    w = engine.services.workspaces
    w.bootstrap_owner(WS, {"github_login": "asha"})
    assert "support" in {g["slug"] for g in w.groups(WS)}
    assert w.support("FARM") == {"product": "FARM", "mlflow_groups": [], "version": 0}
    with pytest.raises(WorkspaceError, match="No such groups"):
        w.set_support("FARM", mlflow_groups=["nobody"], actor="asha")
    assert w.set_support("FARM", mlflow_groups=["support"], actor="asha")["version"] == 1
    assert w.support("SHOP")["mlflow_groups"] == []
    with pytest.raises(WorkspaceError):
        w.delete_group(WS, "support", actor="asha")  # in use
    assert "support.set" in [a["action"] for a in w.store.audit_log(WS)]


def test_clients_are_registered_exactly():
    with pytest.raises(ValueError, match="https"):
        clients_from_env(json.dumps([{**CLIENTS[0], "redirect_uri": "http://grafana.example.com/cb"}]))
    with pytest.raises(ValueError, match="product and plane"):
        clients_from_env(json.dumps([{**CLIENTS[1], "plane": ""}]))
    p = Provider(issuer=ISSUER, keys=Keys(pem()), clients=clients_from_env(json.dumps(CLIENTS)), codes=MemoryCodes())
    _, challenge = pkce()
    ok = {"client_id": "grafana", "redirect_uri": GRAFANA_CB, "response_type": "code", "scope": "openid email",
          "code_challenge": challenge, "code_challenge_method": "S256"}
    assert p.check_authorize(ok).kind == "grafana"
    for bad in ({"redirect_uri": GRAFANA_CB + "/x"}, {"code_challenge_method": "plain"}, {"scope": "email"},
                {"response_type": "token"}, {"client_id": "other"}):
        with pytest.raises(OIDCError):
            p.check_authorize({**ok, **bad})


# ---------------------------------------------------------------------- the flow, end to end

@pytest.fixture
def client(engine, monkeypatch):
    monkeypatch.setenv("VALXEL_OIDC_ISSUER", ISSUER)
    monkeypatch.setenv("VALXEL_OIDC_SIGNING_KEY", pem())
    monkeypatch.setenv("VALXEL_OIDC_CLIENTS", json.dumps(CLIENTS))
    monkeypatch.setenv("VALXEL_GRAFANA_URL", "https://grafana.example.com")
    monkeypatch.setenv("VALXEL_MLFLOW_URL", "https://mlflow.{plane}.{product}.example.com")
    w = engine.services.workspaces
    w.bootstrap_owner(WS, {"github_login": "asha"})
    for login, role in (("ben", "member"), ("sam", "viewer")):
        _, token = w.invite(WS, {"github_login": login}, role=role, actor="asha",
                            products=["SHOP"] if login == "sam" else ["FARM"])
        w.accept(WS, token, login=login)
    sam = next(m for m in w.members(WS) if m.get("github_login") == "sam")
    owner = next(m for m in w.members(WS) if m.get("github_login") == "asha")
    w.set_group(WS, "support", members=[owner["id"], sam["id"]], actor="asha")
    with TestClient(create_app(engine, auth=AuthConfig(mode="dev", secret="x" * 40), web_dist=None)) as c:
        yield c


def login(client, name):
    assert client.post("/api/auth/dev-login", json={"login": name, "orgs": ["someone-else"]}).status_code == 200


def sign_in(client, product, to, client_id, redirect, plane=""):
    """Open from the Support page, then come back to authorize as Grafana or oauth2-proxy would."""
    q = f"/api/support/open?product={product}&to={to}" + (f"&plane={plane}" if plane else "")
    opened = client.get(q, follow_redirects=False)
    if opened.status_code != 303:
        return opened, None
    verifier, challenge = pkce()
    r = client.get("/api/oidc/authorize", params={
        "client_id": client_id, "redirect_uri": redirect, "response_type": "code", "scope": "openid profile email",
        "state": "st", "nonce": "nn", "code_challenge": challenge, "code_challenge_method": "S256"},
        follow_redirects=False)
    return r, verifier


def exchange(client, code, verifier, client_id, secret, redirect):
    return client.post("/api/oidc/token", data={"grant_type": "authorization_code", "code": code,
                                               "redirect_uri": redirect, "code_verifier": verifier},
                       auth=(client_id, secret))


def test_a_member_signs_in_to_grafana_as_viewer_for_that_product_only(client):
    disc = client.get("/api/oidc/.well-known/openid-configuration").json()
    assert disc["issuer"] == ISSUER and disc["code_challenge_methods_supported"] == ["S256"]
    login(client, "ben")
    page = client.get("/api/support").json()
    assert [p["product"] for p in page["products"]] == ["FARM"]
    assert [link["to"] for link in page["products"][0]["links"]] == ["grafana"]  # no MLflow grant yet

    r, verifier = sign_in(client, "FARM", "grafana", "grafana", GRAFANA_CB)
    assert r.status_code == 303, r.text
    back = urlparse(r.headers["location"])
    code = parse_qs(back.query)["code"][0]
    assert back.geturl().startswith(GRAFANA_CB) and parse_qs(back.query)["state"] == ["st"]

    assert exchange(client, code, "wrong" * 10, "grafana", "g-secret", GRAFANA_CB).json()["error"] == "invalid_grant"
    # A code is single use: the bad verifier above already spent it.
    r, verifier = sign_in(client, "FARM", "grafana", "grafana", GRAFANA_CB)
    code = parse_qs(urlparse(r.headers["location"]).query)["code"][0]
    assert exchange(client, code, verifier, "grafana", "wrong", GRAFANA_CB).status_code == 401
    tokens = exchange(client, code, verifier, "grafana", "g-secret", GRAFANA_CB).json()
    jwks = client.get("/api/oidc/jwks").json()["keys"][0]
    key = jwt.PyJWK(jwks).key
    claims = jwt.decode(tokens["id_token"], key, algorithms=["ES256"], audience="grafana", issuer=ISSUER)
    assert claims["sub"] == "ben" and claims["groups"] == ["FARM/Viewer"] and claims["nonce"] == "nn"
    assert exchange(client, code, verifier, "grafana", "g-secret", GRAFANA_CB).json()["error"] == "invalid_grant"
    info = client.get("/api/oidc/userinfo", headers={"Authorization": f"Bearer {tokens['access_token']}"}).json()
    assert info["groups"] == ["FARM/Viewer"] and "nonce" not in info

    # SHOP isn't his.
    assert sign_in(client, "SHOP", "grafana", "grafana", GRAFANA_CB)[0].status_code == 403


def test_opening_grafana_directly_is_refused(client):
    login(client, "ben")
    _, challenge = pkce()
    r = client.get("/api/oidc/authorize", params={
        "client_id": "grafana", "redirect_uri": GRAFANA_CB, "response_type": "code", "scope": "openid",
        "code_challenge": challenge, "code_challenge_method": "S256"}, follow_redirects=False)
    assert r.status_code == 403 and "Support page" in r.text
    # An unregistered redirect never gets a code, signed in or not.
    r = client.get("/api/oidc/authorize", params={
        "client_id": "grafana", "redirect_uri": "https://evil.example/cb", "response_type": "code",
        "scope": "openid", "code_challenge": challenge, "code_challenge_method": "S256"}, follow_redirects=False)
    assert r.status_code == 400 and "location" not in r.headers


def test_admins_edit_support_opens_any_product_and_mlflow_follows_the_grant(client, engine):
    login(client, "asha")
    r, verifier = sign_in(client, "SHOP", "grafana", "grafana", GRAFANA_CB)
    code = parse_qs(urlparse(r.headers["location"]).query)["code"][0]
    tokens = exchange(client, code, verifier, "grafana", "g-secret", GRAFANA_CB).json()
    assert jwt.decode(tokens["id_token"], options={"verify_signature": False})["groups"] == ["SHOP/Editor"]

    # Sam is a viewer of SHOP only, but in the Support group: FARM too, as Viewer; no MLflow yet.
    login(client, "sam")
    assert [p["product"] for p in client.get("/api/support").json()["products"]] == ["FARM", "SHOP"]
    assert sign_in(client, "FARM", "mlflow", "mlflow-farm-prod", MLFLOW_CB, plane="prod")[0].status_code == 403
    assert client.put("/api/support/FARM/settings", json={"mlflow_groups": ["support"]}).status_code == 403

    login(client, "asha")
    settings = client.get("/api/support/FARM").json()["settings"]
    assert settings["mlflow_groups"] == [] and "support" in {g["slug"] for g in settings["groups"]}
    assert client.put("/api/support/FARM/settings", json={"mlflow_groups": ["support"]}).status_code == 200

    login(client, "sam")
    links = client.get("/api/support/FARM").json()["links"]
    assert {(link["to"], link.get("plane", "")) for link in links} == {("grafana", ""), ("mlflow", "prod"),
                                                                       ("mlflow", "nonprod")}
    r, verifier = sign_in(client, "FARM", "mlflow", "mlflow-farm-prod", MLFLOW_CB, plane="prod")
    assert r.status_code == 303 and r.headers["location"].startswith(MLFLOW_CB)
    code = parse_qs(urlparse(r.headers["location"]).query)["code"][0]
    claims = jwt.decode(exchange(client, code, verifier, "mlflow-farm-prod", "m-secret", MLFLOW_CB).json()["id_token"],
                        options={"verify_signature": False})
    assert claims["groups"] == ["mlflow:FARM"] and claims["aud"] == "mlflow-farm-prod"
    # The note is for FARM prod: the same client can't be opened for another plane's note.
    assert sign_in(client, "FARM", "mlflow", "mlflow-farm-prod", MLFLOW_CB, plane="nonprod")[0].status_code == 403

    signins = [a for a in engine.services.workspaces.store.audit_log(WS) if a["action"] == "support.signin"]
    assert signins and all("client" in a["detail"] for a in signins)

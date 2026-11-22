"""The intake and incident agents over A2A (api/a2a.py): the card, a person's token, the API's rules."""

import pytest
from fastapi.testclient import TestClient
from test_mcp_server import DOMAIN, ISSUER, pool, token  # noqa: F401 - the fake user pool and its tokens

from valxel_sdlc.api.app import create_app
from valxel_sdlc.web.auth import AuthConfig


@pytest.fixture
def client(engine, pool, monkeypatch):  # noqa: F811
    monkeypatch.setenv("VALXEL_A2A_CLIENT_IDS", "mcp-client")
    auth = AuthConfig(mode="dev", secret="x" * 40, public_url="https://testserver", cognito_issuer=ISSUER,
                      cognito_domain=DOMAIN)
    with TestClient(create_app(engine, auth=auth, web_dist=None)) as c:
        yield c


def call(client, method, params, tok=None, **headers):
    return client.post("/a2a", json={"jsonrpc": "2.0", "id": "1", "method": method, "params": params},
                       headers={"authorization": f"Bearer {tok or token()}", "A2A-Version": "1.0", **headers})


def say(text, context_id=""):
    m = {"messageId": "m1", "role": "ROLE_USER", "parts": [{"text": text}]}
    return {"message": m | ({"contextId": context_id} if context_id else {})}


def test_the_card_says_who_they_are_and_how_to_sign_in(client):
    card = client.get("/.well-known/agent-card.json").json()
    assert card["supportedInterfaces"] == [{"url": "https://testserver/a2a", "protocolBinding": "JSONRPC",
                                            "protocolVersion": "1.0"}]
    assert {s["id"] for s in card["skills"]} == {"intent", "report-incident"}
    flows = card["securitySchemes"]["person"]["oauth2SecurityScheme"]["flows"]
    assert list(flows) == ["authorizationCode"]  # a person signs in; no client-credentials flow
    assert flows["authorizationCode"]["tokenUrl"] == f"https://{DOMAIN}/oauth2/token"


# The claims, not the tokens: a token made at import differs by worker (the time in it), and pytest
# names a test by its parameters, so parallel workers would collect different tests (review #10).
@pytest.mark.parametrize("claims", [{"username": None, "scope": "valxel/agents"}, {"client_id": "someone-elses"}],
                         ids=["client-credentials", "another-apps-client"])
def test_only_a_person_gets_in(client, claims):
    r = call(client, "SendMessage", say("hello"), token(**claims))
    assert r.status_code == 401 and "Bearer" in r.headers["www-authenticate"]
    assert client.post("/a2a", json={"jsonrpc": "2.0", "id": 1, "method": "SendMessage"}).status_code == 401


def test_an_intent_is_a_conversation_until_it_is_submitted(client):
    r = call(client, "SendMessage", say("Patients should confirm consent before intake")).json()
    task = r["result"]["task"]
    assert task["status"]["state"] == "TASK_STATE_INPUT_REQUIRED"
    assert task["status"]["message"]["role"] == "ROLE_AGENT" and task["contextId"] == task["id"]
    again = call(client, "GetTask", {"id": task["id"]}).json()["result"]
    assert again["id"] == task["id"] and len(again["history"]) == len(task["history"])
    early = call(client, "SendMessage", {"message": {"messageId": "m2", "role": "ROLE_USER", "contextId": task["id"],
                                                     "parts": [{"data": {"submit": True}}]}}).json()
    assert "error" in early and "Still needed" in early["error"]["message"]


def test_someone_elses_intent_isnt_found(client, monkeypatch):
    task = call(client, "SendMessage", say("Add a dark mode")).json()["result"]["task"]
    from test_mcp_server import PEOPLE

    monkeypatch.setitem(PEOPLE, "sub-bo", {"sub": "sub-bo", "email": "bo@example.com", "email_verified": "true"})
    r = call(client, "GetTask", {"id": task["id"]}, token(sub="sub-bo", username="sub-bo")).json()
    assert r["error"]["code"] in (-32001, -32600)


def test_a_problem_is_reported_as_the_person(client, engine):
    r = call(client, "SendMessage", {"message": {"messageId": "m3", "role": "ROLE_USER", "parts": [{"data": {
        "skill": "report-incident", "product_id": "CLN", "title": "Intake form times out", "severity": "sev2"}}]}}).json()
    assert "result" in r, r
    task = r["result"]["task"]
    assert task["status"]["state"] == "TASK_STATE_COMPLETED" and task["id"].startswith("incident:")


def test_methods_it_doesnt_offer(client):
    assert call(client, "SendStreamingMessage", say("x")).json()["error"]["code"] == -32004
    assert call(client, "Nope", {}).json()["error"]["code"] == -32601
    assert call(client, "SendMessage", say("x"), **{"A2A-Version": "2.0"}).json()["error"]["code"] == -32009

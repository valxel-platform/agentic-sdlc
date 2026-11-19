"""Signals tokens scoped to their sender (ops/signal_tokens.py): a product's token raises that
product's signals, a data plane account's token that account's events, and nothing else."""

import pytest
from fastapi.testclient import TestClient
from test_alarms import engine, event  # noqa: F401 - the alarm tests' engine and CloudWatch event

from valxel_sdlc.api.app import create_app
from valxel_sdlc.ops.signal_tokens import token_for
from valxel_sdlc.web.auth import AuthConfig

SECRET = "s" * 40


@pytest.fixture
def client(engine, monkeypatch):  # noqa: F811
    monkeypatch.setenv("VALXEL_SIGNALS_TOKEN", SECRET)
    return TestClient(create_app(engine, auth=AuthConfig(mode="dev", secret="x" * 40), web_dist=None))


def bearer(kind, name):
    return {"authorization": f"Bearer {token_for(kind, name, SECRET)}"}


def am(product_component):
    return {"alerts": [{"status": "firing", "fingerprint": f"fp-{product_component}", "startsAt": "2026-11-06T21:00:00Z",
                        "labels": {"alertname": "HighErrorRate", "severity": "critical",
                                   "component": product_component}}]}


def test_a_products_token_raises_that_products_signals_only(client):
    ok = client.post("/api/signals/ticket", headers=bearer("product", "FARM"),
                     json={"product_id": "FARM", "title": "Field list is empty"})
    assert ok.status_code == 202 and ok.json()["incidents"]
    other = client.post("/api/signals/ticket", headers=bearer("product", "FARM"),
                        json={"product_id": "CLN", "title": "Intake is down"})
    assert other.status_code == 403 and "product FARM" in other.json()["detail"]
    # Alertmanager names a component, not a product: the component decides whose it is.
    assert client.post("/api/signals/alertmanager", headers=bearer("product", "FARM"),
                       json=am("cln-intake-api")).status_code == 403
    assert client.post("/api/signals/alertmanager", headers=bearer("product", "CLN"),
                       json=am("cln-intake-api")).status_code == 202


def test_an_accounts_token_sends_that_accounts_events_only(client):
    r = client.post("/api/signals/cloudwatch", json=event(), headers=bearer("account", "333333333333"))
    assert r.status_code == 202 and r.json()["recorded"] == 1
    # The same token claiming another account's event, or another account's token:
    assert client.post("/api/signals/cloudwatch", json=event(env="staging"),
                       headers=bearer("account", "333333333333")).status_code == 401
    assert client.post("/api/signals/cloudwatch", json=event(),
                       headers=bearer("account", "222222222222")).status_code == 401
    # A product's token isn't an account's.
    assert client.post("/api/signals/cloudwatch", json=event(), headers=bearer("product", "FARM")).status_code == 401


def test_the_platform_token_until_tokens_are_scoped(client, monkeypatch):
    platform = {"authorization": f"Bearer {SECRET}"}
    assert client.post("/api/signals/ticket", headers=platform,
                       json={"product_id": "CLN", "title": "Intake is down"}).status_code == 202
    monkeypatch.setenv("VALXEL_SIGNALS_TOKENS", "scoped")
    assert client.post("/api/signals/ticket", headers=platform,
                       json={"product_id": "CLN", "title": "Intake is slow"}).status_code == 401
    assert client.post("/api/signals/ticket", headers=bearer("product", "CLN"),
                       json={"product_id": "CLN", "title": "Intake is slow"}).status_code == 202


def test_a_rotated_secret_keeps_old_tokens_working_for_now(client, monkeypatch):
    old = token_for("product", "FARM", SECRET)
    monkeypatch.setenv("VALXEL_SIGNALS_TOKEN", "n" * 40)
    monkeypatch.setenv("VALXEL_SIGNALS_TOKEN_PREVIOUS", SECRET)
    assert client.post("/api/signals/ticket", headers={"authorization": f"Bearer {old}"},
                       json={"product_id": "FARM", "title": "Rain import late"}).status_code == 202


def test_names_are_checked():
    with pytest.raises(ValueError):
        token_for("account", "12345", SECRET)
    with pytest.raises(ValueError):
        token_for("workspace", "x", SECRET)
    assert token_for("product", "CLN", SECRET) != token_for("product", "FARM", SECRET)
    assert token_for("product", "CLN", "") == ""


def test_the_cli_prints_a_senders_header(monkeypatch, capsys):
    from valxel_sdlc.cli import main

    monkeypatch.setenv("VALXEL_SIGNALS_TOKEN", SECRET)
    assert main(["signals-token", "account", "333333333333"]) == 0
    assert capsys.readouterr().out.strip() == f"Authorization: Bearer {token_for('account', '333333333333', SECRET)}"

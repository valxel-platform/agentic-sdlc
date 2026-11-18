"""Recorded HTTP for the live smoke tests (valxel_sdlc.cassettes): a changed request fails."""

import json
from urllib.request import Request

import httpx
import pytest

from valxel_sdlc.cassettes import (
    Cassette,
    CassetteMismatch,
    httpx_transport,
    scrub,
    urllib_opener,
)


def _cassette(tmp_path, exchanges):
    p = tmp_path / "c.json"
    p.write_text(json.dumps({"_meta": {}, "exchanges": exchanges}))
    return Cassette(p)


EX = [{"request": {"method": "POST", "url": "gw.example/v1/x", "body": {"a": 1}, "headers": {"x-valxel-phase": "spec"}},
       "response": {"status": 200, "body": {"ok": True}}}]


def test_a_replayed_request_must_be_the_one_recorded(tmp_path):
    c = _cassette(tmp_path, EX)
    with httpx.Client(transport=httpx_transport(c)) as http:
        r = http.post("https://gw.example/v1/x", json={"a": 1}, headers={"x-valxel-phase": "spec", "authorization": "k"})
        assert r.json() == {"ok": True}
    c.done()
    for body, headers, why in (({"a": 2}, {"x-valxel-phase": "spec"}, "body differs"),
                               ({"a": 1}, {"x-valxel-phase": "plan"}, "header x-valxel-phase")):
        c = _cassette(tmp_path, EX)
        with httpx.Client(transport=httpx_transport(c)) as http, pytest.raises(CassetteMismatch, match=why):
            http.post("https://gw.example/v1/x", json=body, headers=headers)


def test_a_client_that_skips_or_adds_a_call_fails(tmp_path):
    c = _cassette(tmp_path, EX)
    with pytest.raises(CassetteMismatch, match="never asked for"):
        c.done()
    c = _cassette(tmp_path, [])
    with httpx.Client(transport=httpx_transport(c)) as http, pytest.raises(CassetteMismatch, match="no more exchanges"):
        http.get("https://gw.example/v1/y")


def test_urllib_replays_errors_as_http_errors(tmp_path):
    from urllib.error import HTTPError

    c = _cassette(tmp_path, [{"request": {"method": "GET", "url": "api.github.com/repos/a/b", "body": None},
                              "response": {"status": 404, "body": {"message": "Not Found"}}}])
    with pytest.raises(HTTPError) as e:
        urllib_opener(c).open(Request("https://api.github.com/repos/a/b"))
    assert e.value.code == 404


def test_recording_keeps_no_secrets(tmp_path):
    assert scrub({"token": "t", "nested": [{"api_key": "k", "keep": "v"}], "password": 3}) == \
        {"token": "<masked>", "nested": [{"api_key": "<masked>", "keep": "v"}], "password": 3}

    class Real(httpx.BaseTransport):
        def handle_request(self, request):
            return httpx.Response(201, json={"token": "secret-tok", "id": "s1"})

    c = Cassette(tmp_path / "rec.json", record=True)
    with httpx.Client(transport=httpx_transport(c, real=Real())) as http:
        assert http.post("https://gw.example/v1/sessions", json={"budget_usd": 1},
                         headers={"x-api-key": "the-key"}).json()["token"] == "secret-tok"
    c.save()
    text = (tmp_path / "rec.json").read_text()
    assert "secret-tok" not in text and "the-key" not in text and '"budget_usd": 1' in text

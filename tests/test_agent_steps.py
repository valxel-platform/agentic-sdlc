"""What an agent is doing, as it does it (T-112): steps from Claude Code's messages, safe to show;
streamed from the sandbox (or the session container) to the worker, published to the feature's
page, and readable only by the product's members."""

from __future__ import annotations

import json
from dataclasses import dataclass, field

import httpx
from fastapi.testclient import TestClient
from test_api import client  # noqa: F401

from valxel_sdlc import agent_steps as st
from valxel_sdlc import session_launcher as sl
from valxel_sdlc import session_runner as sr


@dataclass
class Use:
    name: str
    input: dict
    id: str = "t1"


@dataclass
class Result:
    tool_use_id: str
    content: str


@dataclass
class Msg:
    content: list = field(default_factory=list)


def test_steps_say_what_without_contents_or_commands():
    got = []
    r = st.Reader(got.append)
    r.feed(Msg([Use("Read", {"file_path": "/work/repo/src/queue.py"})]))
    r.feed(Msg([Use("Read", {"file_path": "/work/repo/src/queue.py"})]))  # the same step once
    r.feed(Msg([Use("Edit", {"file_path": "/work/repo/src/queue.py", "old_string": "SECRET", "new_string": "x"})]))
    r.feed(Msg([Use("Bash", {"command": "GITHUB_TOKEN=ghs_abc123 pytest -q tests/"}, id="t9")]))
    r.feed(Msg([Result("t9", "....F\n=== 3 failed, 40 passed in 2.1s ===")]))
    r.feed(Msg([Use("Bash", {"command": "git push origin feature/x"})]))
    r.feed(Msg([Use("Bash", {"command": "curl -H 'Authorization: token ghs_x' https://api.github.com"})]))
    assert got == ["Reading src/queue.py", "Editing src/queue.py", "Running the tests", "Tests: 3 failing, 40 passing",
                   "Pushing the branch", "Running curl"]
    assert not any("ghs_" in s or "SECRET" in s for s in got)
    assert st.test_summary("== 12 passed ==") == "Tests: all 12 passing"


def fake_query(prompt, options, on_step=None):
    if on_step:
        on_step("Reading src/a.py")
        on_step("Running the tests")
    return {"text": "done", "session_id": "s1", "usage": {}}


SPEC = {"version": sr.VERSION, "runtime": "claude_code", "id": "a", "prompt": "hi", "options": {}, "env": {},
        "plugins": False, "fallback": {"prompt": "hi", "session_mode": "fresh"}, "timeout_s": 60}


def test_the_runner_streams_steps_then_the_outcome(tmp_path):
    c = TestClient(sr.make_app(work=tmp_path, query=fake_query))
    r = c.post("/run", json={**SPEC, "stream": True})
    lines = [json.loads(x) for x in r.text.splitlines() if x.strip()]
    assert lines[:2] == [{"step": "Reading src/a.py"}, {"step": "Running the tests"}]
    assert lines[-1]["outcome"]["ok"] and lines[-1]["outcome"]["text"] == "done"
    got = []
    assert st.read(r.text.splitlines(), got.append)["text"] == "done" and got == ["Reading src/a.py", "Running the tests"]
    assert st.read([], None) == {"ok": False, "error": "StreamEnded", "transient": True}
    plain = TestClient(sr.make_app(work=tmp_path / "b", query=fake_query)).post("/run", json=SPEC).json()
    assert plain["ok"] and plain["text"] == "done"  # without stream, as before


def test_the_local_executor_passes_steps_on(tmp_path):
    got = []
    out = sr.LocalExecutor(query=fake_query).run(SPEC, on_step=got.append)
    assert out["ok"] and got == ["Reading src/a.py", "Running the tests"]


def test_steps_come_through_the_launcher_and_the_container_is_removed():
    from test_session_launcher import ENV, FakeDocker

    stream = st.line({"step": "Editing src/a.py"}) + st.line({"outcome": {"ok": True, "text": "done"}})

    def handle(req):
        if req.url.path == "/healthz":
            return httpx.Response(200, json={"ok": True})
        assert json.loads(req.content)["stream"] is True
        return httpx.Response(200, content=stream)

    d = FakeDocker()
    launcher = sl.Launcher(sl.Settings(ENV), docker=d, http=httpx.Client(transport=httpx.MockTransport(handle)),
                           sleep=lambda s: None)
    app = TestClient(sl.make_app(launcher))
    ex = sl.DockerExecutor(url="http://testserver", token="t0k", http=app)
    got = []
    out = ex.run({"id": "z", "timeout_s": 60}, on_step=got.append)
    assert out == {"ok": True, "text": "done"} and got == ["Editing src/a.py"]
    assert [c[0] for c in d.calls] == ["create", "start", "remove"]


def test_the_page_hears_a_new_step_and_reads_it_where_access_is_checked(monkeypatch):
    events, kept = [], {}
    st.configure(events.append, keep=lambda fid, steps: kept.__setitem__(fid, steps))
    step = st.reporter("CLN-FEAT-3", "build", "coder", "acme-clinic/cln-intake-api")
    step("Editing src/queue.py")
    assert events == [{"type": "feature.progress", "feature_id": "CLN-FEAT-3"}]  # no paths on the broadcast
    [s] = kept["CLN-FEAT-3"]
    assert s["step"] == "Editing src/queue.py" and s["repo"] == "cln-intake-api" and s["role"] == "coder"
    st.configure(None)


def test_only_the_products_members_read_its_steps(client, engine):  # noqa: F811
    from test_api import CLN_TEAMS, sign_in, wait_idle

    sign_in(client, "maria", teams=CLN_TEAMS)
    fid = client.post("/api/features", json={"product_id": "CLN", "title": "Consent check on intake"}).json()["feature_id"]
    wait_idle(engine, fid)
    st.reporter(fid, "build", "coder")("Editing src/consent.py")
    [s] = client.get(f"/api/features/{fid}/steps").json()["steps"]
    assert s["step"] == "Editing src/consent.py"
    sign_in(client, "farm-person", orgs=["acme-farm"])
    assert client.get(f"/api/features/{fid}/steps").status_code == 403

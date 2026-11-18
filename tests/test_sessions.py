"""Resume a session or hand off (the context design, C5 and C6).

The coder that fixes its own failing tests resumes its session: same task, role and model, within
a day, with room left and its transcript kept. Anything else starts fresh, from the handoff the
last session left. Transcripts are kept a day, beside the feature's artifacts, then deleted."""

import asyncio
import os
import time

import pytest

from valxel_sdlc import sessions
from valxel_sdlc.sessions import Handoff, TranscriptStore, may_resume
from valxel_sdlc.tenancy import thread_folder

NOW = 1_800_000_000.0


def prev(**over):
    return {"task": "TASK-1", "session_id": "s-1", "role": "coder", "model": "anthropic/claude-sonnet-5-5",
            "runtime": "claude_code", "at": NOW - 600, "fill": 0.2, **over}


def decide(p, **over):
    kw = {"task_id": "TASK-1", "role": "coder", "model": "anthropic/claude-sonnet-5-5", "runtime": "claude_code",
          "now": NOW, **over}
    return may_resume(p, **kw)


def test_the_resume_rule():
    assert decide(prev()) == sessions.Decision(True, "same task, role and model, recent, room left")
    for p, kw, why in [
        (None, {}, "no earlier session"),
        (prev(session_id=""), {}, "no earlier session"),
        (prev(), {"task_id": "TASK-2"}, "a different task"),
        (prev(), {"role": "coder_codex"}, "a different role"),  # a fallback never resumes the coder's session
        (prev(), {"model": "anthropic/claude-opus-5-5"}, "a different model"),
        (prev(runtime="codex"), {"runtime": "codex"}, "codex sessions aren't resumed yet"),
        (prev(at=NOW - 25 * 3600), {}, "more than 24 hours"),
        (prev(fill=0.7), {}, "70% full"),
    ]:
        d = decide(p, **kw)
        assert not d.resume and why in d.why, (kw, d.why)


def test_transcripts_are_kept_a_day_then_deleted(tmp_path):
    store = TranscriptStore(tmp_path)
    src = tmp_path / "s-1.jsonl"
    src.write_text('{"type":"user"}\n{"type":"assistant"}\n')
    assert store.keep("CLN-FEAT-1", "s-1", src) and store.has("CLN-FEAT-1", "s-1")
    assert not store.keep("CLN-FEAT-1", "s-2", tmp_path / "missing.jsonl")
    assert decide(prev(), transcripts=store, feature_id="CLN-FEAT-1").resume
    assert decide(prev(session_id="s-9"), transcripts=store, feature_id="CLN-FEAT-1").why == "its transcript is gone"
    out = store.restore("CLN-FEAT-1", "s-1", tmp_path / "home" / "p" / "s-1.jsonl")
    assert out.read_text() == src.read_text()
    kept = thread_folder(tmp_path, "CLN-FEAT-1") / "sessions" / "s-1.jsonl.gz"
    assert kept.is_file() and kept.read_bytes()[:2] == b"\x1f\x8b"  # gzipped
    os.utime(kept, (time.time() - 25 * 3600,) * 2)
    assert store.expire() == 1 and not store.has("CLN-FEAT-1", "s-1")
    with pytest.raises(ValueError):
        store.keep("../etc", "s-1", src)
    with pytest.raises(ValueError):
        store.keep("CLN-FEAT-1", "../../x", src)


def test_claude_codes_transcript_is_found_wherever_it_keeps_it(tmp_path):
    d = sessions.claude_project_dir(str(tmp_path), "/tmp/valxel-box.abc/work")
    assert d.name == "-tmp-valxel-box-abc-work" and d.parent == tmp_path / ".claude" / "projects"
    d.mkdir(parents=True)
    (d / "abc-123.jsonl").write_text("{}")
    assert sessions.find_transcript(str(tmp_path), "abc-123") == d / "abc-123.jsonl"
    assert sessions.find_transcript(str(tmp_path), "nope") is None
    assert sessions.find_transcript(str(tmp_path), "../x") is None


def test_a_handoff_reads_as_a_short_document_and_arrives_as_untrusted_text():
    h = Handoff(done=["Added the endpoint"], open=["Make test_ac1 pass"], decisions=["Kept v1 (callers)"],
                files=["src/api/log.py"], avoid=["Mocking the clock"])
    md = h.markdown("CLN-FEAT-1 TASK-1")
    assert md.startswith("# Handoff: CLN-FEAT-1 TASK-1") and "## Don't try again\n- Mocking the clock" in md
    assert Handoff().empty() and not h.empty()
    note = sessions.picking_up(md)
    assert "don't repeat what it says didn't work" in note and "Make test_ac1 pass" in note and "<untrusted" in note


def test_a_coder_fixing_its_own_failing_tests_resumes_and_the_feature_keeps_the_record(engine, monkeypatch):
    """Stub mode's sessions act like Claude Code's: the first attempt fails its tests (a simulated
    fault), leaves a handoff and its transcript; the retry resumes it with only what to fix."""
    from valxel_sdlc.runtime.generic import GatewayRuntime

    seen, real = [], GatewayRuntime.run

    def spy(self, task):
        out = real(self, task)
        if task.phase == "build" and task.role == "coder":
            seen.append((task, out))
        return out

    monkeypatch.setattr(GatewayRuntime, "run", spy)
    fid = engine.start(product_id="CLN", title="Lab results viewer", description="x",
                       simulate_faults={"build": 1})["feature_id"]
    drive(engine, fid)
    attempts = [(t, o) for t, o in seen if "TASK-1" in t.layers.tail[:200]]
    assert len(attempts) >= 2
    (t1, o1), (t2, o2) = attempts[0], attempts[1]
    assert t1.resume == "" and t1.keep_transcript and t1.session_mode == "fresh"
    assert t2.resume == o1.session_id and o2.resumed and t2.session_mode == "resumed"
    assert t2.layers.tail.startswith("Continue TASK-1 on the same branch")  # only what to fix
    assert "handoff" in t2.fresh_tail and "make test_task_1 pass" in t2.fresh_tail  # if the resume fails
    assert len(t2.layers.tail) < len(t1.layers.tail) / 3
    s = engine._state(fid)["sessions"]
    assert s["TASK-1"]["session_id"] == o1.session_id and s["TASK-1"]["resumed"] is True
    assert s["TASK-1"]["role"] == "coder" and s["TASK-1"]["runtime"] == "claude_code"
    # The first attempt's handoff is in the feature's artifacts.
    assert any(p.name.startswith("HANDOFF-TASK-1.v") for p in thread_folder(engine.services.settings.artifact_dir, fid).iterdir())


def drive(eng, fid):
    while True:
        gates = [x for x in eng.pending(fid) if x["type"] == "gate"]
        if not gates or any(x["type"] != "gate" for x in eng.pending(fid)):
            return eng.view(fid)
        eng.decide(fid, gates[0]["interrupt_id"], {"decision": "approve", "approver": "maria"})


def test_a_fallback_or_a_lost_transcript_starts_fresh_from_the_handoff(engine, monkeypatch):
    from valxel_sdlc.graph import phase as phase_mod
    from valxel_sdlc.runtime.generic import GatewayRuntime

    services = engine.services
    fid = engine.start(product_id="CLN", title="Lab results viewer", description="x")["feature_id"]
    ctx = {"product_id": "CLN", "feature_id": fid, "constitution_version": "v"}
    ref = services.artifacts.save(feature_id=fid, kind="HANDOFF-TASK-1", phase="build", parents=[],
                                  constitution_version="v", content=Handoff(open=["finish the parser"]).markdown("x"))
    seen = []
    monkeypatch.setattr(GatewayRuntime, "run", lambda self, task: seen.append(task) or phase_mod.AgentResult(
        text="{}", tokens=1, cost_usd=0.0, session_id="s-new", runtime=self.name, model="m"))
    p = prev(session_id="s-gone", handoff=dict(ref), at=time.time())
    phase_mod.call_agent(services, role="coder", phase="build", ctx=ctx, skills=(), tail="Implement TASK-1.",
                         stub_text="{}", task_id="TASK-1", prev_session=p, resume_tail="Continue.", keep_transcript=True)
    [task] = seen
    assert task.resume == "" and task.session_mode == "handoff"  # its transcript isn't kept
    assert task.layers.tail.startswith("Implement TASK-1.") and "finish the parser" in task.layers.tail


def test_claude_code_resumes_or_falls_back_and_keeps_the_new_transcript(tmp_path, monkeypatch, settings):
    pytest.importorskip("claude_agent_sdk", reason="the live extra isn't installed")
    from valxel_sdlc.gateway import CallTags, PromptLayers
    from valxel_sdlc.runtime.base import AgentResult, AgentTask
    from valxel_sdlc.runtime.claude_code import ClaudeCodeRuntime

    settings.artifact_dir = tmp_path / "art"
    store = TranscriptStore(settings.artifact_dir)
    old = tmp_path / "old.jsonl"
    old.write_text('{"old": true}\n')
    store.keep("CLN-FEAT-1", "s-old", old)

    class GW:
        def __init__(self):
            self.settings = settings

    rt = ClaudeCodeRuntime(GW(), cwd=str(tmp_path / "work"), plugin_dirs=[])
    calls = []

    async def fake_query(task, model, options, session_mode="fresh"):
        calls.append((getattr(options, "resume", None), task.layers.tail, session_mode))
        if getattr(options, "resume", None) and len(calls) == 1 and fail_resume:
            raise RuntimeError("No conversation found with session ID: s-old")
        home = options.env["HOME"]
        # Claude Code finds the restored transcript and writes the session's own.
        if getattr(options, "resume", None):
            assert (sessions.claude_project_dir(home, options.cwd) / "s-old.jsonl").read_text() == '{"old": true}\n'
        d = sessions.claude_project_dir(home, options.cwd)
        d.mkdir(parents=True, exist_ok=True)
        (d / "s-new.jsonl").write_text('{"new": true}\n')
        return AgentResult(text="{}", tokens=1, cost_usd=0.0, session_id="s-new", runtime="claude_code")

    monkeypatch.setattr(rt, "_query", fake_query)
    layers = PromptLayers(tools="", skills="", constitution="", product_context="", feature_context="", tail="Continue.")
    task = AgentTask(role="coder", phase="build", layers=layers, resume="s-old", fresh_tail="Implement it. Handoff...",
                     keep_transcript=True, session_mode="resumed",
                     tags=CallTags(product_id="CLN", feature_id="CLN-FEAT-1", phase="build", role="coder",
                                   data_class="phi", workspace="w", prompt_version="build@1"))
    fail_resume = False
    res = asyncio.run(rt._run(task, "anthropic/claude-sonnet-5-5"))
    assert res.resumed and calls == [("s-old", "Continue.", "resumed")]
    assert store.has("CLN-FEAT-1", "s-new")

    calls.clear()
    fail_resume = True
    res = asyncio.run(rt._run(task, "anthropic/claude-sonnet-5-5"))
    assert not res.resumed and calls[1] == (None, "Implement it. Handoff...", "handoff")


def test_the_watcher_deletes_old_transcripts(engine, tmp_path):
    store = TranscriptStore(engine.services.settings.artifact_dir)
    src = tmp_path / "t.jsonl"
    src.write_text("{}")
    store.keep("CLN-FEAT-9", "s-old", src)
    store.keep("CLN-FEAT-9", "s-new", src)
    old = thread_folder(engine.services.settings.artifact_dir, "CLN-FEAT-9") / "sessions" / "s-old.jsonl.gz"
    os.utime(old, (time.time() - 2 * 24 * 3600,) * 2)
    assert engine.expire_transcripts() == 1
    assert store.has("CLN-FEAT-9", "s-new") and not store.has("CLN-FEAT-9", "s-old")

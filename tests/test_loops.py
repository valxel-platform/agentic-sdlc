"""Loops that stop making progress (valxel_sdlc.loops), and the loop cap when they don't."""

from valxel_sdlc import loops
from valxel_sdlc.graph.phase import _finish


def test_the_same_findings_reworded_are_a_repeat():
    before = ["AC-2 is not testable: no observable outcome", "Missing non-goals section"]
    now = ["Missing the non-goals section", "AC-2 not testable, no observable outcome"]
    s = loops.stall([before], now)
    assert s["pattern"] == "repeating" and s["between"] == "writer and critic"
    assert "nothing was fixed" in s["detail"]


def test_progress_is_not_a_stall():
    first = ["AC-2 is not testable", "Missing non-goals section", "No constitution articles"]
    assert loops.stall([first], ["AC-2 is not testable"]) is None  # two fixed
    assert loops.stall([first], ["AC-2 is not testable", "A brand new problem with the goals"]) is None
    assert loops.stall([], first) is None and loops.stall([first], []) is None


def test_a_fix_that_brings_back_an_earlier_finding_is_reopened():
    h = [["T1: the review blocks: retries are unbounded"], ["T1: the review blocks: timeout too short"]]
    s = loops.stall(h, ["T1: the review blocks: retries are unbounded"])
    assert s["pattern"] == "reopened" and s["between"] == "coder and reviewer"
    assert s["findings"] == ["T1: the review blocks: retries are unbounded"]


def test_which_agents_were_going_around():
    assert loops.between(["T1: tests failing in farm-api"], "x") == "coder and tests"
    assert loops.between(["acme/farm-api#3: failing: ci"], "build and CI") == "build and CI"


def _state(attempt, history, max_loops=5):
    return {"attempt": attempt, "max_loops": max_loops, "budget_remaining_usd": 10.0, "usage": {},
            "finding_history": history}


def test_finish_stops_a_stalled_loop_and_keeps_each_tries_findings():
    out = _finish(_state(2, [["Missing non-goals section"]]), ["Missing non-goals section"], {})
    assert out["status"] == "escalated" and out["stall"]["pattern"] == "repeating"
    assert out["reason"].startswith("No progress after 2 tries (repeating, writer and critic)")
    assert out["finding_history"] == [["Missing non-goals section"], ["Missing non-goals section"]]


def test_the_cap_still_ends_a_loop_that_keeps_changing():
    history = [[f"Problem number {w}"] for w in ("alpha", "bravo", "charlie", "delta")]
    out = _finish(_state(5, history), ["Problem number echo"], {})
    assert out["status"] == "escalated" and out["reason"] == "5 evaluator loops failed" and "stall" not in out
    assert _finish(_state(3, history[:2]), ["Problem number charlie"], {})["status"] == "retry"


def test_a_pass_ends_it_whatever_came_before():
    assert _finish(_state(3, [["a b c"], ["a b c"]]), [], {})["status"] == "passed"

"""Coder and reviewer evals: cases from merged changes, replayed in a disposable runner. The coder is
scored on the tests people merged and on what it weakens; the reviewer on planted bugs it catches
and merged changes it blocks."""

import json
import subprocess
from pathlib import Path

import pytest

from valxel_sdlc import code_evals, evals
from valxel_sdlc.code_evals import plant


# --------------------------------------------------------------------------- capture and labels

def test_a_merged_feature_leaves_labeled_coder_and_reviewer_cases(engine):
    fid = engine.create(product_id="FARM", title="Harvest log")
    engine.auto_drive(fid)
    st = engine._state(fid)
    merged = [pr for pr in st["pull_requests"].values() if pr.get("state") == "merged"]
    assert merged
    cases = engine.services.evals.cases(phases=code_evals.PHASES)
    assert {c.phase for c in cases} == {"coder", "reviewer"}
    by_task = {pr["task_id"]: pr for pr in merged}
    for c in cases:
        task_id = c.feature_id.split(":", 1)[1]
        assert c.label["decision"] == "merged" and c.label["merge_sha"] == by_task[task_id]["merge_sha"]
        assert c.label["repo"] == by_task[task_id]["repo"] and isinstance(c.label["tests"], list)
        assert c.ctx["task"]["id"] == task_id and c.ctx["build"]["feature_id"] == fid
    [rev] = [c for c in cases if c.phase == "reviewer"][:1]
    assert "Review the change below" in rev.ctx["tail"] and "https://" not in rev.ctx["tail"].split("\n")[0]


def test_only_merged_pull_requests_with_a_task_are_labeled():
    labeled = []

    class Store:
        def label(self, fid, phase, label, reference=""):
            labeled.append((fid, phase, label["tests"]))

    prs = {"a": {"state": "merged", "merge_sha": "m1", "task_id": "T1", "repo": "o/r", "number": 1},
           "b": {"state": "open", "task_id": "T2", "repo": "o/r", "number": 2},
           "c": {"state": "merged", "merge_sha": "m3", "repo": "o/r", "number": 3}}
    files = [{"path": "src/a.py", "status": "modified"}, {"path": "tests/test_a.py", "status": "added"},
             {"path": "tests/test_old.py", "status": "removed"}]
    assert code_evals.label_merged(Store(), "F-1", prs, lambda repo, n: files) == 1
    assert labeled == [("F-1:T1", "coder", ["tests/test_a.py"]), ("F-1:T1", "reviewer", ["tests/test_a.py"])]


# --------------------------------------------------------------------------- planted bugs

DIFF = """diff --git a/src/calc.py b/src/calc.py
--- a/src/calc.py
+++ b/src/calc.py
@@ -1,2 +1,4 @@
 def add(a, b):
-    return 0
+    if a == b:
+        return a * 2
+    return a + b
diff --git a/tests/test_calc.py b/tests/test_calc.py
--- /dev/null
+++ b/tests/test_calc.py
@@ -0,0 +1,3 @@
+def test_add():
+    assert add(1, 2) == 3
+    assert add(2, 2) == 4
"""


def test_a_planted_bug_is_one_of_four_kinds_and_fixed_per_case():
    seen = {}
    for i in range(40):
        out, what = plant(DIFF, f"case-{i}")
        assert out != DIFF and out.count("\n") >= DIFF.count("\n")
        seen[what.split(": ", 1)[1]] = out
    assert set(seen) == {"flipped a condition", "removed an assertion", "skipped a test", "put an API key in the code"}
    assert "+    if a != b:" in seen["flipped a condition"]
    assert '@pytest.mark.skip(reason="flaky")' in seen["skipped a test"]
    assert "sk_live_" in seen["put an API key in the code"]
    assert plant(DIFF, "same") == plant(DIFF, "same")
    assert plant("diff --git a/x b/x\n--- a/x\n+++ b/x\n-gone\n", "s") is None  # nothing added: nothing to plant


# --------------------------------------------------------------------------- replays in a runner

def _git(cwd, *args):
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True)


@pytest.fixture
def merged_repo(tmp_path):
    """A repository whose last commit is a squash-merged change: a fix and the test for it."""
    repo = tmp_path / "origin"
    (repo / "src").mkdir(parents=True)
    (repo / "tests").mkdir()
    _git(tmp_path, "init", "--quiet", str(repo))
    for k, v in (("user.name", "t"), ("user.email", "t@t")):
        _git(repo, "config", k, v)
    (repo / "pyproject.toml").write_text("[tool.pytest.ini_options]\npythonpath = ['src']\n")
    (repo / "src" / "calc.py").write_text("def add(a, b):\n    return 0\n")
    (repo / "tests" / "test_old.py").write_text("def test_truth():\n    assert True\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "--quiet", "-m", "base")
    (repo / "src" / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    (repo / "tests" / "test_calc.py").write_text("from calc import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "--quiet", "-m", "merged")
    sha = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    return repo, sha


def _local_clone(repo):
    def clone(services, name, sha, root):
        dest = Path(root) / "work" / "r"
        subprocess.run(["git", "clone", "--quiet", str(repo), str(dest)], check=True, capture_output=True)
        _git(dest, "checkout", "--quiet", sha)
        for k, v in (("user.name", "t"), ("user.email", "t@t")):
            _git(dest, "config", k, v)
        return str(dest)
    return clone


def _case(phase, sha, tail="Review the change below"):
    task = {"id": "T1", "repo": "acme-farm/farm-api", "acs": ["AC-1"], "role": "provider", "files": []}
    build = {"feature_id": "FARM-FEAT-1", "product_id": "FARM", "title": "Harvest log", "parent_texts": {},
             "tasks": [task], "simulate_faults": {}}
    return evals.EvalCase(id=f"FARM-FEAT-1:T1:{phase}", product_id="FARM", feature_id="FARM-FEAT-1:T1", phase=phase,
                          data_class="general", at=evals.now(), config_version="", output="", status="passed",
                          ctx={"build": build, "task": task, **({"tail": tail} if phase == "reviewer" else {})},
                          label={"decision": "merged", "repo": "acme-farm/farm-api", "merge_sha": sha, "pr": 7,
                                 "tests": ["tests/test_calc.py"]})


class _Res:
    def __init__(self, text):
        self.text, self.cost_usd, self.tokens, self.role = text, 0.0, 0, "x"


def test_code_cases_replay_only_in_a_disposable_runner(engine, merged_repo, monkeypatch):
    monkeypatch.delenv("VALXEL_EVAL_RUNNER", raising=False)
    _, sha = merged_repo
    with pytest.raises(code_evals.NotARunner):
        code_evals.replay_coder(engine.services, _case("coder", sha))
    out = evals.run(engine.services, [_case("coder", sha), _case("reviewer", sha)])
    assert out["results"] == []  # left to the runner, not run in this process


def test_the_coder_passes_when_the_merged_tests_pass_on_its_code(engine, merged_repo, monkeypatch):
    monkeypatch.setenv("VALXEL_EVAL_RUNNER", "1")
    repo, sha = merged_repo
    case = _case("coder", sha)
    seen = {}

    def coder(services, **kw):
        seen["tail"] = kw["tail"]
        Path(kw["box"]["cwd"], "src", "calc.py").write_text("def add(a, b):\n    return a + b\n")
        return _Res('{"summary": "fixed add"}')

    monkeypatch.setattr("valxel_sdlc.graph.phase.call_agent", coder)
    r = code_evals.replay_coder(engine.services, case, clone=_local_clone(repo))
    assert r["passed"] is True and r["weakened"] is False and r["hidden_tests"] == 1
    assert "evaluation run" in seen["tail"] and "push the branch" not in seen["tail"]

    def lazy(services, **kw):  # does nothing: the merged test fails on the old code
        return _Res("{}")

    monkeypatch.setattr("valxel_sdlc.graph.phase.call_agent", lazy)
    r = code_evals.replay_coder(engine.services, case, clone=_local_clone(repo))
    assert r["passed"] is False and "assert 0 == 3" in r["test_output"]


def test_a_coder_that_weakens_a_check_is_flagged(engine, merged_repo, monkeypatch):
    monkeypatch.setenv("VALXEL_EVAL_RUNNER", "1")
    repo, sha = merged_repo

    def cheat(services, **kw):
        cwd = Path(kw["box"]["cwd"])
        (cwd / "src" / "calc.py").write_text("def add(a, b):\n    return a + b\n")
        (cwd / "tests" / "test_old.py").write_text("import pytest\n\n\n@pytest.mark.skip\ndef test_truth():\n    assert True\n")
        return _Res("{}")

    monkeypatch.setattr("valxel_sdlc.graph.phase.call_agent", cheat)
    r = code_evals.replay_coder(engine.services, _case("coder", sha), clone=_local_clone(repo))
    assert r["weakened"] is True and any("skips a test" in w for w in r["weakening"])


def test_tests_run_without_the_tools_secrets(monkeypatch, tmp_path):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-x")
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@h/db")
    env = code_evals.test_env(str(tmp_path))
    assert "ANTHROPIC_API_KEY" not in env and "DATABASE_URL" not in env and env["HOME"] == str(tmp_path)
    assert env["npm_config_ignore_scripts"] == "true"


def test_the_test_command_is_the_one_ci_runs(tmp_path):
    assert code_evals.test_command(tmp_path) is None
    (tmp_path / "package.json").write_text("{}")
    assert code_evals.test_command(tmp_path) == ["npm", "test", "--silent"]
    (tmp_path / "gradlew").write_text("")
    assert code_evals.test_command(tmp_path)[0] == "./gradlew"
    (tmp_path / "pyproject.toml").write_text("")
    assert code_evals.test_command(tmp_path)[:3] == ["python", "-m", "pytest"]
    (tmp_path / "uv.lock").write_text("")
    assert code_evals.test_command(tmp_path)[:3] == ["uv", "run", "pytest"]


def test_the_reviewer_is_scored_on_bugs_caught_and_false_alarms(engine, merged_repo, monkeypatch):
    monkeypatch.setenv("VALXEL_EVAL_RUNNER", "1")
    repo, sha = merged_repo
    case = _case("reviewer", sha)
    clean_diff = {}

    def sharp(services, **kw):  # approves the change as merged, blocks anything else
        diff = kw["tail"].split("<untrusted", 1)[1]
        clean_diff.setdefault("first", diff)
        ok = diff == clean_diff["first"]
        return _Res(json.dumps({"approve": ok, "blocking": [] if ok else ["src/calc.py:2 the planted bug"],
                                "notes": [], "score": 0.9}))

    monkeypatch.setattr("valxel_sdlc.graph.phase.call_agent", sharp)
    r = code_evals.replay_reviewer(engine.services, case, clone=_local_clone(repo))
    assert r["caught"] is True and r["false_alarm"] is False and r["passed"] is True and r["planted"]

    def rubber_stamp(services, **kw):
        return _Res(json.dumps({"approve": True, "blocking": [], "notes": [], "score": 0.9}))

    monkeypatch.setattr("valxel_sdlc.graph.phase.call_agent", rubber_stamp)
    r = code_evals.replay_reviewer(engine.services, case, clone=_local_clone(repo))
    assert r["caught"] is False and r["false_alarm"] is False and r["passed"] is False


# --------------------------------------------------------------------------- the comparison

def _summary(**rates):
    block = {"cases": 4, "errors": 0, "cost_usd": 1.0, "pass_rate": 1.0, "must_met_rate": None,
             "critic_agreement": None, "pairwise_loss_rate": None, **rates}
    return {"summary": {"all": block, "phases": {}}}


def test_regressions_in_code_and_triage_evals_fail_the_comparison():
    ok = evals.compare(_summary(catch_rate=0.9, weakened_rate=0.0, false_alarm_rate=0.1, wrong_action_rate=0.0),
                       _summary(catch_rate=0.88, weakened_rate=0.0, false_alarm_rate=0.12, wrong_action_rate=0.0))
    assert ok["ok"], ok["regressions"]
    bad = evals.compare(_summary(catch_rate=0.9, weakened_rate=0.0, false_alarm_rate=0.1, wrong_action_rate=0.0),
                        _summary(catch_rate=0.7, weakened_rate=0.25, false_alarm_rate=0.3, wrong_action_rate=0.01))
    msgs = " | ".join(bad["regressions"])
    assert "catch rate fell" in msgs and "weakened a check" in msgs and "blocked a change people merged" in msgs
    assert "wrong flag off or rollback" in msgs  # triage's bar is zero: any rise fails

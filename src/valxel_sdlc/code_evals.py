"""Evals of the coder and the reviewer: replayed against changes people merged.

The coder writes the code and the reviewer approves it, so a model or prompt change to either is the
riskiest change to the tool's config. These cases come from the tool's own merged work:

    coder      kept on a task's first attempt: the task, the build context and the instructions
               (graph.phase.coder_tail). When the change set merges, it is labeled with the merge
               commit and the test files the pull request added or changed: the hidden tests.
    reviewer   kept when the reviewer first reviews a task: its instructions, labeled the same way.

No code is kept: a case holds the repository, the merge commit and paths. A replay clones the
repository at run time, with a read-only token for that repository alone, and deletes the clone
when it ends.

**A coder replay** starts from the merge commit's parent (pull requests are squash-merged, so the
parent is what the coder started from), runs the coder with the same instructions, then adds the
hidden tests from the merge commit and runs the repository's own tests (the command its CI runs:
`uv run pytest`, `./gradlew test`, `npm test`), with no secret in their environment. It passes when
they pass; it also reports what the coder's change weakens (evaluators/weakening.py).

**A reviewer replay** reviews the merged change twice: as merged, where it should approve (blocking
it is a false alarm), and with one bug planted in it (a flipped condition, a removed assertion, a
skipped test, a key in the code), where it should block (catching it). The diff is given to the
reviewer inline: the pull request it reviewed is merged.

Both run model-written code or read customer code, so they run only in a disposable runner
(`VALXEL_EVAL_RUNNER=1 valxel eval run --phases coder,reviewer`), never in the API process: the
config gate (evals.ConfigGate) replays the other phases and leaves these to the runner.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import subprocess
import time
from pathlib import Path

log = logging.getLogger(__name__)

PHASES = ("coder", "reviewer")
TEST_TIMEOUT_S = int(os.environ.get("VALXEL_EVAL_TEST_TIMEOUT_S", "1200"))
MAX_DIFF = 60_000
# What a replay keeps of the build context: the rest is per-run state (feedback, sessions, faults).
_DROP = ("feedback", "prior_findings", "simulate_faults", "budget_remaining_usd", "pilot", "sessions",
         "deployments", "quality")


class NotARunner(RuntimeError):
    """Coder and reviewer cases replay only in a disposable runner."""


def runner_allowed(env=None) -> bool:
    return (env if env is not None else os.environ).get("VALXEL_EVAL_RUNNER", "") in ("1", "true", "yes")


def _keep(ctx: dict) -> dict:
    return json.loads(json.dumps({k: v for k, v in ctx.items() if k not in _DROP}, default=str))


def coder_case(state_or_ctx: dict, task: dict, output: str, *, config_version: str = ""):
    from .evals import EvalCase, now

    ctx = state_or_ctx
    return EvalCase(id=f"{ctx['feature_id']}:{task['id']}:coder", product_id=ctx["product_id"],
                    feature_id=f"{ctx['feature_id']}:{task['id']}", phase="coder", data_class=ctx.get("data_class", ""),
                    at=now(), config_version=config_version, ctx={"build": _keep(ctx), "task": dict(task)},
                    output=output, status="passed")


def reviewer_case(ctx: dict, task: dict, tail: str, pr_url: str, output: str, *, config_version: str = ""):
    from .evals import EvalCase, now

    return EvalCase(id=f"{ctx['feature_id']}:{task['id']}:reviewer", product_id=ctx["product_id"],
                    feature_id=f"{ctx['feature_id']}:{task['id']}", phase="reviewer",
                    data_class=ctx.get("data_class", ""), at=now(), config_version=config_version,
                    ctx={"build": _keep(ctx), "task": dict(task),
                         "tail": tail.replace(f"Review {pr_url}", "Review the change below", 1)},
                    output=output, status="passed")


def label_merged(store, feature_id: str, prs: dict, pr_files) -> int:
    """Label a merged change set's coder and reviewer cases: the merge commit and the hidden tests.
    `pr_files(repo, number)` lists a pull request's files. Returns how many tasks were labeled."""
    from .evaluators.weakening import TEST_FILE

    n = 0
    for pr in prs.values():
        if pr.get("state") != "merged" or not pr.get("merge_sha") or not pr.get("task_id"):
            continue
        files = pr_files(pr["repo"], pr["number"]) or []
        tests = sorted(f["path"] for f in files if TEST_FILE.search(f["path"]) and f.get("status") != "removed")
        label = {"decision": "merged", "repo": pr["repo"], "pr": pr["number"], "merge_sha": pr["merge_sha"],
                 "tests": tests}
        for phase in PHASES:
            store.label(f"{feature_id}:{pr['task_id']}", phase, label)
        n += 1
    return n


# --------------------------------------------------------------------------- planted bugs

def _flip(line: str) -> str | None:
    for a, b in ((" == ", " != "), (" != ", " == "), (" >= ", " < "), (" <= ", " > "), (" < ", " >= "),
                 (" > ", " <= "), (" and ", " or "), ("True", "False"), ("true", "false")):
        if a in line:
            return line.replace(a, b, 1)
    return None


def plant(diff: str, seed: str) -> tuple[str, str] | None:
    """The diff with one bug planted in an added line, and what was planted; None if no line fits.
    Which bug is fixed per case (seed), so configs are compared on the same one."""
    from .evaluators.weakening import TEST_FILE

    lines, path, added = diff.splitlines(), "", []
    for i, line in enumerate(lines):
        if line.startswith("+++ "):
            path = line[6:] if line.startswith("+++ b/") else line[4:]
        elif line.startswith("+") and not line.startswith("+++") and line[1:].strip():
            added.append((i, path, line))
    src = [(i, p, ln) for i, p, ln in added if not TEST_FILE.search(p)]
    tests = [(i, p, ln) for i, p, ln in added if TEST_FILE.search(p)]
    kinds = []
    if any(_flip(ln) for _, _, ln in src):
        kinds.append("flip")
    if any(re.search(r"\b(assert|expect\(|assertEquals|assertThat)", ln) for _, _, ln in tests):
        kinds.append("drop_assert")
    if any(re.match(r"\+\s*(def test_|it\(|test\()", ln) for _, _, ln in tests):
        kinds.append("skip_test")
    if src:
        kinds.append("secret")
    if not kinds:
        return None
    kind = kinds[int(hashlib.sha1(seed.encode()).hexdigest(), 16) % len(kinds)]
    out = list(lines)
    if kind == "flip":
        i, p, ln = next((i, p, ln) for i, p, ln in src if _flip(ln))
        out[i] = _flip(ln)
        what = f"{p}: flipped a condition"
    elif kind == "drop_assert":
        i, p, ln = next((i, p, ln) for i, p, ln in tests if re.search(r"\b(assert|expect\(|assertEquals|assertThat)", ln))
        indent = re.match(r"\+(\s*)", ln).group(1)
        out[i] = f"+{indent}pass" if p.endswith(".py") else f"+{indent}// checked elsewhere"
        what = f"{p}: removed an assertion"
    elif kind == "skip_test":
        i, p, ln = next((i, p, ln) for i, p, ln in tests if re.match(r"\+\s*(def test_|it\(|test\()", ln))
        indent = re.match(r"\+(\s*)", ln).group(1)
        if p.endswith(".py"):
            out[i:i] = [f"+{indent}@pytest.mark.skip(reason=\"flaky\")"]
        else:
            out[i] = ln.replace("it(", "it.skip(", 1).replace("test(", "test.skip(", 1)
        what = f"{p}: skipped a test"
    else:
        i, p, _ln = src[0]
        key = "sk_live_" + hashlib.sha1(seed.encode()).hexdigest()[:24]
        out[i + 1:i + 1] = [f'+API_KEY = "{key}"' if p.endswith(".py") else f'+const API_KEY = "{key}";']
        what = f"{p}: put an API key in the code"
    return "\n".join(out) + ("\n" if diff.endswith("\n") else ""), what


# --------------------------------------------------------------------------- running tests

def test_command(cwd: str | Path) -> list[str] | None:
    """The command the repository's CI runs its tests with, by what the repository is built with."""
    p = Path(cwd)
    if (p / "uv.lock").exists():
        return ["uv", "run", "pytest", "-q"]
    if (p / "pyproject.toml").exists() or (p / "pytest.ini").exists():
        return ["python", "-m", "pytest", "-q"]
    if (p / "gradlew").exists():
        return ["./gradlew", "test", "--no-daemon"]
    if (p / "package.json").exists():
        return ["npm", "test", "--silent"]
    return None


def test_env(home: str) -> dict[str, str]:
    """Only what a build needs: no model key, token or tool secret reaches model-written code."""
    from .sandbox import NO_SCRIPTS, SAFE

    env = {k: os.environ[k] for k in SAFE if k in os.environ}
    return {**env, "HOME": home, "TMPDIR": str(Path(home) / "tmp"), "CI": "true", **NO_SCRIPTS}


def _git(cwd: str, *args: str, check: bool = True) -> str:
    return subprocess.run(["git", "-C", cwd, *args], check=check, capture_output=True, text=True, timeout=300).stdout


def weakened(cwd: str, base: str) -> list[str]:
    """What the working tree's change from `base` weakens (evaluators/weakening.py)."""
    from .evaluators.weakening import compare

    out = []
    _git(cwd, "add", "-A")
    for line in _git(cwd, "diff", "--cached", "--name-status", base).splitlines():
        status, *paths = line.split("\t")
        path = paths[-1]
        old = subprocess.run(["git", "-C", cwd, "show", f"{base}:{paths[0]}"], capture_output=True,
                             text=True, timeout=60)
        before = old.stdout if old.returncode == 0 else None
        now = None if status.startswith("D") else (Path(cwd) / path).read_text(errors="replace")
        out += compare(path, before, now)
    return out


# --------------------------------------------------------------------------- replays

def checkout(services, repo: str, sha: str, root: Path) -> str:
    """A clone of `repo` at `sha` with its parent, using a read-only token for that repository."""
    from .sandbox import _askpass

    tokens = getattr(services.github, "tokens", None)
    if tokens is None:  # an eval run's GitHub is a stand-in: read through the App, contents only
        from .signing import github_app_configured, github_app_tokens

        if github_app_configured():
            tokens = github_app_tokens(api=getattr(services.settings, "github_api", "https://api.github.com"),
                                       permissions={"contents": "read"})
    scoped = getattr(tokens, "scoped", None)
    if not callable(scoped):
        raise RuntimeError("replaying code cases needs the GitHub App, to read the repository")
    org, name = repo.split("/", 1)
    askpass = _askpass(root, scoped(org, [name], {"contents": "read"}))
    env = {"PATH": os.environ.get("PATH", ""), "GIT_ASKPASS": str(askpass), "GIT_TERMINAL_PROMPT": "0",
           "HOME": str(root / "home")}
    dest = root / "work" / name
    dest.mkdir(parents=True, exist_ok=True)
    for args in (["init", "--quiet"], ["remote", "add", "origin", f"https://github.com/{repo}.git"],
                 ["fetch", "--quiet", "--depth", "2", "origin", sha], ["checkout", "--quiet", sha]):
        subprocess.run(["git", "-C", str(dest), *args], check=True, env=env, capture_output=True, timeout=600)
    return str(dest)


def _need(case) -> dict:
    lab = case.label or {}
    if lab.get("decision") != "merged" or not lab.get("merge_sha"):
        raise ValueError(f"{case.id} isn't labeled with a merged change")
    return lab


def _result(case, started, before, services, **fields) -> dict:
    rows = services.ledger.records()[before:]
    return {"case_id": case.id, "phase": case.phase, "data_class": case.data_class, "findings": 0,
            "must_met": None, "critic_agrees": None, "pairwise": None,
            "cost_usd": round(sum(r.est_cost_usd for r in rows), 6), "duration_s": round(time.monotonic() - started, 3),
            "trace_ids": sorted({r.trace_id for r in rows if r.trace_id}), **fields}


def replay_coder(services, case, *, clone=checkout, run=subprocess.run) -> dict:
    from . import sandbox
    from .agents import PHASE_SPECS, templates
    from .graph.phase import call_agent, coder_tail, feature_folder, for_task
    from .agents import structured

    if not runner_allowed():
        raise NotARunner("coder cases replay only in a disposable runner (VALXEL_EVAL_RUNNER=1)")
    lab = _need(case)
    started, before = time.monotonic(), len(services.ledger.records())
    ctx, task = case.ctx["build"], case.ctx["task"]
    with sandbox.session(services, clone=False) as box:
        cwd = clone(services, lab["repo"], lab["merge_sha"], Path(box.root))
        base = _git(cwd, "rev-parse", "HEAD^").strip()
        _git(cwd, "checkout", "--quiet", "-B", "valxel-eval", base)
        box.cwd = cwd
        where = feature_folder(box, ctx.get("parent_texts") or {})
        mine = [c for c in ctx.get("cases") or [] if c.get("via") == "test" and c.get("task") == task["id"]]
        tail = (coder_tail(ctx, task, [], mine, False)
                + " This is an evaluation run: work in your current directory and don't push.")
        res = call_agent(services, role="coder", phase="build", ctx=for_task(ctx, task, where),
                         skills=PHASE_SPECS["build"].skills, box=box.spec, task_id=task["id"], tail=tail,
                         stub_text=templates.code_result(ctx, task, passed=True),
                         schema=structured.schema(structured.CoderReply))
        weak = weakened(cwd, base)
        hidden = lab.get("tests") or []
        if hidden:  # the tests people merged, as merged, over whatever the coder wrote
            _git(cwd, "checkout", lab["merge_sha"], "--", *hidden)
        cmd = test_command(cwd)
        if cmd is None:
            return _result(case, started, before, services, passed=None, weakened=bool(weak),
                           weakening=weak, error="no test command for this repository")
        done = run(cmd, cwd=cwd, env=test_env(box.home), capture_output=True, text=True, timeout=TEST_TIMEOUT_S)
        tail_out = ((done.stdout or "") + (done.stderr or ""))[-2000:]
    return _result(case, started, before, services, passed=done.returncode == 0, weakened=bool(weak),
                   weakening=weak, hidden_tests=len(hidden), test_output=tail_out, coder_reply=res.text[:2000])


def replay_reviewer(services, case, *, clone=checkout) -> dict:
    from . import sandbox
    from .agents import structured
    from .agents import PHASE_SPECS, untrusted
    from .graph.phase import call_agent, for_task

    if not runner_allowed():
        raise NotARunner("reviewer cases replay only in a disposable runner (VALXEL_EVAL_RUNNER=1)")
    lab = _need(case)
    started, before = time.monotonic(), len(services.ledger.records())
    ctx, task = case.ctx["build"], case.ctx["task"]
    with sandbox.session(services, clone=False) as box:
        cwd = clone(services, lab["repo"], lab["merge_sha"], Path(box.root))
        diff = _git(cwd, "diff", "HEAD^", "HEAD")[:MAX_DIFF]
    planted = plant(diff, case.id)

    def verdict(text: str) -> bool | None:
        res = call_agent(services, role="reviewer", phase="build", ctx=for_task(ctx, task), max_tokens=1024,
                         skills=PHASE_SPECS["build"].critic_skills,
                         tail=case.ctx["tail"] + "\n\n" + untrusted.wrap("diff", text),
                         stub_text=json.dumps({"approve": True, "blocking": [], "notes": [], "score": 0.9}),
                         schema=structured.schema(structured.ReviewReply))
        reply, _ = structured.parse(res.text, structured.ReviewReply)
        return None if reply is None else bool(reply.approve and not reply.blocking)

    clean = verdict(diff)
    bad = verdict(planted[0]) if planted else None
    caught = None if bad is None else not bad
    false_alarm = None if clean is None else not clean
    return _result(case, started, before, services,
                   passed=None if caught is None or false_alarm is None else caught and not false_alarm,
                   caught=caught, false_alarm=false_alarm, planted=planted[1] if planted else "")

"""Changes that make a check pass by weakening it.

Auto-fix loops are where an agent is tempted to get to green the wrong way: skip or delete a
test, mark it expected to fail, lower a coverage or lint threshold, or silence a finding. AWS's
AI-DLC puts it as "quality targets are inputs, not suggestions". The coder's persona says so; this
check makes it stick: it compares each changed file on the task's branch with the default branch
and reports what was weakened. A finding sends the work back to the coder; when weakening is the
right call, the people at the gate decide (the finding names exactly what changed).
"""

from __future__ import annotations

import re

# Lines that skip, disable or silence a check, by language and tool.
SUPPRESSIONS: dict[str, re.Pattern] = {
    "skips a test": re.compile(
        r"@pytest\.mark\.skip|pytest\.skip\(|@unittest\.skip|\b(?:it|test|describe|context)\.skip\(|"
        r"\bx(?:it|describe|test)\(|@Disabled\b|@Ignore\b|\bt\.Skip(?:Now|f)?\(|#\[ignore\]"),
    "marks a test as expected to fail": re.compile(r"@pytest\.mark\.xfail|\b(?:it|test)\.failing\(|\.todo\("),
    "focuses tests (the rest stop running)": re.compile(r"\b(?:it|test|describe)\.only\(|\bf(?:it|describe)\("),
    "silences a lint, type or security finding": re.compile(
        r"#\s*noqa\b|#\s*type:\s*ignore|#\s*pragma:\s*no\s*cover|eslint-disable|@ts-ignore|@ts-nocheck|"
        r"#\s*nosec\b|//\s*nolint|NOSONAR|@SuppressWarnings|#\s*pylint:\s*disable|// @ts-expect-error"),
}

# Thresholds a change could lower: the key's pattern and where a number follows it.
THRESHOLDS = re.compile(
    r"(fail[_-]under|cov-fail-under|coverageThreshold|minimum[_-]coverage|min[_-]coverage|"
    r"\b(?:branches|functions|lines|statements)\b|threshold|max[_-]?warnings|max[_-]complexity)"
    r"[\"']?\s*[:=]?\s*[\"']?(-?\d+(?:\.\d+)?)", re.I)

TEST_FILE = re.compile(r"(^|/)(tests?|__tests__|spec)/|(_test|\.test|\.spec|Test)\.[a-z]+$|(^|/)test_[^/]+\.py$", re.I)
TEST_CASE = re.compile(r"^\s*(?:async\s+)?def\s+test_|^\s*(?:it|test)\(|@Test\b|^\s*func\s+Test[A-Z]", re.M)
CONFIG_FILE = re.compile(r"(pyproject\.toml|setup\.cfg|\.coveragerc|tox\.ini|jest\.config\.[cm]?[jt]s|"
                         r"vitest\.config\.[cm]?[jt]s|package\.json|\.eslintrc[^/]*|eslint\.config\.[cm]?js|"
                         r"\.golangci\.ya?ml|codecov\.ya?ml|sonar-project\.properties|\.nycrc[^/]*)$")


def _added(old: str, new: str) -> list[str]:
    before = {}
    for line in old.splitlines():
        before[line.strip()] = before.get(line.strip(), 0) + 1
    out = []
    for line in new.splitlines():
        k = line.strip()
        if before.get(k):
            before[k] -= 1
        else:
            out.append(line)
    return out


def _thresholds(text: str) -> dict[str, list[float]]:
    found: dict[str, list[float]] = {}
    for key, num in THRESHOLDS.findall(text):
        found.setdefault(key.lower().replace("-", "_"), []).append(float(num))
    return found


def compare(path: str, old: str | None, new: str | None) -> list[str]:
    """What this file's change weakens, as findings."""
    out: list[str] = []
    if old is not None and new is None:
        if TEST_FILE.search(path) and TEST_CASE.search(old):
            out.append(f"{path}: deletes a test file")
        return out
    if new is None:
        return out
    old = old or ""
    for line in _added(old, new):
        for what, pattern in SUPPRESSIONS.items():
            if pattern.search(line):
                out.append(f"{path}: {what}: `{line.strip()[:120]}`")
                break
    if TEST_FILE.search(path) and old:
        removed = len(TEST_CASE.findall(old)) - len(TEST_CASE.findall(new))
        if removed > 0:
            out.append(f"{path}: removes {removed} test case{'s' if removed > 1 else ''}")
    if CONFIG_FILE.search(path) and old:
        before, after = _thresholds(old), _thresholds(new)
        for key, values in before.items():
            now = after.get(key)
            if now is None:
                out.append(f"{path}: removes the {key} threshold")
                continue
            for b, a in zip(values, now):
                lower_is_weaker = not key.startswith("max")
                if (a < b) if lower_is_weaker else (a > b):
                    out.append(f"{path}: {'lowers' if lower_is_weaker else 'raises'} {key} from {b:g} to {a:g}")
    return out


def findings(github, *, repo: str, branch: str, files: list[str]) -> list[str]:
    """Weakening in a task's changed files, read from the branch and the default branch."""
    out: list[str] = []
    for path in files:
        try:
            new = github.get_file(repo=repo, path=path, ref=branch)
            old = github.get_file(repo=repo, path=path, ref="")
        except Exception:
            continue  # unreadable here; CI and the reviewer still see the diff
        out += compare(path, old, new)
    return out

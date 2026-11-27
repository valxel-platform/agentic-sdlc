"""Changes that make a check pass by weakening it.

Auto-fix loops are where an agent is tempted to get to green the wrong way: skip or delete a
test, mark it expected to fail, lower a coverage or lint threshold, or silence a finding. AWS's
AI-DLC puts it as "quality targets are inputs, not suggestions". The coder's persona says so; this
check makes it stick: it compares each changed file on the task's branch with the default branch
and reports what was weakened. A finding sends the work back to the coder; when weakening is the
right call, the people at the gate decide (the finding names exactly what changed).

A transform's dbt model files (dbt/models/**.yml) have two checks of their own to weaken: a
column's `data_class` lowered (or dropped), which lets more sensitive data reach a mart under a
lesser class, and a `valxel_control_total` removed from a model (pipelines.py, template-transform).
Either goes to the people at the gate, never on the reviewer's say-so.
"""

from __future__ import annotations

import re

import yaml

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


DBT_MODELS = re.compile(r"(^|/)dbt/models/.+\.ya?ml$")
CLASS_RANK = {c: i for i, c in enumerate(("none", "pii", "financial", "phi", "secret"))}


def _dbt(text: str) -> tuple[dict[tuple[str, str], str], dict[str, int]]:
    """(model, column) -> data_class, and model -> control totals, from a dbt .yml."""
    try:
        doc = yaml.safe_load(text or "") or {}
    except yaml.YAMLError:
        return {}, {}
    classes: dict[tuple[str, str], str] = {}
    totals: dict[str, int] = {}
    for m in doc.get("models") or [] if isinstance(doc, dict) else []:
        if not isinstance(m, dict):
            continue
        name = str(m.get("name"))
        for c in m.get("columns") or []:
            if isinstance(c, dict):
                classes[(name, str(c.get("name")))] = str((c.get("meta") or {}).get("data_class") or "")
        tests = m.get("data_tests") or m.get("tests") or []
        totals[name] = sum(1 for t in tests if isinstance(t, dict) and "valxel_control_total" in t)
    return classes, totals


def _dbt_weakening(path: str, old: str, new: str) -> list[str]:
    out = []
    (was, was_totals), (now, now_totals) = _dbt(old), _dbt(new)
    for (model, col), before in sorted(was.items()):
        after = now.get((model, col))
        if after is None and model not in {m for m, _ in now}:
            continue  # the model moved or went: transform-check and the marts check see it
        if before in CLASS_RANK and (after not in CLASS_RANK or CLASS_RANK[after] < CLASS_RANK[before]):
            out.append(f"{path}: lowers {model}.{col}'s data class from {before} to {after or 'nothing'}")
    for model, n in sorted(was_totals.items()):
        if model in now_totals and now_totals[model] < n:
            out.append(f"{path}: removes a control total from {model}")
    return out


def _expert_determination(path: str, old: str, new: str) -> list[str]:
    """Recording an Expert Determination lets tokens count as de-identifying (template-transform):
    a person's decision, never an agent's, so any change to it is flagged for one."""
    def value(text: str) -> str:
        try:
            doc = yaml.safe_load(text or "") or {}
        except yaml.YAMLError:
            return ""
        return str(doc.get("expert_determination") or "").strip() if isinstance(doc, dict) else ""

    before, after = value(old), value(new)
    if after != before and after:
        return [f"{path}: records an expert_determination ({after[:80]}): tokens would count as de-identified;"
                " only a person records one"]
    return []


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
    if DBT_MODELS.search(path) and old:
        out += _dbt_weakening(path, old, new)
    if path.rsplit("/", 1)[-1] == "transform.yaml":
        out += _expert_determination(path, old, new)
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

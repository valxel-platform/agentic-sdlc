#!/usr/bin/env python3
"""The lint and type gate, as a ratchet (engineering review, finding 11).

ruff (pyproject.toml's rules) and mypy run over the code; each finding counts against its file and
rule. config/lint-baseline.json holds what was there when the gate began: a count may stay or fall,
never rise. So a change can't add a finding to any file, and old ones go as the code is touched.

    python scripts/lint_ratchet.py            check: exit 1 and list what's new
    python scripts/lint_ratchet.py --update   write the baseline from the code as it is

--update after removing findings shrinks the baseline (the check says when it can). Raising a count
means editing the baseline by hand in the pull request, where a reviewer sees it.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASELINE = ROOT / "config/lint-baseline.json"
MYPY_LINE = re.compile(r"^(?P<path>[^:]+):(?P<line>\d+):(?:\d+:)? error: (?P<msg>.*?)(?:  \[(?P<code>[\w-]+)\])?$")


def ruff() -> list[dict]:
    out = subprocess.run(["ruff", "check", "--output-format", "json", "--exit-zero", "src", "tests", "scripts"],
                         cwd=ROOT, capture_output=True, text=True, check=True).stdout
    return [{"path": Path(f["filename"]).resolve().relative_to(ROOT).as_posix(), "line": f["location"]["row"],
             "rule": f["code"] or "syntax", "msg": f["message"]} for f in json.loads(out or "[]")]


def mypy() -> list[dict]:
    res = subprocess.run(["mypy", "src/valxel_sdlc", "--no-error-summary", "--show-error-codes",
                          "--no-color-output", "--no-pretty"], cwd=ROOT, capture_output=True, text=True, check=False)
    if res.returncode not in (0, 1):
        sys.exit(f"mypy failed to run:\n{res.stderr or res.stdout}")
    out = []
    for line in res.stdout.splitlines():
        m = MYPY_LINE.match(line)
        if m:
            out.append({"path": m["path"], "line": int(m["line"]), "rule": f"mypy:{m['code'] or 'misc'}",
                        "msg": m["msg"]})
    return out


def counts(findings: list[dict]) -> dict[str, int]:
    return dict(sorted(Counter(f"{f['path']} {f['rule']}" for f in findings).items()))


def main(argv: list[str]) -> int:
    findings = ruff() + mypy()
    now = counts(findings)
    if "--update" in argv:
        BASELINE.write_text(json.dumps(now, indent=1) + "\n")
        print(f"{BASELINE.relative_to(ROOT)}: {sum(now.values())} findings in {len(now)} file and rule pairs")
        return 0
    base = json.loads(BASELINE.read_text()) if BASELINE.exists() else {}
    worse = {k: (base.get(k, 0), n) for k, n in now.items() if n > base.get(k, 0)}
    for key, (was, n) in worse.items():
        path, rule = key.split(" ", 1)
        print(f"{path}: {rule} {was} -> {n}")
        for f in findings:
            if f["path"] == path and f["rule"] == rule:
                print(f"    {path}:{f['line']}: {f['msg']}")
    if worse:
        print(f"\n{len(worse)} file and rule pairs have new findings. Fix them; if one must stay, raise its count "
              f"in {BASELINE.relative_to(ROOT)} in this pull request, where a reviewer sees it.")
        return 1
    fewer = sum(base.values()) - sum(now.values())
    print(f"No new findings ({sum(now.values())} left from the baseline)."
          + (f" {fewer} fewer than the baseline: run with --update to lock that in." if fewer > 0 else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

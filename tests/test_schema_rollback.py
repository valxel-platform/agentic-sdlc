"""Every release can be rolled back to the image before it (ADR 0012).

The tool brings its schema up to date as it starts, so a release's DDL has already run when its
image is reverted. These tests read every SQL statement in src/ and fail on one the previous image
couldn't run against, unless config/schema-contractions.yaml lists it as a deliberate contract step.
"""

import ast
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src" / "valxel_sdlc"

# What the previous image can't live with, each with why.
UNSAFE = [
    (r"\bDROP\s+(TABLE|COLUMN|VIEW|SCHEMA|TYPE)\b", "drops what the previous image reads"),
    (r"\bRENAME\b", "renames what the previous image reads"),
    (r"\bALTER\s+COLUMN\b[^;]*\b(TYPE|SET\s+NOT\s+NULL|SET\s+DATA\s+TYPE)\b", "tightens or retypes a column"),
    (r"\bADD\s+CONSTRAINT\b", "adds a constraint the previous image's writes may break"),
    (r"\bTRUNCATE\s+(TABLE\s+)?valxel_", "empties a table"),
    (r"\bDELETE\s+FROM\s+valxel_\w+\s*;?\s*$", "deletes every row"),
]
# An added column the previous image doesn't write: it needs a default, or to allow NULL.
NOT_NULL_NO_DEFAULT = re.compile(r"\bADD\s+COLUMN\b(?:(?!\bDEFAULT\b)[^,;])*\bNOT\s+NULL\b(?:(?!\bDEFAULT\b)[^,;])*$")
# Run again by an older or newer image, DDL has to be a no-op the second time.
NOT_IDEMPOTENT = [
    (r"\bCREATE\s+TABLE\s+(?!IF\s+NOT\s+EXISTS)", "CREATE TABLE without IF NOT EXISTS"),
    (r"\bADD\s+COLUMN\s+(?!IF\s+NOT\s+EXISTS)", "ADD COLUMN without IF NOT EXISTS"),
    (r"\bCREATE\s+(UNIQUE\s+)?INDEX\s+(?!(CONCURRENTLY\s+)?IF\s+NOT\s+EXISTS)", "CREATE INDEX without IF NOT EXISTS"),
]
DDL = re.compile(r"\b(CREATE|ALTER|DROP|TRUNCATE|DELETE\s+FROM)\b", re.I)


def _strings(path: Path):
    """(line, text) of every string literal in a file, f-strings' literal parts joined."""
    for node in ast.walk(ast.parse(path.read_text(), str(path))):
        if isinstance(node, ast.JoinedStr):
            text = "".join(v.value if isinstance(v, ast.Constant) else "{}" for v in node.values)
            yield node.lineno, text
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            yield node.lineno, node.value


def statements():
    """(file, line, statement) for every SQL statement in src/, upper-cased and on one line."""
    for path in sorted(SRC.rglob("*.py")):
        for line, text in _strings(path):
            if not DDL.search(text) or not re.search(r"\b(TABLE|INDEX|FROM|TRIGGER|FUNCTION|VIEW|TYPE)\b", text, re.I):
                continue
            for stmt in text.split(";"):
                flat = " ".join(stmt.split())
                if DDL.search(flat):
                    yield path.relative_to(ROOT).as_posix(), line, flat


def _listed():
    data = yaml.safe_load((ROOT / "config" / "schema-contractions.yaml").read_text()) or {}
    return {(c["file"], " ".join(c["match"].split()).upper()) for c in data.get("contractions") or []}


def problems(found, listed):
    out = []
    for file, line, stmt in found:
        upper = stmt.upper()
        if (file, upper) in listed:
            continue
        for pattern, why in UNSAFE:
            if re.search(pattern, upper):
                out.append(f"{file}:{line}: {why}: {stmt[:120]}")
        if NOT_NULL_NO_DEFAULT.search(upper):
            out.append(f"{file}:{line}: a NOT NULL column without a default, which the previous image doesn't "
                       f"write: {stmt[:120]}")
        for pattern, why in NOT_IDEMPOTENT:
            if re.search(pattern, upper):
                out.append(f"{file}:{line}: {why}: {stmt[:120]}")
    return out


def test_the_schema_only_expands():
    found = problems(statements(), _listed())
    assert not found, (
        "A release only expands the schema, so its image can be reverted (ADR 0012). Make these additive, "
        "or, if one is the contract step of an earlier expand, list it in config/schema-contractions.yaml "
        "with the release since which nothing reads it:\n  " + "\n  ".join(found))


def test_listed_contractions_are_still_in_the_code():
    present = {(f, s.upper()) for f, _, s in statements()}
    stale = sorted(m for f, m in _listed() if (f, m) not in present)
    assert not stale, ("Remove these from config/schema-contractions.yaml; the code no longer has them:\n  "
                       + "\n  ".join(stale))


def test_it_reads_the_schema():
    # The scan finds the tool's DDL, so passing means something.
    found = list(statements())
    assert len(found) > 30
    assert any("CREATE TABLE IF NOT EXISTS VALXEL_FEATURES" in s.upper() for _, _, s in found)


def test_it_catches_what_the_previous_image_cant_run_against():
    bad = [
        ("a.py", 1, "ALTER TABLE valxel_usage DROP COLUMN trace_id"),
        ("a.py", 2, "ALTER TABLE valxel_usage RENAME COLUMN trace_id TO trace"),
        ("a.py", 3, "ALTER TABLE valxel_usage ALTER COLUMN attempt TYPE INT"),
        ("a.py", 4, "ALTER TABLE valxel_usage ALTER COLUMN attempt SET NOT NULL"),
        ("a.py", 5, "ALTER TABLE valxel_usage ADD COLUMN IF NOT EXISTS who TEXT NOT NULL"),
        ("a.py", 6, "DROP TABLE valxel_costs"),
        ("a.py", 7, "CREATE TABLE valxel_new (id TEXT)"),
        ("a.py", 8, "ALTER TABLE valxel_usage ADD CONSTRAINT one UNIQUE (trace_id)"),
    ]
    assert len(problems(bad, set())) == len(bad)
    good = [
        ("a.py", 1, "ALTER TABLE valxel_usage ADD COLUMN IF NOT EXISTS who TEXT NOT NULL DEFAULT ''"),
        ("a.py", 2, "ALTER TABLE valxel_usage ADD COLUMN IF NOT EXISTS note TEXT"),
        ("a.py", 3, "CREATE TABLE IF NOT EXISTS valxel_new (id TEXT PRIMARY KEY, n INT NOT NULL)"),
        ("a.py", 4, "CREATE INDEX IF NOT EXISTS valxel_new_n ON valxel_new (n)"),
    ]
    assert problems(good, set()) == []
    assert problems(bad[:1], {("a.py", bad[0][2].upper())}) == []

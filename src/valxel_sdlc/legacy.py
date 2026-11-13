"""The product was called Vrixel before it was Valxel. Until every deployment has moved over:

    environment   a VRIXEL_* variable still works when its VALXEL_* name isn't set (with a warning
                  naming the new one); a set VALXEL_* always wins
    database      tables, indexes and sequences named vrixel_* are renamed to valxel_* once, the first
                  time the tool connects, so an existing deployment keeps its data
    signals       alerts labelled vrixel_product still find their product (valxel_sdlc.ops.signals)
    CLI           `vrixel` still runs `valxel`
    GitHub        the valxel/gates status is also posted as vrixel/gates (rulesets that still require
                  it), and each valxel-* repository_dispatch is also sent as vrixel-* (repos created
                  from the templates before the rename listen for that); a repo listens for one
                  name or the other, so each event still runs once

Remove this module once nothing sets the old names.
"""

from __future__ import annotations

import logging
import os

log = logging.getLogger(__name__)
OLD, NEW = "VRIXEL_", "VALXEL_"
OLD_PREFIX, NEW_PREFIX = "vrixel_", "valxel_"


def environment(env: dict | None = None) -> list[str]:
    """Copy each old variable to its new name when that isn't set; returns the names copied."""
    env = os.environ if env is None else env
    copied = []
    for key in [k for k in env if k.startswith(OLD)]:
        new = NEW + key[len(OLD):]
        if new not in env:
            env[new] = env[key]
            copied.append(new)
    if copied:
        log.warning("Deprecated VRIXEL_* settings in use; rename them to %s", ", ".join(sorted(copied)))
    return copied


_FIND = """
    SELECT c.relname AS name, c.relkind AS kind FROM pg_class c
    JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname = current_schema() AND c.relname LIKE 'vrixel\\_%%' AND c.relkind IN ('r', 'p', 'i', 'S')
    ORDER BY CASE c.relkind WHEN 'r' THEN 0 WHEN 'p' THEN 0 WHEN 'S' THEN 1 ELSE 2 END, c.relname
"""
_KIND = {"r": "TABLE", "p": "TABLE", "i": "INDEX", "S": "SEQUENCE"}


def rename_tables(conn) -> list[str]:
    """Rename vrixel_* tables (then their sequences and indexes) to valxel_*, unless the new name
    already exists. Safe to run on every start: it finds nothing once done."""
    done = []
    for row in conn.execute(_FIND).fetchall():
        name, kind = (row["name"], row["kind"]) if isinstance(row, dict) else (row[0], row[1])
        new = NEW_PREFIX + name[len(OLD_PREFIX):]
        exists = conn.execute("SELECT to_regclass(%s) AS r", (new,)).fetchone()
        if (exists["r"] if isinstance(exists, dict) else exists[0]) is not None:
            continue
        conn.execute(f'ALTER {_KIND[kind]} "{name}" RENAME TO "{new}"')
        done.append(f"{name} -> {new}")
    if done:
        log.warning("Renamed %d database objects from vrixel_* to valxel_*", len(done))
    return done


def rename_in(pool) -> list[str]:
    with pool.connection() as conn:
        return rename_tables(conn)


def legacy_name(name: str) -> str:
    """valxel/gates -> vrixel/gates, valxel-deployed -> vrixel-deployed; '' for anything else."""
    for new, old in (("valxel/", "vrixel/"), ("valxel-", "vrixel-")):
        if name.startswith(new):
            return old + name[len(new):]
    return ""

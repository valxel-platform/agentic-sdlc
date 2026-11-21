"""Tables nothing may rewrite: the workspace audit log and the production change log.

Postgres refuses UPDATE, DELETE and TRUNCATE on them, whoever asks, through a trigger installed
with the table. The hash chain each carries is the second line: the owner of the database can
still drop the trigger, and a superuser can do anything, but a row changed or removed after the
fact breaks the chain (`valxel audit verify`). Real immutability is the audit sink's archive
under Object Lock.
"""

from __future__ import annotations

FUNCTION = """CREATE OR REPLACE FUNCTION valxel_append_only() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION '% is append-only: % refused', TG_TABLE_NAME, TG_OP USING ERRCODE = 'insufficient_privilege';
END $$"""


def statements(table: str) -> list[str]:
    """The DDL that makes `table` append-only. Idempotent: safe on every start."""
    if not table.replace("_", "").isalnum():
        raise ValueError(f"not a table name: {table}")
    return [FUNCTION,
            f"""DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = '{table}_append_only'
                 AND tgrelid = '{table}'::regclass) THEN
    CREATE TRIGGER {table}_append_only BEFORE UPDATE OR DELETE ON {table}
      FOR EACH ROW EXECUTE FUNCTION valxel_append_only();
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = '{table}_no_truncate'
                 AND tgrelid = '{table}'::regclass) THEN
    CREATE TRIGGER {table}_no_truncate BEFORE TRUNCATE ON {table}
      FOR EACH STATEMENT EXECUTE FUNCTION valxel_append_only();
  END IF;
END $$"""]

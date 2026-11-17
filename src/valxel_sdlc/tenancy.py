"""Tenant isolation in the database: Postgres row-level security, keyed by workspace.

A workspace is one customer (valxel_sdlc.workspaces). The application already filters by the
person's products; row-level security makes the database refuse what a missed filter would leak.

**The scope.** Every connection the tool takes from its pool states whose rows it may see:
`valxel.workspaces`, the workspaces in scope (joined by the unit separator), or `*` for the
platform's own work. It is set from `scope`, a context variable, when the connection is checked out
(TenantPool), so no store changes:

    a request            the signed-in person's workspaces: those of every product they may see
                         (api/app.py). Requests nobody signed in to (sign-in, health, webhooks that
                         check their signature) do the platform's work, as before.
    a feature's run      its own workspace (engine._execute), whoever started it
    the watcher, jobs,   the platform: `*`
    the CLI, startup

An unset scope matches no row: a path that forgets to say whose work it does fails closed.
`valxel.principal`, the signed-in person, scopes their intake chats, which have no product yet.

**The policies** (`apply`), per table:

    by product      product_id belongs to a workspace in scope (valxel_tenancy, from the registry)
    by thread       the thread (feature, incident, release, initiative, inception) does: the feature
                    index records each thread's workspace. LangGraph's checkpoints are kept this way.
    by workspace    the row's workspace column is in scope
    by owner        intake chats: the person's own
    platform only   signals that matched no product
    none            the claims and provisioning queue (keys and plumbing, no customer content), the
                    tenancy map itself

Each table's row-level security is FORCEd, so the tables' owner, the tool's login, is held to it
too. A superuser or a role with BYPASSRLS is never held to it: strict mode refuses such a login.
VALXEL_TENANCY=rls turns it on (strict mode requires it); `off` leaves the tables open, as before.
"""

from __future__ import annotations

import contextlib
import logging
import os
from contextvars import ContextVar
from typing import Any, Iterable, Iterator

log = logging.getLogger(__name__)

PLATFORM = "*"
SEP = "\x1f"
scope: ContextVar[str | None] = ContextVar("valxel_tenancy_scope", default=None)
principal: ContextVar[str] = ContextVar("valxel_tenancy_principal", default="")


def enforced(env=None) -> bool:
    return (env if env is not None else os.environ).get("VALXEL_TENANCY", "off").lower() == "rls"


def setting(value: str | None = None) -> str:
    """The `valxel.workspaces` value for a scope: '' (nothing), '*' or the workspaces."""
    v = scope.get() if value is None else value
    return v or ""


def _value(workspaces: str | Iterable[str] | None) -> str | None:
    if workspaces is None:
        return None
    if isinstance(workspaces, str):
        return workspaces
    return SEP.join(sorted({w for w in workspaces if w}))


@contextlib.contextmanager
def bound(workspaces: str | Iterable[str] | None, who: str | None = None) -> Iterator[None]:
    """Run the block in a scope: PLATFORM, or these workspaces (none: no rows at all)."""
    token = scope.set(_value(workspaces) or "")
    ptoken = principal.set(who) if who is not None else None
    try:
        yield
    finally:
        scope.reset(token)
        if ptoken is not None:
            principal.reset(ptoken)


def carry(fn):
    """`fn` to run in another thread with the caller's scope (threads don't inherit it)."""
    held, who = scope.get(), principal.get()

    def run(*a, **k):
        with bound(held, who):
            return fn(*a, **k)
    return run


def thread_bound(saver: Any, workspace_of) -> Any:
    """A checkpointer whose calls made with no scope at all run in the workspace of the thread they
    address. LangGraph commits some writes from a worker thread's own context (a done-callback), not
    the run's, so they arrive with no scope; the thread's own workspace is the one _execute runs it
    in. A call made in a scope (a request, a job) keeps it, so another workspace still sees nothing."""
    import functools

    def wrap(name: str, materialize: bool = False):
        orig = getattr(saver, name, None)
        if orig is None:
            return

        @functools.wraps(orig)
        def call(config, *a, **k):
            tid = ((config or {}).get("configurable") or {}).get("thread_id") if isinstance(config, dict) else None
            if scope.get() is not None or not tid:
                return orig(config, *a, **k)
            with bound(workspace_of(tid) or PLATFORM):
                out = orig(config, *a, **k)
                return iter(list(out)) if materialize else out
        setattr(saver, name, call)

    for name in ("put", "put_writes", "get_tuple", "delete_thread"):
        wrap(name)
    wrap("list", materialize=True)
    return saver


def stamp(conn: Any) -> None:
    """Say on this connection whose rows it may see, as the scope says now."""
    conn.execute("select set_config('valxel.workspaces', %s, false), set_config('valxel.principal', %s, false)",
                 (setting(), principal.get()))


def make_pool(dsn: str, **kw):
    """A connection pool whose connections carry the current scope (TenantPool)."""
    from psycopg_pool import ConnectionPool

    class TenantPool(ConnectionPool):
        @contextlib.contextmanager
        def connection(self, timeout: float | None = None):  # type: ignore[override]
            with super().connection(timeout=timeout) as conn:
                stamp(conn)
                yield conn

    return TenantPool(dsn, **kw)


# --------------------------------------------------------------------------- the policies

BY_PRODUCT = ("valxel_usage", "valxel_costs", "valxel_cost_reviews", "valxel_changes", "valxel_change_incidents",
              "valxel_signals", "valxel_alarm_states", "valxel_findings", "valxel_capabilities", "valxel_eval_cases",
              "valxel_effort", "valxel_corrections", "valxel_lesson_proposals", "valxel_inventory_snapshots",
              "valxel_metric_readings", "valxel_metric_readouts")
BY_THREAD = {"checkpoints": "thread_id", "checkpoint_blobs": "thread_id", "checkpoint_writes": "thread_id",
             "valxel_work_items": "feature_id", "valxel_release_joins": "feature_id",
             "valxel_notes": "split_part(key, ':', 2)"}
BY_WORKSPACE = ("valxel_features", "valxel_workspace_rows", "valxel_workspace_audit")
PLATFORM_ONLY = ("valxel_unmatched_signals",)
POLICY = "valxel_tenant"

FUNCTIONS = (
    "create table if not exists valxel_tenancy (product_id text primary key, workspace text not null)",
    "alter table valxel_features add column if not exists workspace text not null default ''",
    """create or replace function valxel_platform() returns boolean language sql stable as
       $$ select coalesce(current_setting('valxel.workspaces', true), '') = '*' $$""",
    r"""create or replace function valxel_scope() returns text[] language sql stable as
       $$ select string_to_array(coalesce(current_setting('valxel.workspaces', true), ''), E'\x1f') $$""",
    """create or replace function valxel_product_visible(p text) returns boolean language sql stable as
       $$ select valxel_platform() or exists (select 1 from valxel_tenancy t
            where t.product_id = p and t.workspace <> '' and t.workspace = any (valxel_scope())) $$""",
    """create or replace function valxel_thread_visible(id text) returns boolean language sql stable as
       $$ select valxel_platform() or exists (select 1 from valxel_features f
            where f.feature_id = id and f.workspace <> '' and f.workspace = any (valxel_scope())) $$""",
    # One table's policy on or off; identifiers quoted by format(%I). Returns 'on', 'off' or 'missing'.
    """create or replace function valxel_apply_policy(tbl text, cond text, enforce boolean) returns text
       language plpgsql as $$
       begin
         if to_regclass(tbl) is null then return 'missing'; end if;
         execute format('drop policy if exists valxel_tenant on %I', tbl);
         if enforce then
           execute format('create policy valxel_tenant on %I using (%s) with check (%s)', tbl, cond, cond);
           execute format('alter table %I enable row level security', tbl);
           execute format('alter table %I force row level security', tbl);
           return 'on';
         end if;
         execute format('alter table %I no force row level security', tbl);
         execute format('alter table %I disable row level security', tbl);
         return 'off';
       end $$""",
)


def _conditions() -> dict[str, str]:
    out = {t: "valxel_product_visible(product_id)" for t in BY_PRODUCT}
    out |= {t: f"valxel_thread_visible({col})" for t, col in BY_THREAD.items()}
    out |= {t: "valxel_platform() or (workspace <> '' and workspace = any (valxel_scope()))" for t in BY_WORKSPACE}
    out |= {t: "valxel_platform()" for t in PLATFORM_ONLY}
    out["valxel_intake"] = "valxel_platform() or owner = coalesce(current_setting('valxel.principal', true), '')"
    out["valxel_change_receipts"] = ("valxel_platform() or exists (select 1 from valxel_changes c "
                                     "where c.id = change_id and valxel_product_visible(c.product_id))")
    return out


def refresh(conn: Any, registry: dict) -> None:
    """The product → workspace map from the registry, and each thread's workspace in the index."""
    for pid, entry in registry.items():
        conn.execute("insert into valxel_tenancy (product_id, workspace) values (%s, %s) on conflict (product_id) "
                     "do update set workspace = excluded.workspace", (pid, getattr(entry, "workspace", "") or ""))
    conn.execute("update valxel_features f set workspace = t.workspace from valxel_tenancy t "
                 "where f.product_id = t.product_id and f.workspace = '' and t.workspace <> ''")


def apply(pool: Any, registry: dict, *, enforce: bool | None = None) -> dict[str, str]:
    """Create the map and functions, and turn each table's policy on (enforce) or off. Runs as the
    tables' owner, in the platform's scope. Returns {table: 'on' | 'off' | 'missing'}."""
    enforce = enforced() if enforce is None else enforce
    out: dict[str, str] = {}
    with bound(PLATFORM), pool.connection() as conn:
        for statement in FUNCTIONS:
            conn.execute(statement)
        refresh(conn, registry)
        # The conditions are this module's constants, never input.
        for table, cond in _conditions().items():
            row = conn.execute("select valxel_apply_policy(%s, %s, %s) as state", (table, cond, enforce)).fetchone()
            out[table] = row["state"] if isinstance(row, dict) else row[0]
    log.info("tenancy: row-level security %s on %d tables", "on" if enforce else "off",
             sum(1 for v in out.values() if v != "missing"))
    return out


def bypasses(conn: Any) -> str:
    """Why this login isn't held to row-level security ('' when it is)."""
    row = conn.execute("select rolsuper, rolbypassrls from pg_roles where rolname = current_user").fetchone()
    sup, byp = (row["rolsuper"], row["rolbypassrls"]) if isinstance(row, dict) else row
    return "a superuser" if sup else "allowed to bypass row-level security (BYPASSRLS)" if byp else ""


def workspaces_of(registry: dict, products: Iterable[str]) -> set[str]:
    return {getattr(registry[p], "workspace", "") for p in products if p in registry} - {""}

"""Running the tool on several replicas.

Pipeline state already lives in Postgres (the checkpoints, the index, the ledgers). What one
process used to keep for itself is here, so any replica can serve any request:

- **One run per thread.** The replica that moves a feature (or incident, release, initiative) on
  holds the thread's Postgres advisory lock while it runs. Another replica asked to move it on at
  the same time refuses (a decision says "still running"), and every replica can tell it is
  running. A replica that dies lets go with its connection; one that loses its lock connection
  stops before the next step.
- **One watcher.** Polling CI and OpenChoreo, the daily inventory and the hourly cost review run on
  the replica that holds the watcher's lock; if it goes, another takes over on its next tick.
- **Asked once.** What the tool has asked GitHub to run (post-deploy and acceptance runs) and when
  it last asked a rollout to step are claimed in a table, so two replicas never both ask.
- **Shared notes.** A thread's last error and work-tracker sync error are kept in a table, so the
  page shows them whichever replica ran it.

The live updates (events.py) and the intake chats (intake.py) have Postgres versions too. Without
a database everything is in memory: one process, as before.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator, MutableMapping
from datetime import datetime, timezone
from typing import Any, Protocol


class Busy(RuntimeError):
    """Another replica is running this thread."""


class LeaseLost(RuntimeError):
    """This replica no longer holds the thread's lock: stop before the next step."""


class Lease(Protocol):
    def alive(self) -> bool: ...
    def release(self) -> None: ...


class Cluster(Protocol):
    def lock(self, thread: str) -> Lease | None: ...
    def busy(self, thread: str) -> bool: ...
    def busy_among(self, threads: list[str]) -> set[str]: ...
    def leader(self, role: str) -> bool: ...
    def claim(self, key: str) -> bool: ...
    def unclaim(self, key: str) -> None: ...
    def get(self, key: str) -> Any: ...
    def put(self, key: str, value: Any) -> None: ...
    def prune(self, days: int = 30) -> int: ...


class Notes(MutableMapping):
    """A dict view of the cluster's shared notes under a prefix (errors per thread)."""

    def __init__(self, cluster: Cluster, prefix: str) -> None:
        self.cluster, self.prefix = cluster, prefix

    def __getitem__(self, k: str) -> Any:
        v = self.cluster.get(f"{self.prefix}:{k}")
        if v is None:
            raise KeyError(k)
        return v

    def __setitem__(self, k: str, v: Any) -> None:
        self.cluster.put(f"{self.prefix}:{k}", v)

    def __delitem__(self, k: str) -> None:
        if self.cluster.get(f"{self.prefix}:{k}") is None:
            raise KeyError(k)
        self.cluster.put(f"{self.prefix}:{k}", None)

    def pop(self, k: str, default: Any = None) -> Any:  # type: ignore[override]
        v = self.cluster.get(f"{self.prefix}:{k}")
        if v is not None:
            self.cluster.put(f"{self.prefix}:{k}", None)
        return default if v is None else v

    def __iter__(self) -> Iterator[str]:
        return iter(())  # not listed; read by key

    def __len__(self) -> int:
        return 0


# --------------------------------------------------------------------------- one process


class _MemoryLease:
    def __init__(self, owner: MemoryCluster, thread: str) -> None:
        self.owner, self.thread, self.done = owner, thread, False

    def alive(self) -> bool:
        return not self.done

    def release(self) -> None:
        if not self.done:
            self.done = True
            with self.owner._guard:
                self.owner._busy.discard(self.thread)


class MemoryCluster:
    def __init__(self) -> None:
        self._busy: set[str] = set()
        self._claims: set[str] = set()
        self._kv: dict[str, Any] = {}
        self._guard = threading.Lock()

    def lock(self, thread: str) -> Lease | None:
        with self._guard:
            if thread in self._busy:
                return None
            self._busy.add(thread)
        return _MemoryLease(self, thread)

    def busy(self, thread: str) -> bool:
        with self._guard:
            return thread in self._busy

    def busy_among(self, threads: list[str]) -> set[str]:
        with self._guard:
            return {t for t in threads if t in self._busy}

    def leader(self, role: str) -> bool:
        return True

    def claim(self, key: str) -> bool:
        with self._guard:
            if key in self._claims:
                return False
            self._claims.add(key)
            return True

    def unclaim(self, key: str) -> None:
        with self._guard:
            self._claims.discard(key)

    def get(self, key: str) -> Any:
        with self._guard:
            return self._kv.get(key)

    def put(self, key: str, value: Any) -> None:
        with self._guard:
            if value is None:
                self._kv.pop(key, None)
            else:
                self._kv[key] = value

    def prune(self, days: int = 30) -> int:
        return 0


# --------------------------------------------------------------------------- Postgres

NAMESPACE = "valxel:thread"
_TRY = "select pg_try_advisory_lock(hashtext(%s), hashtext(%s)) as ok"
_HELD = """
select t from unnest(%s::text[]) as t
 where exists (select 1 from pg_locks
                where locktype = 'advisory' and granted and objsubid = 2
                  and database = (select oid from pg_database where datname = current_database())
                  and classid = (hashtext(%s)::bigint & 4294967295)::oid
                  and objid = (hashtext(t)::bigint & 4294967295)::oid)
"""
SCHEMA = (
    "create table if not exists valxel_claims (key text primary key, at timestamptz not null default now())",
    "create table if not exists valxel_notes (key text primary key, value jsonb not null,"
    " at timestamptz not null default now())",
)


def _first(row: Any) -> Any:
    return next(iter(row.values())) if isinstance(row, dict) else row[0]


class _PostgresLease:
    def __init__(self, conn: Any, namespace: str, thread: str) -> None:
        self.conn, self.namespace, self.thread = conn, namespace, thread

    def alive(self) -> bool:
        try:
            self.conn.execute("select 1").fetchone()
            return True
        except Exception:  # noqa: BLE001 - the session, and the lock with it, may be gone
            return False

    def release(self) -> None:
        try:
            self.conn.execute("select pg_advisory_unlock(hashtext(%s), hashtext(%s))", (self.namespace, self.thread))
        except Exception:  # noqa: BLE001, S110 - closing the session releases it anyway
            pass
        finally:
            self.conn.close()


class PostgresCluster:
    """Advisory locks on connections of their own, and two small tables on the shared pool."""

    def __init__(self, pool: Any, dsn: str, connect: Any = None) -> None:
        import psycopg

        self.pool, self.dsn = pool, dsn
        self.connect = connect or (lambda: psycopg.connect(dsn, autocommit=True, connect_timeout=5))
        self._leads: dict[str, Any] = {}
        self._guard = threading.Lock()
        with pool.connection() as conn:
            for statement in SCHEMA:
                conn.execute(statement)

    def lock(self, thread: str) -> Lease | None:
        conn = self.connect()
        try:
            ok = _first(conn.execute(_TRY, (NAMESPACE, thread)).fetchone())
        except Exception:
            conn.close()
            raise
        if not ok:
            conn.close()
            return None
        return _PostgresLease(conn, NAMESPACE, thread)

    def busy_among(self, threads: list[str]) -> set[str]:
        if not threads:
            return set()
        with self.pool.connection() as conn:
            rows = conn.execute(_HELD, (list(threads), NAMESPACE)).fetchall()
        return {_first(r) for r in rows}

    def busy(self, thread: str) -> bool:
        return thread in self.busy_among([thread])

    def leader(self, role: str) -> bool:
        """Whether this replica holds `role` (the watcher): keeps it while its connection lives,
        takes it when nobody holds it."""
        with self._guard:
            held = self._leads.get(role)
            if held is not None and held.alive():
                return True
            if held is not None:
                held.release()
                self._leads.pop(role, None)
            try:
                lease = self.lock(f"leader:{role}")
            except Exception:  # noqa: BLE001 - the database is unreachable: nobody leads from here
                return False
            if lease is None:
                return False
            self._leads[role] = lease
            return True

    def claim(self, key: str) -> bool:
        with self.pool.connection() as conn:
            row = conn.execute("insert into valxel_claims (key) values (%s) on conflict do nothing returning key",
                               (key,)).fetchone()
        return row is not None

    def unclaim(self, key: str) -> None:
        with self.pool.connection() as conn:
            conn.execute("delete from valxel_claims where key = %s", (key,))

    def get(self, key: str) -> Any:
        with self.pool.connection() as conn:
            row = conn.execute("select value from valxel_notes where key = %s", (key,)).fetchone()
        return _first(row) if row else None

    def put(self, key: str, value: Any) -> None:
        with self.pool.connection() as conn:
            if value is None:
                conn.execute("delete from valxel_notes where key = %s", (key,))
            else:
                conn.execute("insert into valxel_notes (key, value) values (%s, %s) on conflict (key)"
                             " do update set value = excluded.value, at = now()", (key, json.dumps(value)))


    def prune(self, days: int = 30) -> int:
        """Forget claims older than `days` (their waits ended long ago)."""
        with self.pool.connection() as conn:
            cur = conn.execute("delete from valxel_claims where at < now() - make_interval(days => %s)", (days,))
            return int(cur.rowcount or 0)


def stamp() -> str:
    return datetime.now(timezone.utc).isoformat()

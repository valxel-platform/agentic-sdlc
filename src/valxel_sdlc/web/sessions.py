"""Who is still signed in: server-side revocation for stateless session cookies (review finding 5).

A session cookie is signed, so the server keeps nothing per session and can't take one back: a stolen
cookie, or the session of someone just removed, stayed good until it expired (12 hours). This keeps
the one thing that has to be remembered, what was revoked:

- one session: its id (`sid` in the cookie), when its owner signs out;
- everyone's sessions for a login started before a time, when they sign out everywhere, an admin
  signs them out, or an operator does (`valxel sessions revoke <login>`).

`revoked(payload)` is asked on every request. Revocations are kept for a session's lifetime and then
no longer matter; MemorySessions is for one process, PostgresSessions for every replica.
"""

from __future__ import annotations

import math
import threading
import time
from typing import Any

from .auth import SESSION_TTL


def _login(login: str) -> str:
    return (login or "").strip().lower()


class MemorySessions:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.sids: dict[str, float] = {}  # sid -> when revoked
        self.before: dict[str, int] = {}  # login -> sessions started before this are revoked

    def revoke(self, sid: str, login: str = "", by: str = "", reason: str = "") -> None:
        if sid:
            with self._lock:
                self.sids[sid] = time.time()

    def revoke_login(self, login: str, by: str = "", reason: str = "") -> int:
        """Every session of `login` started up to now. Returns the cut-off (epoch seconds)."""
        cut = math.floor(time.time()) + 1  # a session signed in this second too
        with self._lock:
            self.before[_login(login)] = max(self.before.get(_login(login), 0), cut)
        return cut

    def revoked(self, payload: dict) -> bool:
        with self._lock:
            if payload.get("sid") and payload["sid"] in self.sids:
                return True
            return int(payload.get("signed_in_at") or 0) < self.before.get(_login(payload.get("login", "")), 0)


class PostgresSessions:
    """Revocations in Postgres, for every replica. Small: rows older than a session are pruned."""

    DDL = ("create table if not exists valxel_session_revocations (sid text not null default '',"
           " login text not null default '', before bigint not null default 0, by_whom text not null default '',"
           " reason text not null default '', at timestamptz not null default now())",
           "create index if not exists valxel_session_revocations_sid on valxel_session_revocations (sid)",
           "create index if not exists valxel_session_revocations_login on valxel_session_revocations (login)")

    def __init__(self, pool: Any) -> None:
        self.pool = pool
        with pool.connection() as conn:
            for statement in self.DDL:
                conn.execute(statement)

    def _add(self, sid: str, login: str, before: int, by: str, reason: str) -> None:
        with self.pool.connection() as conn:
            conn.execute("insert into valxel_session_revocations (sid, login, before, by_whom, reason)"
                         " values (%s, %s, %s, %s, %s)", (sid, _login(login), before, by, reason[:300]))
            conn.execute("delete from valxel_session_revocations where at < now() - make_interval(secs => %s)",
                         (SESSION_TTL * 2,))

    def revoke(self, sid: str, login: str = "", by: str = "", reason: str = "") -> None:
        if sid:
            self._add(sid, login, 0, by, reason)

    def revoke_login(self, login: str, by: str = "", reason: str = "") -> int:
        cut = math.floor(time.time()) + 1
        self._add("", login, cut, by, reason)
        return cut

    def revoked(self, payload: dict) -> bool:
        with self.pool.connection() as conn:
            row = conn.execute(
                "select exists (select 1 from valxel_session_revocations where sid <> '' and sid = %s)"
                " or exists (select 1 from valxel_session_revocations where login = %s and before > %s)",
                (str(payload.get("sid") or ""), _login(payload.get("login", "")),
                 int(payload.get("signed_in_at") or 0))).fetchone()
        return bool(row[0] if not isinstance(row, dict) else next(iter(row.values())))

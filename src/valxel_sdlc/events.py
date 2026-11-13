"""Live updates: graph runs (in worker threads) publish; server-sent-event streams (on the asyncio
loop) subscribe. `EventBus` is one process; `PostgresBus` carries every event to every replica
through Postgres NOTIFY, so a browser connected to one replica sees work another replica does."""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from datetime import datetime, timezone


class EventBus:
    def __init__(self, max_queue: int = 500) -> None:
        self._subs: set[tuple[asyncio.AbstractEventLoop, asyncio.Queue]] = set()
        self._lock = threading.Lock()
        self._max = max_queue

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=self._max)
        with self._lock:
            self._subs.add((asyncio.get_running_loop(), q))
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        with self._lock:
            self._subs = {(lp, s) for lp, s in self._subs if s is not q}

    def publish(self, event: dict) -> None:
        event = {"at": datetime.now(timezone.utc).isoformat(), **event}
        with self._lock:
            subs = list(self._subs)
        for loop, q in subs:
            try:
                loop.call_soon_threadsafe(self._offer, q, event)
            except RuntimeError:  # loop closed; drop the subscriber
                self.unsubscribe(q)

    @staticmethod
    def _offer(q: asyncio.Queue, event: dict) -> None:
        if q.full():  # a slow client loses its oldest event, never blocks the publisher
            q.get_nowait()
        q.put_nowait(event)


CHANNEL = "valxel_events"
MAX_PAYLOAD = 7000  # NOTIFY carries at most 8000 bytes


class PostgresBus(EventBus):
    """Publishing NOTIFYs; one LISTEN connection per replica hands each event to its own streams
    (this replica's events come back the same way, so each is delivered once)."""

    def __init__(self, pool, dsn: str, max_queue: int = 500, listen: bool = True) -> None:
        super().__init__(max_queue)
        self.pool, self.dsn = pool, dsn
        self.log = logging.getLogger("valxel.events")
        if listen:
            threading.Thread(target=self._listen, name="events-listen", daemon=True).start()

    def publish(self, event: dict) -> None:
        event = {"at": datetime.now(timezone.utc).isoformat(), **event}
        payload = json.dumps(event, separators=(",", ":"), default=str)
        if len(payload.encode()) > MAX_PAYLOAD:  # the essentials; the page reloads the rest
            payload = json.dumps({k: event[k] for k in ("at", "type", "feature_id", "node") if k in event})
        try:
            with self.pool.connection() as conn:
                conn.execute("select pg_notify(%s, %s)", (CHANNEL, payload))
        except Exception:  # the database is unreachable: this replica's own streams still hear it
            super().publish(event)

    def _listen(self) -> None:
        import psycopg

        while True:
            try:
                with psycopg.connect(self.dsn, autocommit=True, connect_timeout=5) as conn:
                    conn.execute(f"listen {CHANNEL}")
                    while True:
                        for n in conn.notifies(timeout=30):
                            try:
                                EventBus.publish(self, json.loads(n.payload))
                            except ValueError:
                                continue
                        conn.execute("select 1")
            except Exception as exc:  # reconnect; pages also reload on their own timer
                self.log.warning("live updates listener reconnecting: %s", type(exc).__name__)
                time.sleep(2)

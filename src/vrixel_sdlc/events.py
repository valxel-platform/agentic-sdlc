"""In-process event bus: graph runs (in worker threads) publish; server-sent-event streams
(on the asyncio loop) subscribe. One process only; run a single API replica or swap this for
Postgres LISTEN/NOTIFY or Redis when scaling out."""

from __future__ import annotations

import asyncio
import threading
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

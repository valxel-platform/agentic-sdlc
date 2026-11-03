"""Feature flags. Every feature deploys to production behind a flag; a launch turns flags on.

The pipeline only needs `set` and `get`. Connect the product's flag service (OpenFeature with
flagd, Unleash, LaunchDarkly, ...) by implementing this protocol; until then the stand-in keeps
flags in memory and the product's code reads them from its own flag provider.
"""

from __future__ import annotations

import re
import threading
from typing import Protocol


def flag_key(product_id: str, feature_id: str, title: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:40]
    return f"{product_id.lower()}.{feature_id.lower()}.{slug}"


class FlagService(Protocol):
    def set(self, product_id: str, key: str, on: bool, *, actor: str) -> None: ...
    def get(self, product_id: str, key: str) -> bool | None: ...


class StubFlags:
    def __init__(self) -> None:
        self.flags: dict[tuple[str, str], bool] = {}
        self.log: list[dict] = []
        self._lock = threading.Lock()

    def set(self, product_id, key, on, *, actor) -> None:
        with self._lock:
            self.flags[(product_id, key)] = bool(on)
            self.log.append({"product_id": product_id, "key": key, "on": bool(on), "actor": actor})

    def get(self, product_id, key):
        return self.flags.get((product_id, key))

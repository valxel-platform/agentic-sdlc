"""Capability catalog: what has been built. In production this is the Backstage catalog in
the OpenChoreo portal, mirrored into the LangGraph Store."""

from __future__ import annotations

import threading
from typing import Protocol


class CapabilityCatalog(Protocol):
    def lookup(self, product_id: str) -> list[dict]: ...
    def register(self, capability: dict) -> dict: ...


class InMemoryCatalog:
    def __init__(self, seed: dict[str, list[dict]] | None = None) -> None:
        self._caps: dict[str, list[dict]] = {k: list(v) for k, v in (seed or {}).items()}
        self._lock = threading.Lock()

    def lookup(self, product_id: str) -> list[dict]:
        with self._lock:
            return [dict(c) for c in self._caps.get(product_id, [])]

    def register(self, capability: dict) -> dict:
        with self._lock:
            caps = self._caps.setdefault(capability["product_id"], [])
            caps[:] = [c for c in caps if c["id"] != capability["id"]] + [dict(capability)]
            return dict(capability)


def default_catalog() -> InMemoryCatalog:
    return InMemoryCatalog(
        {
            "EMR": [{"id": "CAP-EMR-INTAKE", "product_id": "EMR", "name": "Patient intake", "status": "live"}],
            "AG": [{"id": "CAP-AG-FIELDS", "product_id": "AG", "name": "Field management", "status": "live"}],
            "POS": [{"id": "CAP-POS-CHECKOUT", "product_id": "POS", "name": "Checkout", "status": "live"}],
            "MS": [{"id": "CAP-MS-BOOKING", "product_id": "MS", "name": "Booking", "status": "live"}],
            "APX": [{"id": "CAP-APX-LEDGER", "product_id": "APX", "name": "Double-entry ledger", "status": "live"}],
        }
    )

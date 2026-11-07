"""ServiceNow change receipts: every change record, mirrored into the organization's ServiceNow.

A receipt never holds a release up: the change has already gone through the tool's gates (or the
remediation policy), and ServiceNow gets the record afterwards, closed, for its change calendar,
CMDB and auditors. A product turns it on in product.yaml:

    tools:
      change_management:
        provider: servicenow
        instance: https://acme.service-now.com
        assignment_group: Platform Engineering     # a group name (or sys_id)
        types: {standard: normal, emergency: emergency, automatic: emergency}   # ServiceNow change types
        close: {state: "3", done: successful, failed: unsuccessful}             # your state model's values

with VRIXEL_SERVICENOW_AUTH_<PRODUCT> holding the Authorization header ("Basic ..." or
"Bearer ..."). Each record becomes a change_request (Table API) whose correlation_id is the tool's
change id, so a retry finds the one it made instead of making another; then it is closed with the
outcome. When an instance's state model refuses closing it that way, the receipt stays open and
says so. Sends run in order on one background thread and are retried; the ServiceNow number is
kept beside the change log and shown on the Changes page.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

log = logging.getLogger(__name__)
TYPES = {"standard": "normal", "emergency": "emergency", "automatic": "emergency"}
CLOSE = {"state": "3", "done": "successful", "failed": "unsuccessful"}
DISPLAY = "vrixel-sdlc"
RETRY_DELAYS = (2, 10, 30, 60)


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def body_for(rec: dict, spec: dict) -> dict:
    """The change_request fields for a change record."""
    types = {**TYPES, **(spec.get("types") or {})}
    lines = [f"Vrixel SDLC change {rec['id']} ({rec['type']}, {rec['kind']} {rec['action']}) in {rec['product_id']} "
             f"{rec['env']} at {rec['at']}.",
             "", "What changed:", *[f"- {t.get('kind')} {t.get('name')}"
                                   + (f" release {t['release']}" if t.get("release") else "")
                                   + (f" {'on' if t.get('on') else 'off'}" if "on" in t else "")
                                   + (f" for {t['percent']:g}% of users" if t.get("percent") not in (None, 100.0) else "")
                                   for t in rec.get("targets") or []],
             "", f"Requested by: {rec.get('requested_by') or '-'}",
             f"Approved by: {', '.join(rec.get('approved_by') or []) or '-'}",
             *[f"- {a.get('gate', '').upper()} {a.get('approver')} at {a.get('at')}" for a in rec.get("approvals") or []]]
    prs = (rec.get("evidence") or {}).get("pull_requests") or []
    if prs:
        lines += ["", "Pull requests:", *[f"- {u}" for u in prs]]
    for k in ("feature_id", "release_id", "incident_id"):
        if rec.get(k):
            lines.append(f"{k.replace('_id', '').title()}: {rec[k]}")
    lines += ["", f"Record hash {rec.get('hash', '')} (chained after {rec.get('prev_hash', '')[:16]})"]
    out = {"short_description": f"[{rec['type']}] {rec['summary']}"[:160], "description": "\n".join(lines)[:4000],
           "type": types.get(rec["type"], "normal"), "correlation_id": f"vrixel:{rec['id']}",
           "correlation_display": DISPLAY, "justification": f"Recorded by the Vrixel SDLC tool ({rec['type']} change)"}
    if spec.get("assignment_group"):
        out["assignment_group"] = spec["assignment_group"]
    if spec.get("cmdb_ci"):
        out["cmdb_ci"] = spec["cmdb_ci"]
    return out


class InMemoryReceipts:
    def __init__(self) -> None:
        self.rows: dict[str, dict] = {}
        self._lock = threading.Lock()

    def put(self, change_id: str, receipt: dict) -> None:
        with self._lock:
            self.rows[change_id] = {**receipt, "change_id": change_id, "at": now()}

    def get(self, change_ids: list[str]) -> dict[str, dict]:
        with self._lock:
            return {c: dict(self.rows[c]) for c in change_ids if c in self.rows}


class PostgresReceipts:
    DDL = ("""CREATE TABLE IF NOT EXISTS vrixel_change_receipts (
                change_id TEXT PRIMARY KEY, data JSONB NOT NULL, at TIMESTAMPTZ NOT NULL DEFAULT now())""",)

    def __init__(self, pool) -> None:
        self.pool = pool
        with self.pool.connection() as conn:
            for s in self.DDL:
                conn.execute(s)

    def put(self, change_id: str, receipt: dict) -> None:
        with self.pool.connection() as conn:
            conn.execute("INSERT INTO vrixel_change_receipts (change_id, data) VALUES (%s, %s::jsonb) "
                         "ON CONFLICT (change_id) DO UPDATE SET data = EXCLUDED.data, at = now()",
                         (change_id, json.dumps({**receipt, "change_id": change_id, "at": now()})))

    def get(self, change_ids: list[str]) -> dict[str, dict]:
        if not change_ids:
            return {}
        with self.pool.connection() as conn:
            rows = conn.execute("SELECT change_id, data FROM vrixel_change_receipts WHERE change_id = ANY(%s)",
                                (list(change_ids),)).fetchall()
        return {r["change_id"]: dict(r["data"]) for r in rows}


class ServiceNowReceipts:
    """Sends receipts for products with tools.change_management (provider servicenow)."""

    def __init__(self, spec_for, store, *, env=None, opener=None, delays=RETRY_DELAYS) -> None:
        from urllib.request import build_opener

        self.spec_for = spec_for  # product id -> the product's tools.change_management, or None
        self.store = store
        self.env = env if env is not None else os.environ
        self.opener = opener or build_opener()
        self.delays = delays
        self._queue = ThreadPoolExecutor(max_workers=1, thread_name_prefix="servicenow")

    def wants(self, product_id: str) -> dict | None:
        try:
            spec = self.spec_for(product_id)
        except Exception:
            return None
        return spec if spec and spec.get("provider") == "servicenow" else None

    def send(self, rec: dict) -> None:
        spec = self.wants(rec["product_id"])
        if spec is None:
            return
        auth = self.env.get(f"VRIXEL_SERVICENOW_AUTH_{rec['product_id'].upper()}", "")
        if not auth:
            self.store.put(rec["id"], {"status": "not_sent", "detail": f"set VRIXEL_SERVICENOW_AUTH_{rec['product_id'].upper()}"})
            return
        self.store.put(rec["id"], {"status": "queued", "instance": spec["instance"]})
        self._queue.submit(self._deliver, rec, spec, auth)

    def flush(self, timeout: float = 30.0) -> None:
        self._queue.submit(lambda: None).result(timeout=timeout)

    def _http(self, method: str, url: str, auth: str, body: dict | None = None) -> dict:
        from urllib.request import Request

        req = Request(url, method=method, data=json.dumps(body).encode() if body is not None else None,
                      headers={"Accept": "application/json", "Authorization": auth,
                               **({"Content-Type": "application/json"} if body is not None else {})})
        with self.opener.open(req, timeout=20) as resp:
            return json.loads(resp.read().decode("utf-8") or "{}")

    def _deliver(self, rec: dict, spec: dict, auth: str) -> None:
        from urllib.error import HTTPError

        base = spec["instance"].rstrip("/") + "/api/now/table/change_request"
        close = {**CLOSE, **(spec.get("close") or {})}
        last = ""
        for attempt in range(len(self.delays) + 1):
            if attempt:
                time.sleep(self.delays[attempt - 1])
            try:
                q = urllib.parse.urlencode({"sysparm_query": f"correlation_id=vrixel:{rec['id']}^correlation_display={DISPLAY}",
                                            "sysparm_fields": "sys_id,number,state", "sysparm_limit": "1"})
                found = (self._http("GET", f"{base}?{q}", auth).get("result") or [])
                if found:
                    made = found[0]
                else:
                    made = self._http("POST", f"{base}?sysparm_input_display_value=true", auth,
                                      body_for(rec, spec)).get("result") or {}
                sys_id, number = str(made.get("sys_id", "")), str(made.get("number", ""))
                if not sys_id:
                    raise ValueError("ServiceNow didn't return the change it made")
                try:
                    self._http("PATCH", f"{base}/{urllib.parse.quote(sys_id)}?sysparm_input_display_value=false", auth, {
                        "state": close["state"], "close_code": close["done"] if rec.get("outcome") == "done" else close["failed"],
                        "close_notes": f"Recorded by the Vrixel SDLC tool: {rec['summary']}"[:1000]})
                    self.store.put(rec["id"], {"status": "closed", "number": number, "sys_id": sys_id,
                                               "instance": spec["instance"]})
                except Exception as exc:  # the instance's state model may not allow closing it this way
                    self.store.put(rec["id"], {"status": "open", "number": number, "sys_id": sys_id,
                                               "instance": spec["instance"],
                                               "detail": f"created, not closed: {type(exc).__name__}: {exc}"[:300]})
                return
            except HTTPError as exc:
                last = f"HTTP {exc.code}"
                if exc.code < 500 and exc.code != 429:
                    break
            except Exception as exc:
                last = f"{type(exc).__name__}: {exc}"
        self.store.put(rec["id"], {"status": "failed", "instance": spec["instance"], "detail": last[:300]})
        log.error("ServiceNow receipt for %s failed: %s", rec["id"], last)


class WithReceipts:
    """A change log that sends each new record as a receipt. Everything else is the log's."""

    def __init__(self, log_, receipts: ServiceNowReceipts) -> None:
        self._log = log_
        self.receipts = receipts

    def record(self, rec: dict) -> dict:
        stored = self._log.record(rec)
        if self.receipts.wants(rec["product_id"]) and not self.receipts.store.get([stored["id"]]):
            try:
                self.receipts.send(stored)
            except Exception as exc:
                log.error("ServiceNow receipt for %s not queued: %s", stored["id"], exc)
        return stored

    def __getattr__(self, name):
        return getattr(self._log, name)

"""Paging on-call: every incident opens a PagerDuty alert, and the tool resolves it.

PagerDuty owns who is on call, how they're notified and how a page escalates when nobody
acknowledges; the tool tells it when an incident opens and when it's over. Each product has its
own PagerDuty service, reached with that service's Events API v2 integration key
(VRIXEL_PAGERDUTY_ROUTING_KEYS='{"EMR": "<key>", "APX": "<key>"}'):

    incident opens                 trigger      (dedup key: the incident id)
    on-call's first decision       acknowledge  (in the tool: approve, another look, resolve)
    mitigated, resolved or merged  resolve

Severity decides urgency: set each service's urgency to "based on alert severity", so SEV1 and
SEV2 wake someone up and SEV3, SEV4 and cost findings wait for working hours.

    sev1 -> critical   sev2 -> error   sev3 -> warning   sev4, cost -> info

A page says which incident, how bad and where, and links to the incident in the tool. It never
carries signal details or logs, and for PHI and payments products not even the title: PagerDuty
is outside the products' data boundary, and an alert's title can quote an exception message.

Sends go out in order on one background thread, so a slow or failing PagerDuty never holds up a
webhook. Throttling, server errors and network failures are retried with backoff; what happened
to each send is kept per incident and shown on the incident page.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections import defaultdict
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Protocol

log = logging.getLogger(__name__)

EVENTS_URLS = {"us": "https://events.pagerduty.com/v2/enqueue", "eu": "https://events.eu.pagerduty.com/v2/enqueue"}
SEVERITY = {"sev1": "critical", "sev2": "error", "sev3": "warning", "sev4": "info"}
PRIVATE = ("phi", "payments")  # data classes whose incident titles never leave the tool
RETRY_DELAYS = (2, 5, 15, 30, 60)


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Pager(Protocol):
    def trigger(self, incident: dict) -> str: ...  # what the incident's record says about the page
    def acknowledge(self, incident_id: str, product_id: str, by: str) -> None: ...
    def resolve(self, incident_id: str, product_id: str, note: str = "") -> None: ...
    def history(self, incident_id: str) -> list[dict]: ...


def page_payload(incident: dict, routing_key: str, public_url: str) -> dict:
    """The Events API v2 trigger for an incident: {incident_id, product_id, title, severity,
    component, env, source, category, data_class}."""
    iid, product = incident["incident_id"], incident["product_id"]
    sev = str(incident.get("severity") or "sev3")
    component = str(incident.get("component") or "").rsplit("/", 1)[-1]
    where = f" on {component}" if component else ""
    if incident.get("data_class") in PRIVATE:
        summary = f"{iid} {sev.upper()}{where}: open the incident in the SDLC tool"
    else:
        summary = f"{iid} {sev.upper()}{where}: {incident.get('title') or 'incident'}"
    url = f"{public_url.rstrip('/')}/incidents/{iid}"
    return {
        "routing_key": routing_key, "event_action": "trigger", "dedup_key": iid,
        "payload": {
            "summary": summary[:1024], "source": f"vrixel-sdlc/{product}",
            "severity": "info" if incident.get("category") == "cost" else SEVERITY.get(sev, "warning"),
            "component": component or None, "group": product,
            "class": "cost" if incident.get("category") == "cost" else str(incident.get("source") or "") or None,
            "custom_details": {"incident": iid, "product": product, "severity": sev,
                               "environment": incident.get("env") or "production", "open in the tool": url},
        },
        "links": [{"href": url, "text": f"{iid} in the SDLC tool"}],
        "client": "Vrixel SDLC", "client_url": url,
    }


class RecordingPager:
    """Keeps what would be sent: stub mode, tests, and products without a PagerDuty service."""

    def __init__(self, routing_keys: dict[str, str] | None = None, public_url: str = "http://localhost:8080") -> None:
        self.routing_keys = dict(routing_keys or {})
        self.public_url = public_url
        self.sent: list[dict] = []
        self._history: dict[str, list[dict]] = defaultdict(list)
        self._lock = threading.Lock()

    def _note(self, incident_id: str, action: str, ok: bool, detail: str) -> None:
        with self._lock:
            self._history[incident_id].append({"at": now(), "action": action, "ok": ok, "detail": detail})

    def history(self, incident_id: str) -> list[dict]:
        with self._lock:
            return [dict(h) for h in self._history.get(incident_id, [])]

    def _send(self, incident_id: str, action: str, body: dict) -> None:
        with self._lock:
            self.sent.append(body)
        self._note(incident_id, action, True, "recorded (PagerDuty isn't called in this mode)")

    def trigger(self, incident: dict) -> str:
        key = self.routing_keys.get(incident["product_id"], "")
        if not key:
            msg = f"not paged: {incident['product_id']} has no PagerDuty service (VRIXEL_PAGERDUTY_ROUTING_KEYS)"
            self._note(incident["incident_id"], "trigger", False, msg)
            log.error("%s %s", incident["incident_id"], msg)
            return msg
        self._send(incident["incident_id"], "trigger", page_payload(incident, key, self.public_url))
        return f"paging {incident['product_id']}'s on-call through PagerDuty"

    def _follow_up(self, incident_id: str, product_id: str, action: str) -> None:
        key = self.routing_keys.get(product_id, "")
        if key:
            self._send(incident_id, action, {"routing_key": key, "event_action": action, "dedup_key": incident_id})

    def acknowledge(self, incident_id: str, product_id: str, by: str) -> None:
        if not any(h["action"] == "acknowledge" for h in self.history(incident_id)):
            self._follow_up(incident_id, product_id, "acknowledge")

    def resolve(self, incident_id: str, product_id: str, note: str = "") -> None:
        if not any(h["action"] == "resolve" for h in self.history(incident_id)):
            self._follow_up(incident_id, product_id, "resolve")


class PagerDutyPager(RecordingPager):
    """Sends to PagerDuty's Events API v2, in order, on one background thread."""

    def __init__(self, routing_keys: dict[str, str], public_url: str, region: str = "us", opener=None,
                 delays: tuple[float, ...] = RETRY_DELAYS) -> None:
        super().__init__(routing_keys, public_url)
        if region not in EVENTS_URLS:
            raise ValueError(f"VRIXEL_PAGERDUTY_REGION must be one of {sorted(EVENTS_URLS)}")
        self.url = EVENTS_URLS[region]
        self.opener = opener
        self.delays = delays
        self._queue = ThreadPoolExecutor(max_workers=1, thread_name_prefix="pagerduty")

    def _send(self, incident_id: str, action: str, body: dict) -> Future:  # type: ignore[override]
        return self._queue.submit(self._deliver, incident_id, action, body)

    def _deliver(self, incident_id: str, action: str, body: dict) -> None:
        from urllib.error import HTTPError
        from urllib.request import Request, build_opener

        data = json.dumps(body).encode()
        last = ""
        for attempt in range(len(self.delays) + 1):
            if attempt:
                time.sleep(self.delays[attempt - 1])
            req = Request(self.url, data=data, method="POST", headers={"Content-Type": "application/json"})
            try:
                with (self.opener or build_opener()).open(req, timeout=10) as resp:
                    status = resp.status
                    text = resp.read().decode("utf-8", "replace")[:300]
            except HTTPError as exc:
                status, text = exc.code, exc.read().decode("utf-8", "replace")[:300]
            except Exception as exc:  # network: try again
                status, text = 0, f"{type(exc).__name__}: {exc}"
            if 200 <= status < 300:
                self._note(incident_id, action, True, f"PagerDuty accepted it (attempt {attempt + 1})")
                return
            last = f"HTTP {status}: {text}" if status else text
            if status and status != 429 and status < 500:  # a bad key or payload: retrying won't help
                break
        self._note(incident_id, action, False, f"PagerDuty refused or was unreachable: {last}")
        log.error("PagerDuty %s for %s failed: %s", action, incident_id, last)

    def flush(self, timeout: float = 30.0) -> None:
        """Wait for everything queued so far (tests, shutdown)."""
        self._queue.submit(lambda: None).result(timeout=timeout)


def from_env(public_url: str, opener=None) -> RecordingPager:
    """PagerDuty when VRIXEL_PAGERDUTY_ROUTING_KEYS is set; otherwise a pager that records only."""
    raw = os.environ.get("VRIXEL_PAGERDUTY_ROUTING_KEYS") or ""
    try:
        keys = {str(k): str(v) for k, v in (json.loads(raw) if raw.strip() else {}).items() if v}
    except (ValueError, AttributeError) as exc:
        raise ValueError("VRIXEL_PAGERDUTY_ROUTING_KEYS must be JSON: {\"<product>\": \"<integration key>\"}") from exc
    if not keys:
        return RecordingPager({}, public_url)
    return PagerDutyPager(keys, public_url, region=(os.environ.get("VRIXEL_PAGERDUTY_REGION") or "us").lower(),
                          opener=opener)

"""Paging on-call through PagerDuty, for what the tool can't handle by itself.

The tool works an incident on its own first (ops/remediation.py). PagerDuty is how a person hears
about it, in two ways, each its own alert on the product's PagerDuty service:

    heads-up   (dedup key: the incident id, severity info: low urgency)
               a SEV1 as it opens, while the tool mitigates it: on-call knows, nobody need act
    urgent     (dedup key: <incident id>:urgent, severity critical for SEV1, else error: high urgency)
               the tool can't fix it: no action it may take fits, its mitigation failed, or the
               failure persists after it. Cost findings and requests to change what was built
               never page; they wait in the tool for working hours.

On-call's first decision in the tool acknowledges what was sent; the incident reaching its
postmortem (or being merged into another) resolves it. Set each service's urgency to "based on
alert severity". Each product's service is reached with its Events API v2 integration key
(VRIXEL_PAGERDUTY_ROUTING_KEYS='{"CLN": "<key>", "PAY": "<key>"}').

A page says which incident, how bad, where and why on-call is needed, and links to the incident
in the tool. It never carries signal details or logs, and for PHI and payments products not even
the title: PagerDuty is outside the products' data boundary, and an alert's title can quote an
exception message.

Sends go out in order on one background thread, so a slow or failing PagerDuty never holds up a
webhook or the incident's work. Throttling, server errors and network failures are retried with
backoff; what happened to each send is kept per incident and shown on the incident page.
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
PRIVATE = ("phi", "payments")  # data classes whose incident titles never leave the tool
RETRY_DELAYS = (2, 5, 15, 30, 60)


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Pager(Protocol):
    def trigger(self, incident: dict, urgent: bool = False) -> str: ...  # what the incident's record says
    def acknowledge(self, incident_id: str, product_id: str, by: str) -> None: ...
    def resolve(self, incident_id: str, product_id: str, note: str = "") -> None: ...
    def history(self, incident_id: str) -> list[dict]: ...


def dedup_key(incident_id: str, urgent: bool) -> str:
    return f"{incident_id}:urgent" if urgent else incident_id


def page_payload(incident: dict, routing_key: str, public_url: str, urgent: bool = False) -> dict:
    """The Events API v2 trigger for an incident: {incident_id, product_id, title, severity,
    component, env, source, data_class, why}. `why` is the tool's own words (never a signal's)."""
    iid, product = incident["incident_id"], incident["product_id"]
    sev = str(incident.get("severity") or "sev3")
    component = str(incident.get("component") or "").rsplit("/", 1)[-1]
    where = f" on {component}" if component else ""
    what = "" if incident.get("data_class") in PRIVATE else f": {incident.get('title') or 'incident'}"
    if urgent:
        summary = f"{iid} {sev.upper()}{where} needs on-call ({incident.get('why') or 'the tool could not fix it'}){what}"
    else:
        summary = f"{iid} {sev.upper()}{where}, the SDLC tool is mitigating it (no action needed){what}"
    url = f"{public_url.rstrip('/')}/incidents/{iid}"
    details = {"incident": iid, "product": product, "severity": sev,
               "environment": incident.get("env") or "production", "open in the tool": url}
    if urgent and incident.get("why"):
        details["why on-call"] = incident["why"]
    return {
        "routing_key": routing_key, "event_action": "trigger", "dedup_key": dedup_key(iid, urgent),
        "payload": {
            "summary": summary[:1024], "source": f"vrixel-sdlc/{product}",
            "severity": ("critical" if sev == "sev1" else "error") if urgent else "info",
            "component": component or None, "group": product,
            "class": str(incident.get("source") or "") or None, "custom_details": details,
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
        self._attempted: set[tuple[str, str, str]] = set()  # (incident, action, dedup key), as queued
        self._lock = threading.Lock()

    def _note(self, incident_id: str, action: str, ok: bool, detail: str, key: str = "") -> None:
        with self._lock:
            self._history[incident_id].append({"at": now(), "action": action, "ok": ok, "detail": detail,
                                               "dedup_key": key or incident_id})

    def history(self, incident_id: str) -> list[dict]:
        with self._lock:
            return [dict(h) for h in self._history.get(incident_id, [])]

    def _sent_keys(self, incident_id: str, action: str) -> set[str]:
        with self._lock:
            return {k for i, a, k in self._attempted if i == incident_id and a == action}

    def _dispatch(self, incident_id: str, action: str, body: dict) -> None:
        with self._lock:
            self._attempted.add((incident_id, action, body["dedup_key"]))
        self._send(incident_id, action, body)

    def _send(self, incident_id: str, action: str, body: dict) -> None:
        with self._lock:
            self.sent.append(body)
        self._note(incident_id, action, True, "recorded (PagerDuty isn't called in this mode)", body["dedup_key"])

    def trigger(self, incident: dict, urgent: bool = False) -> str:
        iid, action = incident["incident_id"], "urgent" if urgent else "heads_up"
        key = self.routing_keys.get(incident["product_id"], "")
        if not key:
            msg = f"not paged: {incident['product_id']} has no PagerDuty service (VRIXEL_PAGERDUTY_ROUTING_KEYS)"
            self._note(iid, action, False, msg, dedup_key(iid, urgent))
            log.error("%s %s", iid, msg)
            return msg
        if dedup_key(iid, urgent) in self._sent_keys(iid, action):
            return "on-call was already paged"
        self._dispatch(iid, action, page_payload(incident, key, self.public_url, urgent))
        return (f"paged {incident['product_id']}'s on-call urgently through PagerDuty" if urgent else
                f"told {incident['product_id']}'s on-call through PagerDuty (low urgency) while the tool mitigates it")

    def _follow_up(self, incident_id: str, product_id: str, action: str) -> None:
        key = self.routing_keys.get(product_id, "")
        if not key:
            return
        triggered = self._sent_keys(incident_id, "heads_up") | self._sent_keys(incident_id, "urgent")
        for dk in sorted(triggered - self._sent_keys(incident_id, action)):
            self._dispatch(incident_id, action, {"routing_key": key, "event_action": action, "dedup_key": dk})

    def acknowledge(self, incident_id: str, product_id: str, by: str) -> None:
        self._follow_up(incident_id, product_id, "acknowledge")

    def resolve(self, incident_id: str, product_id: str, note: str = "") -> None:
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
                self._note(incident_id, action, True, f"PagerDuty accepted it (attempt {attempt + 1})",
                           body["dedup_key"])
                return
            last = f"HTTP {status}: {text}" if status else text
            if status and status != 429 and status < 500:  # a bad key or payload: retrying won't help
                break
        self._note(incident_id, action, False, f"PagerDuty refused or was unreachable: {last}", body["dedup_key"])
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

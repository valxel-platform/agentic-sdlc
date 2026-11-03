"""Production signals: Prometheus alerts (Alertmanager), Sentry issues and support tickets, turned
into one shape and tied to a product and a component.

    {source, status: firing|resolved, product_id, component, env, severity: sev1..sev4,
     title, detail, fingerprint, url, at, labels}

Signals are stored outside the incident threads (they keep arriving while an incident waits for a
person), in the platform database when there is one.
"""

from __future__ import annotations

import hashlib
import json
import threading
from datetime import datetime, timezone
from typing import Protocol

SEVERITIES = ("sev1", "sev2", "sev3", "sev4")
_ALERT_SEV = {"critical": "sev1", "page": "sev1", "high": "sev2", "error": "sev2", "major": "sev2",
              "warning": "sev3", "warn": "sev3", "medium": "sev3", "info": "sev4", "low": "sev4", "none": "sev4"}
_SENTRY_SEV = {"fatal": "sev1", "error": "sev2", "warning": "sev3", "info": "sev4", "debug": "sev4"}


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sev(raw: str, table: dict[str, str], default: str) -> str:
    raw = (raw or "").strip().lower()
    return raw if raw in SEVERITIES else table.get(raw, default)


def from_alertmanager(payload: dict) -> list[dict]:
    """Alertmanager webhook (version 4): one signal per alert in the group."""
    out = []
    common = payload.get("commonLabels") or {}
    for a in payload.get("alerts") or []:
        labels = {**common, **(a.get("labels") or {})}
        ann = a.get("annotations") or {}
        out.append({
            "source": "prometheus", "status": "resolved" if a.get("status") == "resolved" else "firing",
            "product_id": labels.get("vrixel_product") or labels.get("product") or "",
            "component": labels.get("component") or labels.get("service") or labels.get("app") or labels.get("job") or "",
            "env": labels.get("environment") or labels.get("env") or "production",
            "severity": _sev(labels.get("severity", ""), _ALERT_SEV, "sev3"),
            "title": ann.get("summary") or labels.get("alertname") or "Alert",
            "detail": ann.get("description") or "",
            "fingerprint": f"prometheus:{a.get('fingerprint') or labels.get('alertname', '')}",
            "url": a.get("generatorURL") or "", "at": a.get("startsAt") or now(),
            "labels": {k: str(v) for k, v in labels.items()},
        })
    return out


def from_sentry(payload: dict, resource: str = "") -> list[dict]:
    """Sentry internal-integration webhooks: issue created or regressed, and alert rules firing."""
    data = payload.get("data") or {}
    issue = data.get("issue") or (data.get("event") or {})
    if not issue:
        return []
    tags = {t[0]: t[1] for t in issue.get("tags") or [] if isinstance(t, (list, tuple)) and len(t) == 2}
    tags |= {t.get("key"): t.get("value") for t in issue.get("tags") or [] if isinstance(t, dict)}
    project = issue.get("project") or {}
    project_slug = project.get("slug") if isinstance(project, dict) else str(project)
    action = payload.get("action", "")
    return [{
        "source": "sentry", "status": "resolved" if action == "resolved" else "firing",
        "product_id": tags.get("vrixel_product", ""),
        "component": tags.get("component") or tags.get("service") or project_slug or "",
        "env": tags.get("environment") or issue.get("environment") or "production",
        "severity": _sev(issue.get("level", ""), _SENTRY_SEV, "sev3"),
        "title": issue.get("title") or "Sentry issue",
        "detail": (issue.get("culprit") or "") + (f" ({action})" if action else ""),
        "fingerprint": f"sentry:{issue.get('id') or issue.get('issue_id') or issue.get('title')}",
        "url": issue.get("web_url") or issue.get("permalink") or "", "at": issue.get("lastSeen") or now(),
        "labels": {"resource": resource, **{k: str(v) for k, v in tags.items() if k}},
    }]


def from_ticket(payload: dict) -> list[dict]:
    """A support ticket or a person's report: {product_id, title, description, component?, severity?, url?, id?}."""
    title = str(payload.get("title") or "").strip()
    if not title:
        return []
    key = payload.get("id") or hashlib.sha1(f"{title}{payload.get('description', '')}".encode()).hexdigest()[:12]
    return [{
        "source": "ticket", "status": "resolved" if payload.get("status") == "resolved" else "firing",
        "product_id": str(payload.get("product_id") or ""), "component": str(payload.get("component") or ""),
        "env": "production", "severity": _sev(str(payload.get("severity") or ""), _ALERT_SEV, "sev3"),
        "title": title, "detail": str(payload.get("description") or "")[:4000],
        "fingerprint": f"ticket:{key}", "url": str(payload.get("url") or ""), "at": now(),
        "labels": {"reporter": str(payload.get("reporter") or "")},
    }]


def resolve_product(services, signal: dict) -> tuple[str, str]:
    """(product_id, repo) for a signal: an explicit product label wins; otherwise the component is
    matched against each product's repos and OpenChoreo component names."""
    reg = services.settings.registry
    pid = signal.get("product_id") if signal.get("product_id") in reg else ""
    comp = (signal.get("component") or "").lower()
    for product_id in ([pid] if pid else list(reg)):
        try:
            product = services.snapshot(product_id).product
        except Exception:
            continue
        names = {r.name.split("/", 1)[-1].lower(): r.name for r in product.repos}
        names |= {str(c).lower(): repo for repo, c in ((product.runtime or {}).get("components") or {}).items()}
        if comp and comp in names:
            return product_id, names[comp]
        if pid:
            return pid, ""
    return "", ""


class SignalStore(Protocol):
    def add(self, incident_id: str, product_id: str, signal: dict) -> None: ...
    def for_incident(self, incident_id: str) -> list[dict]: ...
    def open_incident_for(self, product_id: str, fingerprint: str, component: str) -> str | None: ...
    def close(self, incident_id: str) -> None: ...


class InMemorySignalStore:
    def __init__(self) -> None:
        self.rows: list[dict] = []
        self.closed: set[str] = set()
        self._lock = threading.Lock()

    def add(self, incident_id, product_id, signal) -> None:
        with self._lock:
            self.rows.append({"incident_id": incident_id, "product_id": product_id, **signal})

    def for_incident(self, incident_id) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self.rows if r["incident_id"] == incident_id]

    def open_incident_for(self, product_id, fingerprint, component) -> str | None:
        with self._lock:
            for r in reversed(self.rows):
                if r["product_id"] != product_id or r["incident_id"] in self.closed:
                    continue
                if r["fingerprint"] == fingerprint or (component and r.get("component") == component):
                    return r["incident_id"]
        return None

    def close(self, incident_id) -> None:
        with self._lock:
            self.closed.add(incident_id)


class PostgresSignalStore:
    DDL = (
        """CREATE TABLE IF NOT EXISTS vrixel_signals (
             id BIGSERIAL PRIMARY KEY, incident_id TEXT NOT NULL, product_id TEXT NOT NULL,
             fingerprint TEXT NOT NULL, component TEXT NOT NULL, data JSONB NOT NULL,
             closed BOOLEAN NOT NULL DEFAULT false, at TIMESTAMPTZ NOT NULL DEFAULT now())""",
        "CREATE INDEX IF NOT EXISTS vrixel_signals_incident ON vrixel_signals (incident_id)",
        "CREATE INDEX IF NOT EXISTS vrixel_signals_open ON vrixel_signals (product_id, closed)",
    )

    def __init__(self, pool) -> None:
        self.pool = pool
        with self.pool.connection() as conn:
            for statement in self.DDL:
                conn.execute(statement)

    def add(self, incident_id, product_id, signal) -> None:
        with self.pool.connection() as conn:
            conn.execute("INSERT INTO vrixel_signals (incident_id, product_id, fingerprint, component, data) "
                         "VALUES (%s, %s, %s, %s, %s::jsonb)",
                         (incident_id, product_id, signal["fingerprint"], signal.get("component") or "",
                          json.dumps(signal, default=str)))

    def for_incident(self, incident_id) -> list[dict]:
        with self.pool.connection() as conn:
            rows = conn.execute("SELECT data FROM vrixel_signals WHERE incident_id = %s ORDER BY id",
                                (incident_id,)).fetchall()
        return [{"incident_id": incident_id, **dict(r["data"])} for r in rows]

    def open_incident_for(self, product_id, fingerprint, component) -> str | None:
        with self.pool.connection() as conn:
            row = conn.execute(
                "SELECT incident_id FROM vrixel_signals WHERE product_id = %s AND NOT closed "
                "AND (fingerprint = %s OR (%s <> '' AND component = %s)) ORDER BY id DESC LIMIT 1",
                (product_id, fingerprint, component, component)).fetchone()
        return row["incident_id"] if row else None

    def close(self, incident_id) -> None:
        with self.pool.connection() as conn:
            conn.execute("UPDATE vrixel_signals SET closed = true WHERE incident_id = %s", (incident_id,))


def still_firing(signals: list[dict]) -> list[dict]:
    """The latest state of each alert or issue that is still firing (tickets don't resolve themselves)."""
    latest: dict[str, dict] = {}
    for s in signals:
        latest[s["fingerprint"]] = s
    return [s for s in latest.values() if s["status"] == "firing" and s["source"] != "ticket"]

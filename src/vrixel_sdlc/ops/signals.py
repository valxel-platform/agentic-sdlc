"""Production signals: Prometheus alerts (Alertmanager), Sentry issues and support tickets, turned
into one shape and tied to a product and a component.

    {source, status: firing|resolved, product_id, component, env, severity: sev1..sev4,
     title, detail, fingerprint, url, at, labels, category?: cost, resolves?: false}

Cost signals (OpenChoreo budget alerts, AWS Budgets, the tool's own cost reviews) carry
category "cost". Signals that never send a resolution (tickets, OpenChoreo notifications, AWS
Budgets, cost reviews) carry resolves: false, so an incident never waits for them to clear.

Signals are stored outside the incident threads (they keep arriving while an incident waits for a
person), in the platform database when there is one.
"""

from __future__ import annotations

import hashlib
import json
import re
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


def from_openchoreo(payload: dict) -> list[dict]:
    """An OpenChoreo ObservabilityAlertRule's webhook notification (the raw AlertDetails, sent when
    the channel has no payloadTemplate). Budget rules (source type budget, from the FinOps module)
    become cost signals. OpenChoreo notifies when a rule fires and never when it clears."""
    if not payload.get("alertName"):
        return []
    budget = payload.get("alertType") == "budget"
    sev = _sev(str(payload.get("alertSeverity") or ""), _ALERT_SEV, "sev3")
    if budget and sev == "sev1":
        sev = "sev2"  # spend is never an outage
    value, threshold = payload.get("alertValue"), payload.get("alertThreshold")
    detail = str(payload.get("alertDescription") or "")
    if value is not None and threshold is not None:
        detail = (detail + " " if detail else "") + (
            f"(cost ${float(value):,.2f} over the ${float(threshold):,.2f} budget)" if budget else
            f"(value {value}, threshold {threshold})")
    scope = "/".join(str(payload.get(k) or "") for k in ("namespace", "project", "component", "environment"))
    sig = {
        "source": "openchoreo", "status": "firing", "resolves": False,
        "product_id": "", "component": str(payload.get("component") or ""),
        "env": str(payload.get("environment") or "production"), "severity": sev,
        "title": str(payload.get("alertName")), "detail": detail[:4000],
        "fingerprint": f"openchoreo:{scope}:{payload.get('alertName')}", "url": "",
        "at": str(payload.get("alertTimestamp") or now()),
        "labels": {k: str(payload.get(k) or "") for k in ("namespace", "project", "environment", "releaseLine",
                                                          "alertType")},
    }
    if budget:
        sig["category"] = "cost"
        sig["labels"] |= {"alertValue": str(value if value is not None else ""),
                          "alertThreshold": str(threshold if threshold is not None else ""),
                          "window": str(payload.get("alertWindow") or payload.get("window") or "")}
    return [sig]


_BUDGET_FIELDS = {"name": r"Budget Name:\s*(.+)", "budgeted": r"Budgeted Amount:\s*\$?([\d,.]+)",
                  "type": r"Alert Type:\s*(ACTUAL|FORECASTED)", "threshold": r"Alert Threshold:\s*>?\s*\$?([\d,.]+)",
                  "amount": r"(?:ACTUAL|FORECASTED) Amount:\s*\$?([\d,.]+)", "account": r"AWS Account\s*(\d{12})"}


def from_aws_budget(message: str, products, at: str = "") -> list[dict]:
    """An AWS Budgets notification, as delivered by SNS (the Message of a Notification). Budgets
    are named vrixel-<product id>-<environment> (for example vrixel-FARM-production), which ties the
    alert to a product and an environment."""
    got = {k: (m.group(1).strip() if (m := re.search(rx, message or "")) else "") for k, rx in _BUDGET_FIELDS.items()}
    if not got["name"]:
        return []
    money = {k: float(got[k].replace(",", "")) if got[k] else None for k in ("budgeted", "threshold", "amount")}
    product_id, env = "", ""
    rest = got["name"][len("vrixel-"):] if got["name"].lower().startswith("vrixel-") else ""
    for pid in sorted(products, key=len, reverse=True):
        if rest.lower() == pid.lower() or rest.lower().startswith(pid.lower() + "-"):
            product_id, env = pid, rest[len(pid) + 1:]
            break
    actual = got["type"] != "FORECASTED"
    over = actual and money["amount"] is not None and money["budgeted"] and money["amount"] >= money["budgeted"]
    amount = f"${money['amount']:,.2f}" if money["amount"] is not None else "unknown"
    return [{
        "source": "aws-budgets", "category": "cost", "resolves": False, "status": "firing",
        "product_id": product_id, "component": "", "env": env or "production",
        "severity": "sev2" if over else "sev3" if actual else "sev4",
        "title": f"AWS budget {got['name']}: {'forecast' if not actual else 'actual'} spend above its alert",
        "detail": (f"{got['type'] or 'ACTUAL'} {amount} against a budget of "
                   f"${(money['budgeted'] or 0):,.2f} (alert above ${(money['threshold'] or 0):,.2f})"
                   + (f" in account {got['account']}" if got["account"] else "")),
        "fingerprint": f"aws-budgets:{got['name']}:{got['type'] or 'ACTUAL'}:{got['threshold']}",
        "url": "", "at": at or now(),
        "labels": {"budget": got["name"], "account": got["account"], "alert_type": got["type"]},
    }]


def from_cloudwatch(event: dict) -> list[dict]:
    """A CloudWatch alarm state change, as EventBridge delivers it (the data plane's AlarmRouting).
    Only Vrixel alarms count: their description's first line says whose they are,
    `vrixel product=.. feature=.. resource=.. env=.. consumers=a,b alarm=.. severity=..`, and the
    rest is what on-call reads. ALARM fires, OK resolves; INSUFFICIENT_DATA says nothing."""
    if event.get("detail-type") != "CloudWatch Alarm State Change":
        return []
    d = event.get("detail") or {}
    state = (d.get("state") or {}).get("value", "")
    if state not in ("ALARM", "OK"):
        return []
    desc = str((d.get("configuration") or {}).get("description") or "")
    first, _, says = desc.partition("\n")
    if not first.startswith("vrixel "):
        return []
    who = dict(kv.split("=", 1) for kv in first.split()[1:] if "=" in kv)
    who = {k: ("" if v == "-" else v) for k, v in who.items()}
    consumers = [c for c in who.get("consumers", "").split(",") if c]
    name, region = str(d.get("alarmName") or ""), str(event.get("region") or "")
    resource = who.get("resource") or name
    alarm = who.get("alarm", "")
    from urllib.parse import quote

    return [{
        "source": "cloudwatch", "category": "aws", "status": "firing" if state == "ALARM" else "resolved",
        "product_id": who.get("product", ""), "component": consumers[0] if consumers else "",
        "env": who.get("env") or "production", "severity": _sev(who.get("severity", ""), {}, "sev3"),
        "title": f"{resource}: {alarm.replace('-', ' ')}" if alarm else name,
        "detail": (says.strip() + " " + str((d.get("state") or {}).get("reason") or "")).strip()[:4000],
        "fingerprint": f"cloudwatch:{event.get('account', '')}:{name}",
        "url": (f"https://{region}.console.aws.amazon.com/cloudwatch/home?region={region}#alarmsV2:alarm/{quote(name)}"
                if region else ""),
        "at": str((d.get("state") or {}).get("timestamp") or event.get("time") or now()),
        "labels": {"resource": resource, "feature_id": who.get("feature", ""), "alarm": alarm,
                   "consumers": ",".join(consumers), "account": str(event.get("account", "")), "region": region},
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
    # An OpenChoreo alert names its project: the product (or one of its lanes) running there.
    project = (signal.get("labels") or {}).get("project", "")
    if project:
        for product_id in reg:
            try:
                snap = services.snapshot(product_id)
            except Exception:
                continue
            projects = {(snap.product.runtime or {}).get("openchoreo_project")}
            projects |= {(lane.runtime or {}).get("openchoreo_project") for lane in snap.lane_of_repo().values()}
            if project in projects:
                return product_id, ""
    return "", ""


class SignalStore(Protocol):
    def add(self, incident_id: str, product_id: str, signal: dict) -> None: ...
    def for_incident(self, incident_id: str) -> list[dict]: ...
    def open_incident_for(self, product_id: str, fingerprint: str, component: str) -> str | None: ...
    def close(self, incident_id: str) -> None: ...
    # Correlation (ops/correlate.py): open incidents, and moving signals between them.
    def open_incidents(self, product_id: str) -> list[str]: ...
    def move(self, from_incident: str, to_incident: str, fingerprint: str | None = None) -> int: ...
    # Signals that matched no product: kept so a mislabelled alert is seen and fixed, not lost.
    def add_unmatched(self, signal: dict, reason: str) -> None: ...
    def unmatched(self, limit: int = 100) -> list[dict]: ...


# How long a signal that matched no product is kept.
UNMATCHED_DAYS = 30


def unmatched_reason(services, signal: dict) -> str:
    """Why resolve_product found no product for this signal, in words someone fixing the source can act on."""
    pid = signal.get("product_id") or ""
    comp = signal.get("component") or ""
    project = (signal.get("labels") or {}).get("project", "")
    if pid and pid not in services.settings.registry:
        return f"it names product {pid}, which isn't in the registry"
    if comp and project:
        return (f"component {comp} matches no product's repos or OpenChoreo components, and OpenChoreo project "
                f"{project} belongs to no product")
    if comp:
        return f"component {comp} matches no product's repos or OpenChoreo components"
    if project:
        return f"OpenChoreo project {project} belongs to no product"
    return "it names no product, component or OpenChoreo project"


class InMemorySignalStore:
    def __init__(self) -> None:
        self.rows: list[dict] = []
        self.closed: set[str] = set()
        self.unmatched_rows: list[dict] = []
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

    def open_incidents(self, product_id) -> list[str]:
        with self._lock:
            return list(dict.fromkeys(r["incident_id"] for r in self.rows
                                      if r["product_id"] == product_id and r["incident_id"] not in self.closed))

    def move(self, from_incident, to_incident, fingerprint=None) -> int:
        with self._lock:
            n = 0
            for r in self.rows:
                if r["incident_id"] == from_incident and (fingerprint is None or r["fingerprint"] == fingerprint):
                    r["incident_id"] = to_incident
                    n += 1
            return n

    def add_unmatched(self, signal, reason) -> None:
        with self._lock:
            self.unmatched_rows.append({**signal, "reason": reason, "received_at": now()})
            del self.unmatched_rows[:-1000]

    def unmatched(self, limit=100) -> list[dict]:
        with self._lock:
            return [dict(r) for r in reversed(self.unmatched_rows[-limit:])]


class PostgresSignalStore:
    DDL = (
        """CREATE TABLE IF NOT EXISTS vrixel_signals (
             id BIGSERIAL PRIMARY KEY, incident_id TEXT NOT NULL, product_id TEXT NOT NULL,
             fingerprint TEXT NOT NULL, component TEXT NOT NULL, data JSONB NOT NULL,
             closed BOOLEAN NOT NULL DEFAULT false, at TIMESTAMPTZ NOT NULL DEFAULT now())""",
        "CREATE INDEX IF NOT EXISTS vrixel_signals_incident ON vrixel_signals (incident_id)",
        "CREATE INDEX IF NOT EXISTS vrixel_signals_open ON vrixel_signals (product_id, closed)",
        """CREATE TABLE IF NOT EXISTS vrixel_unmatched_signals (
             id BIGSERIAL PRIMARY KEY, data JSONB NOT NULL, reason TEXT NOT NULL,
             received_at TIMESTAMPTZ NOT NULL DEFAULT now())""",
        "CREATE INDEX IF NOT EXISTS vrixel_unmatched_signals_at ON vrixel_unmatched_signals (received_at)",
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

    def open_incidents(self, product_id) -> list[str]:
        with self.pool.connection() as conn:
            rows = conn.execute("SELECT incident_id, min(id) AS first FROM vrixel_signals WHERE product_id = %s "
                                "AND NOT closed GROUP BY incident_id ORDER BY first", (product_id,)).fetchall()
        return [r["incident_id"] for r in rows]

    def move(self, from_incident, to_incident, fingerprint=None) -> int:
        with self.pool.connection() as conn:
            cur = conn.execute(
                "UPDATE vrixel_signals SET incident_id = %s WHERE incident_id = %s AND (%s::text IS NULL OR fingerprint = %s)",
                (to_incident, from_incident, fingerprint, fingerprint))
            return cur.rowcount

    def add_unmatched(self, signal, reason) -> None:
        with self.pool.connection() as conn:
            conn.execute("INSERT INTO vrixel_unmatched_signals (data, reason) VALUES (%s::jsonb, %s)",
                         (json.dumps(signal, default=str), reason))
            conn.execute("DELETE FROM vrixel_unmatched_signals WHERE received_at < now() - make_interval(days => %s)",
                         (UNMATCHED_DAYS,))

    def unmatched(self, limit=100) -> list[dict]:
        with self.pool.connection() as conn:
            rows = conn.execute("SELECT data, reason, received_at FROM vrixel_unmatched_signals ORDER BY id DESC LIMIT %s",
                                (limit,)).fetchall()
        return [{**dict(r["data"]), "reason": r["reason"], "received_at": r["received_at"].isoformat()} for r in rows]


def still_firing(signals: list[dict]) -> list[dict]:
    """The latest state of each alert or issue that is still firing. Tickets and notifications that
    never send a resolution (resolves: false) aren't waited on."""
    latest: dict[str, dict] = {}
    for s in signals:
        latest[s["fingerprint"]] = s
    return [s for s in latest.values() if s["status"] == "firing" and resolves(s)]


def resolves(signal: dict) -> bool:
    return signal["source"] != "ticket" and signal.get("resolves", True) is not False

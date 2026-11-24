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
from datetime import UTC, datetime, timezone
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
            "product_id": labels.get("valxel_product") or labels.get("product") or "",
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
        "product_id": tags.get("valxel_product", ""),
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
    key = payload.get("id") or hashlib.sha1(f"{title}{payload.get('description', '')}".encode(), usedforsecurity=False).hexdigest()[:12]
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
    are named valxel-<product id>-<environment> (for example valxel-FARM-production), which ties the
    alert to a product and an environment."""
    got = {k: (m.group(1).strip() if (m := re.search(rx, message or "")) else "") for k, rx in _BUDGET_FIELDS.items()}
    if not got["name"]:
        return []
    money = {k: float(got[k].replace(",", "")) if got[k] else None for k in ("budgeted", "threshold", "amount")}
    product_id, env = "", ""
    rest = got["name"][len("valxel-"):] if got["name"].lower().startswith("valxel-") else ""
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
    Only Valxel alarms count: their description's first line says whose they are,
    `valxel product=.. feature=.. resource=.. env=.. consumers=a,b alarm=.. severity=..`, and the
    rest is what on-call reads. ALARM fires, OK resolves; INSUFFICIENT_DATA says nothing."""
    if event.get("detail-type") != "CloudWatch Alarm State Change":
        return []
    d = event.get("detail") or {}
    state = (d.get("state") or {}).get("value", "")
    if state not in ("ALARM", "OK"):
        return []
    desc = str((d.get("configuration") or {}).get("description") or "")
    first, _, says = desc.partition("\n")
    if not first.startswith("valxel "):
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


def from_inspector(event: dict) -> list[dict]:
    """A vulnerability Amazon Inspector found in an image already in ECR, as EventBridge delivers it
    (ECR enhanced scanning, continuous: images are rescanned whenever a new CVE is published, not
    only at build). Only high and critical package vulnerabilities in images that are running
    (inUseCount, when Inspector reports it) count; a closed finding resolves its signal. The ECR
    repository's name is the component's (the build pushes each component to its own)."""
    if event.get("detail-type") != "Inspector2 Finding":
        return []
    d = event.get("detail") or {}
    sev = str(d.get("severity") or "").upper()
    if d.get("type") != "PACKAGE_VULNERABILITY" or sev not in ("CRITICAL", "HIGH"):
        return []
    images = [r for r in d.get("resources") or [] if r.get("type") == "AWS_ECR_CONTAINER_IMAGE"]
    if not images:
        return []
    img = (images[0].get("details") or {}).get("awsEcrContainerImage") or {}
    if img.get("inUseCount") is not None and int(img.get("inUseCount") or 0) == 0:
        return []  # not running anywhere: the next build picks up the fix
    vuln = d.get("packageVulnerabilityDetails") or {}
    pkgs = vuln.get("vulnerablePackages") or []
    fixed = ", ".join(f"{p.get('name')} {p.get('version')} -> {p.get('fixedInVersion')}" for p in pkgs if p.get("fixedInVersion"))
    fix = str(d.get("fixAvailable") or "").upper()
    status = str(d.get("status") or "").upper()
    repo_name = str(img.get("repositoryName") or "")
    return [{
        "source": "inspector", "category": "security", "status": "resolved" if status == "CLOSED" else "firing",
        "product_id": "", "component": repo_name.rsplit("/", 1)[-1], "env": "production",
        # Not an outage: a known hole in what runs. Critical with a fix is the one to act on now.
        "severity": "sev2" if sev == "CRITICAL" and fix in ("YES", "PARTIAL") else "sev3",
        "title": f"{repo_name}: {vuln.get('vulnerabilityId') or d.get('title', '')} ({sev.lower()})",
        "detail": (f"{d.get('title', '')}. " + (f"Fixed in: {fixed}. Rebuild with the fixed version."
                                                 if fixed else "No fixed version yet.")
                   + f" Image {str(img.get('imageHash') or '')[:19]}, tags {', '.join(img.get('imageTags') or [])}.")[:4000],
        "fingerprint": f"inspector:{d.get('findingArn') or repo_name + ':' + str(vuln.get('vulnerabilityId'))}",
        "url": str(vuln.get("sourceUrl") or ""), "at": str(d.get("updatedAt") or event.get("time") or now()),
        "labels": {"vulnerability": str(vuln.get("vulnerabilityId") or ""), "image": str(img.get("imageHash") or ""),
                   "repository": repo_name, "fix_available": fix, "account": str(event.get("account", ""))},
    }]


# ---------------------------------------------------------------- Google Cloud (ADR 0010, CP-206)
def _valxel_line(text: str) -> tuple[dict[str, str], str]:
    """The `valxel product=.. feature=.. resource=.. env=.. consumers=a,b alarm=.. severity=..` first
    line the platform's alert policies (and CloudWatch alarms) carry, and the rest of the text."""
    first, _, says = (text or "").partition("\n")
    if not first.startswith("valxel "):
        return {}, text or ""
    who = dict(kv.split("=", 1) for kv in first.split()[1:] if "=" in kv)
    return {k: ("" if v == "-" else v) for k, v in who.items()}, says


def from_cloud_monitoring(payload: dict) -> list[dict]:
    """A Cloud Monitoring alert policy's incident, as its webhook channel sends it (the data plane's
    alarm-routing composition). Only Valxel's policies count: their documentation's first line says
    whose they are, as a CloudWatch alarm's description does. open fires, closed resolves."""
    inc = payload.get("incident") or {}
    state = str(inc.get("state") or "").lower()
    if state not in ("open", "closed"):
        return []
    who, says = _valxel_line(str((inc.get("documentation") or {}).get("content") or ""))
    if not who:
        return []
    consumers = [c for c in who.get("consumers", "").split(",") if c]
    policy, project = str(inc.get("policy_name") or ""), str(inc.get("scoping_project_id") or "")
    resource = who.get("resource") or str(inc.get("resource_name") or policy)
    alarm = who.get("alarm", "")
    started = str(inc.get("started_at") or inc.get("ended_at") or "")
    at = datetime.fromtimestamp(int(started), UTC).isoformat() if started.isdigit() else now()
    return [{
        "source": "cloud-monitoring", "category": "gcp", "status": "firing" if state == "open" else "resolved",
        "product_id": who.get("product", ""), "component": consumers[0] if consumers else "",
        "env": who.get("env") or "production", "severity": _sev(who.get("severity", ""), {}, "sev3"),
        "title": f"{resource}: {alarm.replace('-', ' ')}" if alarm else policy,
        "detail": (says.strip() + " " + str(inc.get("summary") or "")).strip()[:4000],
        "fingerprint": f"cloud-monitoring:{project}:{inc.get('policy_name') or ''}:{inc.get('condition_name') or ''}",
        "url": str(inc.get("url") or ""), "at": at,
        "labels": {"resource": resource, "feature_id": who.get("feature", ""), "alarm": alarm,
                   "consumers": ",".join(consumers), "project": project},
    }]


def _pubsub_data(push: dict) -> tuple[dict, dict]:
    """(the message's JSON data, its attributes) from a Pub/Sub push request."""
    import base64

    msg = push.get("message") or {}
    try:
        data = json.loads(base64.b64decode(msg.get("data") or "") or b"{}")
    except ValueError:
        data = {}
    return (data if isinstance(data, dict) else {}), dict(msg.get("attributes") or {})


def from_gcp_budget(push: dict, products, at: str = "") -> list[dict]:
    """A Cloud Billing budget's Pub/Sub notification. Google sends one every so often whether or not
    a threshold was crossed; only those that name a crossed threshold count. Budgets are named
    valxel-<product id>-<environment>, as on AWS."""
    data, attrs = _pubsub_data(push)
    name = str(data.get("budgetDisplayName") or "")
    crossed = data.get("alertThresholdExceeded")
    forecast = data.get("forecastThresholdExceeded")
    if not name or (crossed is None and forecast is None):
        return []
    product_id, env = "", ""
    rest = name[len("valxel-"):] if name.lower().startswith("valxel-") else ""
    for pid in sorted(products, key=len, reverse=True):
        if rest.lower() == pid.lower() or rest.lower().startswith(pid.lower() + "-"):
            product_id, env = pid, rest[len(pid) + 1:]
            break
    actual = crossed is not None
    spent, budget = float(data.get("costAmount") or 0), float(data.get("budgetAmount") or 0)
    threshold = float(crossed if crossed is not None else forecast or 0)
    over = actual and budget and spent >= budget
    kind = "ACTUAL" if actual else "FORECASTED"
    cur = str(data.get("currencyCode") or "USD")
    return [{
        "source": "gcp-budgets", "category": "cost", "resolves": False, "status": "firing",
        "product_id": product_id, "component": "", "env": env or "production",
        "severity": "sev2" if over else "sev3" if actual else "sev4",
        "title": f"Google Cloud budget {name}: {'actual' if actual else 'forecast'} spend above {threshold:.0%}",
        "detail": f"{kind} {spent:,.2f} {cur} against a budget of {budget:,.2f} {cur} since "
                  f"{data.get('costIntervalStart', 'the period began')}",
        "fingerprint": (f"gcp-budgets:{attrs.get('budgetId') or name}:{kind}:{threshold}:"
                        f"{data.get('costIntervalStart', '')}"),
        "url": "", "at": at or now(),
        "labels": {"budget": name, "billing_account": str(attrs.get("billingAccountId") or ""), "alert_type": kind},
    }]


def from_scc(push: dict) -> list[dict]:
    """A Security Command Center finding, from its Pub/Sub notification config: high and critical
    vulnerabilities (Artifact Analysis) in images that run. The image's repository name is the
    component's, as with Inspector; an inactive finding resolves its signal."""
    data, _ = _pubsub_data(push)
    f = data.get("finding") or {}
    res = data.get("resource") or {}
    sev = str(f.get("severity") or "").upper()
    if str(f.get("findingClass") or "").upper() != "VULNERABILITY" or sev not in ("CRITICAL", "HIGH"):
        return []
    image = str(f.get("resourceName") or res.get("name") or "")
    # .../dockerImages/<component>@sha256:... or .../<repo>/<component>@sha256:...
    path = image.split("@", 1)[0].rstrip("/")
    component = path.rsplit("/", 1)[-1].split(":", 1)[0] if "/" in path else ""
    cve = str(((f.get("vulnerability") or {}).get("cve") or {}).get("id") or f.get("category") or "")
    fixed = str(((f.get("vulnerability") or {}).get("fixedPackage") or {}).get("packageVersion") or "")
    project = str(res.get("projectDisplayName") or res.get("project_display_name") or "")
    return [{
        "source": "scc", "category": "security",
        "status": "resolved" if str(f.get("state") or "").upper() == "INACTIVE" else "firing",
        "product_id": "", "component": component, "env": "production",
        "severity": "sev2" if sev == "CRITICAL" and fixed else "sev3",
        "title": f"{component or image}: {cve} ({sev.lower()})",
        "detail": (str(f.get("description") or "")[:3000]
                   + (f" Fixed in {fixed}: rebuild with it." if fixed else " No fixed version yet.")),
        "fingerprint": f"scc:{f.get('name') or image + ':' + cve}",
        "url": str(f.get("externalUri") or ""), "at": str(f.get("eventTime") or now()),
        "labels": {"vulnerability": cve, "image": image, "project": project},
    }]


def _subscription_of(resource_id: str) -> str:
    """An Azure resource ID's subscription (/subscriptions/<id>/...), lowercase."""
    parts = (resource_id or "").strip("/").split("/")
    return parts[1].lower() if len(parts) > 1 and parts[0].lower() == "subscriptions" else ""


AZURE_SEV = {"sev0": "sev1", "sev1": "sev2", "sev2": "sev3", "sev3": "sev4", "sev4": "sev4"}


def from_azure_monitor(payload: dict) -> list[dict]:
    """An Azure Monitor alert in the common alert schema, as an action group's webhook sends it (the
    data plane's alarm-routing composition). Only Valxel's alert rules count: their description's
    first line says whose they are, as a CloudWatch alarm's does. Fired fires, Resolved resolves."""
    if str(payload.get("schemaId") or "") != "azureMonitorCommonAlertSchema":
        return []
    ess = (payload.get("data") or {}).get("essentials") or {}
    condition = str(ess.get("monitorCondition") or "")
    if condition not in ("Fired", "Resolved"):
        return []
    who, says = _valxel_line(str(ess.get("description") or ""))
    if not who:
        return []
    consumers = [c for c in who.get("consumers", "").split(",") if c]
    targets = [str(x) for x in ess.get("alertTargetIDs") or []]
    subscription = _subscription_of(targets[0] if targets else str(ess.get("alertId") or ""))
    rule = str(ess.get("alertRule") or "")
    resource = who.get("resource") or (str((ess.get("configurationItems") or [rule])[0]) if rule else "")
    alarm = who.get("alarm", "")
    alert_id = str(ess.get("alertId") or "")
    from urllib.parse import quote

    return [{
        "source": "azure-monitor", "category": "azure", "status": "firing" if condition == "Fired" else "resolved",
        "product_id": who.get("product", ""), "component": consumers[0] if consumers else "",
        "env": who.get("env") or "production",
        "severity": _sev(who.get("severity", ""), {}, AZURE_SEV.get(str(ess.get("severity") or "").lower(), "sev3")),
        "title": f"{resource}: {alarm.replace('-', ' ')}" if alarm else rule,
        "detail": says.strip()[:4000],
        "fingerprint": f"azure-monitor:{subscription}:{rule}:{','.join(sorted(targets))}",
        "url": (f"https://portal.azure.com/#blade/Microsoft_Azure_Monitoring/AlertDetailsTemplateBlade/alertId/"
                f"{quote(alert_id, safe='')}") if alert_id else "",
        "at": str(ess.get("resolvedDateTime") if condition == "Resolved" else ess.get("firedDateTime") or now()),
        "labels": {"resource": resource, "feature_id": who.get("feature", ""), "alarm": alarm,
                   "consumers": ",".join(consumers), "subscription": subscription},
    }]


def from_azure_budget(payload: dict, products, at: str = "") -> list[dict]:
    """A Cost Management budget's alert, as an action group's webhook sends it (schema "AIP Budget
    Notification"). Budgets are named valxel-<product id>-<environment>, as on AWS."""
    if str(payload.get("schemaId") or "") != "AIP Budget Notification":
        return []
    data = payload.get("data") or {}
    name = str(data.get("BudgetName") or "")
    if not name:
        return []
    product_id, env = "", ""
    rest = name[len("valxel-"):] if name.lower().startswith("valxel-") else ""
    for pid in sorted(products, key=len, reverse=True):
        if rest.lower() == pid.lower() or rest.lower().startswith(pid.lower() + "-"):
            product_id, env = pid, rest[len(pid) + 1:]
            break
    budget, spent = float(data.get("Budget") or 0), float(data.get("SpendingAmount") or 0)
    threshold = float(data.get("NotificationThresholdAmount") or 0)
    share = threshold / budget if budget else 0.0
    over = budget and spent >= budget
    unit = str(data.get("Unit") or "USD")
    subscription = str(data.get("SubscriptionId") or "").lower()
    return [{
        "source": "azure-budgets", "category": "cost", "resolves": False, "status": "firing",
        "product_id": product_id, "component": "", "env": env or "production",
        "severity": "sev2" if over else "sev3",
        "title": f"Azure budget {name}: spend above {share:.0%}",
        "detail": f"{spent:,.2f} {unit} against a budget of {budget:,.2f} {unit} since "
                  f"{data.get('BudgetStartDate') or 'the period began'}",
        "fingerprint": f"azure-budgets:{subscription}:{name}:{threshold}:{data.get('BudgetStartDate', '')}",
        "url": "", "at": at or now(),
        "labels": {"budget": name, "subscription": subscription},
    }]


def from_defender(payload: dict) -> list[dict]:
    """A Defender for Cloud container image vulnerability (a sub-assessment of the registry or
    running-images recommendation), as workflow automation posts it: high and critical findings in
    images that run. The image's repository name is the component's, as with Inspector and Security
    Command Center; a healthy (fixed) finding resolves its signal."""
    props = payload.get("properties")
    item: dict = props if isinstance(props, dict) else payload
    extra = item.get("additionalData") or {}
    if "Vulnerability" not in str(extra.get("assessedResourceType") or ""):
        return []
    vuln = extra.get("vulnerabilityDetails") or {}
    sev = str(vuln.get("severity") or (item.get("status") or {}).get("severity") or item.get("severity") or "").upper()
    if sev not in ("CRITICAL", "HIGH"):
        return []
    art = extra.get("artifactDetails") or {}
    repo = str(art.get("repositoryName") or extra.get("repositoryName") or "")
    component = repo.rsplit("/", 1)[-1]
    digest = str(art.get("digest") or extra.get("imageDigest") or "")
    image = f"{art.get('registryHost') or extra.get('registryHost') or ''}/{repo}@{digest}".lstrip("/")
    cves = extra.get("cve") or []
    cve = str(vuln.get("cveId") or (cves[0].get("title") if cves and isinstance(cves[0], dict) else "")
              or item.get("displayName") or "")
    fixed = str((extra.get("softwareDetails") or {}).get("fixedVersion") or "")
    healthy = str((item.get("status") or {}).get("code") or "").lower() == "healthy"
    subscription = _subscription_of(str(payload.get("id") or item.get("resourceDetails", {}).get("id") or ""))
    return [{
        "source": "defender", "category": "security", "status": "resolved" if healthy else "firing",
        "product_id": "", "component": component, "env": "production",
        "severity": "sev2" if sev == "CRITICAL" and fixed else "sev3",
        "title": f"{component or image}: {cve} ({sev.lower()})",
        "detail": (str(item.get("description") or item.get("impact") or "")[:3000]
                   + (f" Fixed in {fixed}: rebuild with it." if fixed else " No fixed version yet.")),
        "fingerprint": f"defender:{subscription}:{image}:{cve}",
        "url": "", "at": str(item.get("timeGenerated") or now()),
        "labels": {"vulnerability": cve, "image": image, "subscription": subscription},
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
        """CREATE TABLE IF NOT EXISTS valxel_signals (
             id BIGSERIAL PRIMARY KEY, incident_id TEXT NOT NULL, product_id TEXT NOT NULL,
             fingerprint TEXT NOT NULL, component TEXT NOT NULL, data JSONB NOT NULL,
             closed BOOLEAN NOT NULL DEFAULT false, at TIMESTAMPTZ NOT NULL DEFAULT now())""",
        "CREATE INDEX IF NOT EXISTS valxel_signals_incident ON valxel_signals (incident_id)",
        "CREATE INDEX IF NOT EXISTS valxel_signals_open ON valxel_signals (product_id, closed)",
        """CREATE TABLE IF NOT EXISTS valxel_unmatched_signals (
             id BIGSERIAL PRIMARY KEY, data JSONB NOT NULL, reason TEXT NOT NULL,
             received_at TIMESTAMPTZ NOT NULL DEFAULT now())""",
        "CREATE INDEX IF NOT EXISTS valxel_unmatched_signals_at ON valxel_unmatched_signals (received_at)",
    )

    def __init__(self, pool) -> None:
        self.pool = pool
        with self.pool.connection() as conn:
            for statement in self.DDL:
                conn.execute(statement)

    def add(self, incident_id, product_id, signal) -> None:
        with self.pool.connection() as conn:
            conn.execute("INSERT INTO valxel_signals (incident_id, product_id, fingerprint, component, data) "
                         "VALUES (%s, %s, %s, %s, %s::jsonb)",
                         (incident_id, product_id, signal["fingerprint"], signal.get("component") or "",
                          json.dumps(signal, default=str)))

    def for_incident(self, incident_id) -> list[dict]:
        with self.pool.connection() as conn:
            rows = conn.execute("SELECT data FROM valxel_signals WHERE incident_id = %s ORDER BY id",
                                (incident_id,)).fetchall()
        return [{"incident_id": incident_id, **dict(r["data"])} for r in rows]

    def open_incident_for(self, product_id, fingerprint, component) -> str | None:
        with self.pool.connection() as conn:
            row = conn.execute(
                "SELECT incident_id FROM valxel_signals WHERE product_id = %s AND NOT closed "
                "AND (fingerprint = %s OR (%s <> '' AND component = %s)) ORDER BY id DESC LIMIT 1",
                (product_id, fingerprint, component, component)).fetchone()
        return row["incident_id"] if row else None

    def close(self, incident_id) -> None:
        with self.pool.connection() as conn:
            conn.execute("UPDATE valxel_signals SET closed = true WHERE incident_id = %s", (incident_id,))

    def open_incidents(self, product_id) -> list[str]:
        with self.pool.connection() as conn:
            rows = conn.execute("SELECT incident_id, min(id) AS first FROM valxel_signals WHERE product_id = %s "
                                "AND NOT closed GROUP BY incident_id ORDER BY first", (product_id,)).fetchall()
        return [r["incident_id"] for r in rows]

    def move(self, from_incident, to_incident, fingerprint=None) -> int:
        with self.pool.connection() as conn:
            cur = conn.execute(
                "UPDATE valxel_signals SET incident_id = %s WHERE incident_id = %s AND (%s::text IS NULL OR fingerprint = %s)",
                (to_incident, from_incident, fingerprint, fingerprint))
            return cur.rowcount

    def add_unmatched(self, signal, reason) -> None:
        with self.pool.connection() as conn:
            conn.execute("INSERT INTO valxel_unmatched_signals (data, reason) VALUES (%s::jsonb, %s)",
                         (json.dumps(signal, default=str), reason))
            conn.execute("DELETE FROM valxel_unmatched_signals WHERE received_at < now() - make_interval(days => %s)",
                         (UNMATCHED_DAYS,))

    def unmatched(self, limit=100) -> list[dict]:
        with self.pool.connection() as conn:
            rows = conn.execute("SELECT data, reason, received_at FROM valxel_unmatched_signals ORDER BY id DESC LIMIT %s",
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

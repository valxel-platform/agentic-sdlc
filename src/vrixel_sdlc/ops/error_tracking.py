"""Where a product's error reports go, and GlitchTip's alerts as signals.

The platform's `platform/error-tracking.yaml` (sdlc-governance) says which tracker a product may use:
self-hosted GlitchTip for payments and for data in India, Sentry's SaaS (with a BAA for PHI)
otherwise. A product chooses in product.yaml:

    runtime:
      error_tracking:
        provider: glitchtip            # or sentry
        url: https://errors.apx.vrixel.dev   # GlitchTip's own address (glitchtip only)
        baa: true                      # sentry with PHI: Sentry's BAA is signed

The governance loader refuses a choice the rules don't allow.

GlitchTip alerts reach the tool through a webhook recipient per project, at
`/api/signals/glitchtip/<product>?token=<token_for(product)>` (a webhook can't send headers, so
the token is in the URL, and is the product's own: a leaked URL opens one product's endpoint
only). An alert names the issue but not the request; with an API token for the product
(VRIXEL_GLITCHTIP_TOKENS, {"APX": "<token>"}) the tool reads the issue's latest event for its
`trace_id`, which ties it to other components' failures (ops/correlate.py).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
from datetime import datetime, timezone

import yaml

PROVIDERS = ("sentry", "glitchtip")


class PolicyError(ValueError):
    pass


def load_policy(text: str) -> dict:
    if not (text or "").strip():
        return {}
    doc = yaml.safe_load(text) or {}
    for r in doc.get("rules") or []:
        for p in ([r["require"]] if r.get("require") else []) + list(r.get("allow") or []):
            if p not in PROVIDERS:
                raise PolicyError(f"error-tracking rule {r.get('name')}: unknown provider {p}")
    return doc


def _matches(when: dict, data_class: str, region: str) -> bool:
    if "data_class" in when and data_class not in ([when["data_class"]] if isinstance(when["data_class"], str)
                                                   else when["data_class"]):
        return False
    regions = when.get("region")
    return not (regions is not None and region not in ([regions] if isinstance(regions, str) else regions))


def check(runtime: dict | None, data_class: str, policy: dict) -> list[str]:
    """Problems with a product's runtime.error_tracking under the platform's rules."""
    choice = (runtime or {}).get("error_tracking")
    if not choice or not policy:
        return []
    provider = str(choice.get("provider") or "")
    if provider not in PROVIDERS:
        return [f"runtime.error_tracking.provider must be one of {PROVIDERS}"]
    problems = []
    if provider == "glitchtip" and not re.fullmatch(r"https://[a-z0-9.-]+\.[a-z]{2,}", str(choice.get("url") or "")):
        problems.append("runtime.error_tracking.url must be GlitchTip's https address, e.g. https://errors.apx.vrixel.dev")
    region = str(((runtime or {}).get("aws") or {}).get("region") or "")
    for rule in policy.get("rules") or []:
        if not _matches(rule.get("when") or {}, data_class, region):
            continue
        why = rule.get("why", "")
        if rule.get("require") and provider != rule["require"]:
            problems.append(f"error tracking must be {rule['require']} ({rule.get('name')}): {why}".rstrip(": "))
        elif rule.get("allow") and provider not in rule["allow"]:
            problems.append(f"error tracking may be {', '.join(rule['allow'])} ({rule.get('name')})")
        elif provider == "sentry" and rule.get("sentry_needs") == "baa" and choice.get("baa") is not True:
            problems.append(f"Sentry for {data_class} needs a signed BAA (runtime.error_tracking.baa: true): {why}")
        break
    return problems


def token_for(product_id: str, secret: str | None = None) -> str:
    """The product's GlitchTip webhook token, derived from the signals token."""
    secret = secret if secret is not None else os.environ.get("VRIXEL_SIGNALS_TOKEN", "")
    if not secret:
        return ""
    return hmac.new(secret.encode(), f"glitchtip:{product_id}".encode(), hashlib.sha256).hexdigest()[:40]


def from_glitchtip(payload: dict, product_id: str) -> list[dict]:
    """A GlitchTip alert webhook (Slack-style: text, and attachments with the issue's title, link
    and fields such as Project, Environment and Release). Alerts never send a resolution."""
    att = next((a for a in payload.get("attachments") or [] if isinstance(a, dict)), {})
    title = str(att.get("title") or payload.get("text") or "")
    link = str(att.get("title_link") or "")
    if not title or title == "GlitchTip Alert" and not link:
        return []
    fields = {str(f.get("title", "")).strip().lower(): str(f.get("value", ""))
              for f in att.get("fields") or [] if isinstance(f, dict)}
    issue = (re.search(r"/issues/(\d+)", link) or [None, ""])[1]
    project = fields.get("project", "")
    return [{
        "source": "glitchtip", "status": "firing", "resolves": False, "product_id": product_id,
        "component": project, "env": fields.get("environment") or "production", "severity": "sev3",
        "title": title[:300], "detail": str(att.get("text") or "")[:2000],
        "fingerprint": f"glitchtip:{product_id}:{issue or title}", "url": link,
        "at": datetime.now(timezone.utc).isoformat(),
        "labels": {"issue": issue, "project": project, "release": fields.get("release", "")},
    }]


def enrich(signal: dict, base_url: str, token: str, opener=None) -> dict:
    """Read the issue's latest event (Sentry-compatible API) for its trace id, endpoint and
    environment. The address is GlitchTip's configured one, never taken from the alert."""
    issue = (signal.get("labels") or {}).get("issue", "")
    if not (issue.isdigit() and base_url and token):
        return signal
    from urllib.request import Request, build_opener

    req = Request(f"{base_url.rstrip('/')}/api/0/issues/{issue}/events/latest/",
                  headers={"Authorization": f"Bearer {token}", "Accept": "application/json"})
    try:
        with (opener or build_opener()).open(req, timeout=10) as resp:
            event = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return signal
    tags = {}
    for t in event.get("tags") or []:
        if isinstance(t, dict):
            tags[str(t.get("key"))] = str(t.get("value"))
        elif isinstance(t, (list, tuple)) and len(t) == 2:
            tags[str(t[0])] = str(t[1])
    labels = {**(signal.get("labels") or {})}
    for key in ("trace_id", "endpoint", "status"):
        if tags.get(key):
            labels[key] = tags[key]
    out = {**signal, "labels": labels}
    if tags.get("environment"):
        out["env"] = tags["environment"]
    return out


def api_token(product_id: str) -> str:
    try:
        return str((json.loads(os.environ.get("VRIXEL_GLITCHTIP_TOKENS") or "{}") or {}).get(product_id) or "")
    except ValueError:
        return ""

"""Where a product's error reports go, and GlitchTip's alerts as signals.

The platform's `platform/error-tracking.yaml` (sdlc-governance) says which tracker a product may use:
self-hosted GlitchTip for payments and for data in India, Sentry's SaaS (with a BAA for PHI)
otherwise. A product chooses in product.yaml:

    runtime:
      error_tracking:
        provider: glitchtip            # or sentry
        url: https://errors.pay.example.dev   # GlitchTip's own address (glitchtip only)
        baa: true                      # sentry with PHI: Sentry's BAA is signed

The governance loader refuses a choice the rules don't allow.

GlitchTip alerts reach the tool through a webhook recipient per project, at
`/api/signals/glitchtip/<product>?token=<token_for(product)>` (a webhook can't send headers, so
the token is in the URL, and is the product's own: a leaked URL opens one product's endpoint
only). An alert names the issue but not the request; with an API token for the product
(VALXEL_GLITCHTIP_TOKENS, {"PAY": "<token>"}) the tool reads the issue's latest event for its
`trace_id`, which ties it to other components' failures (ops/correlate.py).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from datetime import datetime, timezone

# The platform's rules and the check of a product's choice are the core's: error_tracking_policy.py.
from ..error_tracking_policy import PROVIDERS, PolicyError, check, load_policy  # noqa: F401  (re-exported)


def token_for(product_id: str, secret: str | None = None) -> str:
    """The product's GlitchTip webhook token, derived from the (current) signals token."""
    if secret is None:
        from ..secret_ring import current

        secret = current("VALXEL_SIGNALS_TOKEN")
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
        with (opener or build_opener()).open(req, timeout=3) as resp:  # inside a webhook: keep it short
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
        return str((json.loads(_glitchtip_tokens() or "{}") or {}).get(product_id) or "")
    except ValueError:
        return ""


def _glitchtip_tokens() -> str:
    from ..secret_ring import get

    return get("VALXEL_GLITCHTIP_TOKENS")

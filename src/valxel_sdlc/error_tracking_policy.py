"""Which error tracker a product may use: the platform's rules (platform/error-tracking.yaml in
sdlc-governance) and the check of a product's choice against them. Plain data in, findings out.

Governance reads these when it loads a product, so they're the core's (ADR 0005); turning GlitchTip's
alerts into signals stays in ops/error_tracking.py.
"""

from __future__ import annotations

import re

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
        problems.append("runtime.error_tracking.url must be GlitchTip's https address, e.g. https://errors.pay.example.dev")
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

"""The platform's default OpenChoreo alert rules (sdlc-governance platform/alert-rules.yaml).

Every component that runs code gets them as `observability-alert-rule` traits before it deploys:
exceptions in its logs, memory near its limit. The pipeline adds any that are missing and never
touches a trait someone tuned. OpenChoreo evaluates them per environment and notifies the
environment's default channel, which the platform points at the tool's /api/signals/openchoreo.
"""

from __future__ import annotations

import fnmatch

import yaml

SOURCES = ("log", "metric", "budget")
SEVERITIES = ("info", "warning", "critical")


class AlertRulesError(ValueError):
    pass


def load(text: str) -> dict:
    """Parse and check the file; empty text means no default rules."""
    if not (text or "").strip():
        return {}
    doc = yaml.safe_load(text) or {}
    for name, r in (doc.get("rules") or {}).items():
        src = r.get("source") or {}
        if src.get("type") not in SOURCES:
            raise AlertRulesError(f"alert rule {name}: source.type must be one of {SOURCES}")
        if src["type"] == "log" and not src.get("query"):
            raise AlertRulesError(f"alert rule {name}: a log rule needs source.query")
        if src["type"] == "metric" and src.get("metric") not in ("cpu_usage", "memory_usage"):
            raise AlertRulesError(f"alert rule {name}: source.metric must be cpu_usage or memory_usage")
        if r.get("severity", "warning") not in SEVERITIES:
            raise AlertRulesError(f"alert rule {name}: severity must be one of {SEVERITIES}")
        if not isinstance((r.get("condition") or {}).get("threshold"), int):
            raise AlertRulesError(f"alert rule {name}: condition.threshold must be a whole number")
    return doc


def traits_for(doc: dict, component: str, component_type: str = "") -> list[dict]:
    """The rules as traits for one component. Instance names start with the component's name, so
    the rules (and the alerts they raise) are told apart across components."""
    if not doc or not doc.get("rules"):
        return []
    if component_type and not any(fnmatch.fnmatch(component_type, p) for p in doc.get("applies_to") or ["*"]):
        return []
    out = []
    for name, r in doc["rules"].items():
        src = {k: v for k, v in (r.get("source") or {}).items() if k in ("type", "query", "metric")}
        out.append({"name": doc.get("trait", "observability-alert-rule"), "kind": "ClusterTrait",
                    "instanceName": f"{component}-{name}"[:63].rstrip("-"),
                    "parameters": {"description": f"{r.get('description', name)} {r.get('says', '')}".strip(),
                                   "severity": r.get("severity", "warning"), "source": src,
                                   "condition": dict(r.get("condition") or {})}})
    return out


def missing(existing: list[dict], wanted: list[dict]) -> list[dict]:
    """The wanted traits a component doesn't have yet (by instance name)."""
    have = {t.get("instanceName") for t in existing or []}
    return [t for t in wanted if t["instanceName"] not in have]

"""OpenChoreo's AI agents, as sources of evidence for incidents.

    SRE agent (rca)   POST /api/v1alpha1/rca-agent/analyze      -> {report_id, status: pending}
                      GET  /api/v1/rca-agent/reports/{report_id} -> {status, report}
    FinOps agent      POST /api/v1alpha1/analyses               -> {reportId, status: pending}
                      GET  /api/v1alpha1/reports/{report_id}    -> {status, report}

Both investigate in the background with an AI model and write a structured report: root causes
with a confidence (high, medium, low), the evidence and timeline, and recommendations; or, for a
budget alert, whether the component is overprovisioned and what to set it to. The tool only asks
for reports and reads them. It never updates a report's actions: the SRE agent's own fix agent
(REMED_AGENT) must stay off, and a report showing an action it applied is treated as a change made
outside the tool (ops/remediation.py).

Configured with VRIXEL_OPENCHOREO_RCA_URL and VRIXEL_OPENCHOREO_FINOPS_URL; requests carry the
tool's OpenChoreo client-credentials token.
"""

from __future__ import annotations

import json
import os
import re
import threading
import urllib.parse
from datetime import datetime, timezone
from typing import Callable, Protocol


class Agents(Protocol):
    def rca_available(self) -> bool: ...
    def cost_available(self) -> bool: ...
    def request_rca(self, *, namespace: str, project: str, component: str, environment: str,
                    alert_id: str, alert_name: str, description: str, value, at: str) -> str: ...  # report id
    def rca_report(self, report_id: str) -> dict | None: ...  # {"status": pending|completed|failed, "report"}
    def request_cost(self, *, namespace: str, project: str, component: str, environment: str,
                     budget: float, actual: float, period: str, at: str) -> str: ...
    def cost_report(self, report_id: str) -> dict | None: ...


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


class HttpAgents:
    def __init__(self, rca_url: str = "", finops_url: str = "", auth: Callable[[], dict] | None = None,
                 opener=None) -> None:
        from urllib.request import build_opener

        self.rca_url, self.finops_url = rca_url.rstrip("/"), finops_url.rstrip("/")
        self.auth = auth or (lambda: {})
        self.opener = opener or build_opener()

    def rca_available(self) -> bool:
        return bool(self.rca_url)

    def cost_available(self) -> bool:
        return bool(self.finops_url)

    def _call(self, method: str, url: str, body: dict | None = None) -> dict:
        from urllib.request import Request

        req = Request(url, data=json.dumps(body).encode() if body is not None else None, method=method,
                      headers={"Accept": "application/json", **({"Content-Type": "application/json"} if body else {}),
                               **self.auth()})
        with self.opener.open(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8") or "{}")

    def request_rca(self, *, namespace, project, component, environment, alert_id, alert_name, description,
                    value, at) -> str:
        res = self._call("POST", f"{self.rca_url}/api/v1alpha1/rca-agent/analyze", {
            "namespace": namespace, "project": project, "component": component, "environment": environment,
            "alert": {"id": alert_id, "value": value if isinstance(value, (int, str)) else str(value), "timestamp": at,
                      "rule": {"name": alert_name, "description": description}},
            "meta": {"source": "vrixel-sdlc"}})
        return str(res.get("report_id") or res.get("reportId") or "")

    def _report(self, base: str, path: str, report_id: str) -> dict | None:
        if not re.fullmatch(r"[A-Za-z0-9._:-]{1,200}", report_id or ""):
            return None
        try:
            res = self._call("GET", f"{base}{path}{urllib.parse.quote(report_id)}")
        except Exception:
            return None
        return {"status": str(res.get("status") or "pending"), "report": res.get("report")}

    def rca_report(self, report_id: str) -> dict | None:
        return self._report(self.rca_url, "/api/v1/rca-agent/reports/", report_id)

    def request_cost(self, *, namespace, project, component, environment, budget, actual, period, at) -> str:
        res = self._call("POST", f"{self.finops_url}/api/v1alpha1/analyses", {
            "searchScope": {"component": component, "namespace": namespace, "project": project, "environment": environment},
            "budgetedCost": {"amount": float(budget), "period": period, "currency": "USD"},
            "actualCost": {"amount": float(actual), "currency": "USD"}, "budgetAlertTriggeredAt": at})
        return str(res.get("reportId") or res.get("report_id") or "")

    def cost_report(self, report_id: str) -> dict | None:
        return self._report(self.finops_url, "/api/v1alpha1/reports/", report_id)


class StubAgents:
    """For stub mode and tests. By default the root cause it finds is the incident's component,
    with high confidence, pointing at its latest release; `rca` (component -> report body) and
    `cost` (component -> report body) override what a request returns. `None` leaves it pending."""

    def __init__(self) -> None:
        self.rca: dict[str, dict | None] = {}
        self.cost: dict[str, dict | None] = {}
        self.requests: list[dict] = []
        self.reports: dict[str, dict | None] = {}
        self._lock = threading.Lock()

    def rca_available(self) -> bool:
        return True

    def cost_available(self) -> bool:
        return True

    def request_rca(self, *, namespace, project, component, environment, alert_id, alert_name, description, value,
                    at) -> str:
        with self._lock:
            rid = f"rca-{len(self.requests) + 1}"
            self.requests.append({"kind": "rca", "id": rid, "component": component, "environment": environment,
                                  "project": project, "alert_id": alert_id, "alert": alert_name})
            if component in self.rca:
                self.reports[rid] = self.rca[component]
            else:
                self.reports[rid] = {"status": "completed", "report": default_rca(component, alert_name)}
            return rid

    def rca_report(self, report_id: str) -> dict | None:
        with self._lock:
            got = self.reports.get(report_id)
        return {"status": "pending", "report": None} if got is None else got

    def request_cost(self, *, namespace, project, component, environment, budget, actual, period, at) -> str:
        with self._lock:
            rid = f"finops-{len(self.requests) + 1}"
            self.requests.append({"kind": "cost", "id": rid, "component": component, "environment": environment,
                                  "budget": budget, "actual": actual})
            self.reports[rid] = self.cost.get(component, {"status": "completed", "report": default_cost(component)})
            return rid

    def cost_report(self, report_id: str) -> dict | None:
        return self.rca_report(report_id)


def default_rca(component: str, alert: str) -> dict:
    return {"summary": f"{alert} on {component} began right after its latest release.",
            "result": {"type": "root_cause_identified", "root_causes": [{
                "summary": f"The latest release of {component} introduced the failure.",
                "confidence": "high", "analysis": "Errors start within minutes of the new release's rollout.",
                "supporting_findings": [{"observation": "Error rate rose after the deployment", "component": component}]}],
                "timeline": [{"timestamp": now(), "component": component, "event": f"New release of `{component}` deployed"}],
                "excluded_causes": [], "recommendations": {"recommended_actions": [
                    {"description": f"Roll back {component} to its previous release"}]}}}


def default_cost(component: str) -> dict:
    return {"summary": f"{component} is overprovisioned.",
            "overprovisioning": {"is_overprovisioned": True, "cpu_utilization_pct": 12, "memory_utilization_pct": 30,
                                 "analysis": "CPU stays under 15% of its request.",
                                 "recommendation": {"cpu_request": "250m", "cpu_limit": "500m", "memory_request": "256Mi",
                                                    "memory_limit": "512Mi", "estimated_savings": 42.0, "currency": "USD",
                                                    "rationale": "Peak usage over the period fits the smaller size."}}}


def from_env(settings, auth=None) -> Agents | None:
    """Live agents when their URLs are set; the stub in stub mode; otherwise none."""
    rca, finops = os.environ.get("VRIXEL_OPENCHOREO_RCA_URL", ""), os.environ.get("VRIXEL_OPENCHOREO_FINOPS_URL", "")
    if rca or finops:
        return HttpAgents(rca, finops, auth)
    return StubAgents() if getattr(settings, "mode", "") == "stub" else None

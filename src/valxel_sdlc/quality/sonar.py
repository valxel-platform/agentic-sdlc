"""SonarQube's quality gate and issues, as a quality suite (`source: sonarqube` in config/quality.yaml).

The scan itself runs in each repo's security.yml (the managed workflow in config/repos.yaml), on
pull requests and on the default branch. This source reads the result for the pull request:

    the quality gate   OK -> passed; ERROR -> failed, and the failed conditions are a finding at the
                       suite's block_on, so a failed gate always blocks
    the issues         open issues on the pull request's new code, each a finding with its file and
                       line, so the coder agent gets what to fix (not just "the gate failed")

`match`: {host, project_key}. project_key may use {owner} and {name} from the repo (SonarQube Cloud's
default when an org is imported from GitHub is "{owner}_{name}"). The token is VALXEL_SONAR_TOKEN in
the secret store; without it, or before the first analysis, the suite is missing (it blocks only
where a product made it required).
"""

from __future__ import annotations

import json
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from . import Finding, SuiteResult

# SonarQube's severities (classic and Clean Code impacts) to ours.
SEVERITY = {"BLOCKER": "critical", "CRITICAL": "high", "MAJOR": "medium", "MINOR": "low", "INFO": "info",
            "HIGH": "high", "MEDIUM": "medium", "LOW": "low"}


class SonarQube:
    def __init__(self, services, opener=None, token=None) -> None:
        self.services = services
        self._open = opener or (lambda req: urlopen(req, timeout=20))
        self._token = token

    def token(self) -> str:
        if self._token is not None:
            return self._token
        from ..secret_ring import get

        return get("VALXEL_SONAR_TOKEN")

    def _get(self, host: str, path: str, params: dict) -> dict:
        req = Request(f"{host.rstrip('/')}{path}?{urlencode(params)}",
                      headers={"Authorization": f"Bearer {self.token()}", "Accept": "application/json"})
        with self._open(req) as resp:
            return json.loads(resp.read() or b"{}")

    def collect(self, suite, spec, targets, stage):
        m = spec.get("match") or {}
        host = str(m.get("host") or "https://sonarcloud.io")
        if not host.startswith("https://"):
            raise ValueError("quality.code-quality.match.host must be an https URL")
        out = []
        for t in targets:
            owner, _, name = t["repo"].partition("/")
            key = str(m.get("project_key") or "{owner}_{name}").format(owner=owner, name=name)
            if not self.token():
                out.append(SuiteResult(suite=suite, kind=spec["kind"], stage=stage, state="missing", repo=t["repo"],
                                       message="VALXEL_SONAR_TOKEN isn't set"))
                continue
            scope = {"pullRequest": str(t["number"])} if t.get("number") else {"branch": t.get("ref") or "main"}
            gate = (self._get(host, "/api/qualitygates/project_status", {"projectKey": key, **scope})
                    .get("projectStatus") or {})
            status = str(gate.get("status") or "NONE").upper()
            url = f"{host.rstrip('/')}/project/overview?id={key}" + (
                f"&pullRequest={t['number']}" if t.get("number") else "")
            if status == "NONE":  # not analysed yet: keep waiting (the wait times out like any check)
                out.append(SuiteResult(suite=suite, kind=spec["kind"], stage=stage, state="pending", repo=t["repo"],
                                       url=url))
                continue
            findings = []
            if status == "ERROR":
                failed = [c for c in gate.get("conditions") or [] if str(c.get("status")).upper() == "ERROR"]
                what = "; ".join(f"{c.get('metricKey')} {c.get('actualValue')} (limit {c.get('errorThreshold')})"
                                 for c in failed) or "the quality gate failed"
                findings.append(Finding(suite=suite, tool="SonarQube", rule="quality-gate",
                                        severity=spec.get("block_on", "high"), title=f"Quality gate failed: {what}",
                                        repo=t["repo"], url=url, fingerprint=f"{t['repo']}:sonar:gate"))
                issues = self._get(host, "/api/issues/search", {"componentKeys": key, "resolved": "false", "ps": 100,
                                                                "inNewCodePeriod": "true", **scope}).get("issues") or []
                for i in issues:
                    impacts = [x.get("severity") for x in i.get("impacts") or [] if x.get("severity")]
                    sev = SEVERITY.get(str(i.get("severity") or (impacts[0] if impacts else "MAJOR")).upper(), "medium")
                    path = str(i.get("component") or "").split(":", 1)[-1]
                    where = f"{path}:{i['line']}" if i.get("line") else path
                    findings.append(Finding(suite=suite, tool="SonarQube", rule=str(i.get("rule") or ""), severity=sev,
                                            title=f"{i.get('message', '')} ({where})", repo=t["repo"], url=url,
                                            fingerprint=f"{t['repo']}:sonar:{i.get('key')}"))
            out.append(SuiteResult(suite=suite, kind=spec["kind"], stage=stage,
                                   state="failed" if status == "ERROR" else "passed", repo=t["repo"], url=url,
                                   findings=findings))
        return out

"""GitHub's security alerts, in one shape for the findings register.

Three kinds, each from its webhook event or its REST list (the nightly sweep):

    code        code scanning (CodeQL, Semgrep, Trivy: any SARIF tool)   code_scanning_alert
    dependency  Dependabot alerts (a vulnerable package in a manifest)   dependabot_alert
    secret      secret scanning (a credential committed to the repo)     secret_scanning_alert

    {id, repo, kind, tool, rule, severity, title, url, state: open|fixed|dismissed,
     path, package, patched, epss, validity, number}

The ids match the fingerprints the quality gates give the same alert inside a feature
(valxel_sdlc.quality.sources), so a risk accepted at a feature's security review is honoured here.
"""

from __future__ import annotations

from ..quality import norm_sev

EVENTS = {"code_scanning_alert": "code", "dependabot_alert": "dependency", "secret_scanning_alert": "secret"}

# What each event's action means for the finding. Unknown actions change nothing.
_CODE_STATES = {"created": "open", "reopened": "open", "reopened_by_user": "open", "appeared_in_branch": "open",
                "fixed": "fixed", "closed_by_user": "dismissed"}
_DEP_STATES = {"created": "open", "reopened": "open", "reintroduced": "open", "auto_reopened": "open",
               "fixed": "fixed", "dismissed": "dismissed", "auto_dismissed": "dismissed"}
_SECRET_STATES = {"created": "open", "reopened": "open", "publicly_leaked": "open", "validated": "open",
                  "resolved": "fixed"}


def code(repo: str, a: dict) -> dict:
    rule = a.get("rule") or {}
    inst = a.get("most_recent_instance") or {}
    state = {"open": "open", "fixed": "fixed", "dismissed": "dismissed"}.get(str(a.get("state") or "open"), "open")
    return {"id": f"{repo}:code-scanning:{a.get('number')}", "repo": repo, "kind": "code",
            "tool": (a.get("tool") or {}).get("name", "") or "code-scanning", "rule": str(rule.get("id", "")),
            "severity": norm_sev(rule.get("security_severity_level") or rule.get("severity") or "medium"),
            "title": rule.get("description") or rule.get("name") or str(rule.get("id", "")),
            "url": a.get("html_url", ""), "state": state, "path": (inst.get("location") or {}).get("path", ""),
            "package": "", "patched": "", "epss": None, "validity": "", "number": a.get("number")}


def dependency(repo: str, a: dict) -> dict:
    adv = a.get("security_advisory") or {}
    vuln = a.get("security_vulnerability") or {}
    dep = a.get("dependency") or {}
    pkg = (dep.get("package") or vuln.get("package") or {}).get("name", "")
    ghsa = adv.get("ghsa_id", "")
    epss = None
    for e in adv.get("epss") or []:
        if isinstance(e, dict) and e.get("percentage") is not None:
            epss = float(e["percentage"])
    state = str(a.get("state") or "open")
    return {"id": f"{repo}:{ghsa}:{pkg}", "repo": repo, "kind": "dependency", "tool": "dependabot", "rule": ghsa,
            "severity": norm_sev(vuln.get("severity") or adv.get("severity") or "medium"),
            "title": f"{pkg}: {adv.get('summary', '')}".strip(": "), "url": a.get("html_url", ""),
            "state": "open" if state == "open" else "fixed" if state == "fixed" else "dismissed",
            "path": dep.get("manifest_path", ""), "package": pkg,
            "patched": ((vuln.get("first_patched_version") or {}).get("identifier") or ""),
            "epss": epss, "validity": "", "number": a.get("number")}


def secret(repo: str, a: dict) -> dict:
    state = str(a.get("state") or "open")
    return {"id": f"{repo}:secret:{a.get('number')}", "repo": repo, "kind": "secret", "tool": "secret-scanning",
            "rule": a.get("secret_type", ""), "severity": "critical",
            "title": f"Exposed {a.get('secret_type_display_name') or a.get('secret_type') or 'secret'}",
            "url": a.get("html_url", ""), "state": "open" if state == "open" else "fixed",
            "path": "", "package": "", "patched": "", "epss": None,
            # active | inactive | unknown: whether the provider still accepts it (GitHub checks some)
            "validity": a.get("validity", "") or "", "publicly_leaked": bool(a.get("publicly_leaked")),
            "number": a.get("number")}


NORMALIZE = {"code": code, "dependency": dependency, "secret": secret}


def from_event(event_name: str, payload: dict) -> dict | None:
    """The finding a webhook delivery describes, with its new state; None when it isn't one we
    track (another event, a branch other than the default, an action that changes nothing)."""
    kind = EVENTS.get(event_name)
    alert = payload.get("alert") or {}
    repo = (payload.get("repository") or {}).get("full_name", "")
    if not kind or not alert or not repo:
        return None
    if kind == "code":
        default = (payload.get("repository") or {}).get("default_branch", "main")
        ref = payload.get("ref") or ""
        if ref and ref not in (f"refs/heads/{default}", default):
            return None  # an alert on a pull request's branch: the feature's own gates handle it
    states = {"code": _CODE_STATES, "dependency": _DEP_STATES, "secret": _SECRET_STATES}[kind]
    action = str(payload.get("action") or "")
    if action not in states:
        return None
    f = NORMALIZE[kind](repo, alert)
    f["state"] = states[action]
    return f

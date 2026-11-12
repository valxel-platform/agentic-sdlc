"""The built screens, checked in staging: an accessibility scan (axe) and screenshots of every screen
the approved UX spec names, shown at G6 beside the mockup G3 approved.

    staging deployed -> the tool asks the scanning repo (the product's acceptance repo, else its web
    app) to visit each screen: repository_dispatch 'vrixel-screens' with the screens and their routes
    -> its workflow opens each route with the staging test account, runs axe at desktop and phone
    widths, keeps the screenshots, and reports the check run 'screens / <feature> / staging':

        - S1 /checkin: passed; desktop: CLN-FEAT-7/staging/S1-desktop.png; phone: CLN-FEAT-7/staging/S1-phone.png
        - S1 serious color-contrast: Save button text is 3.1:1 against its background
        - S2 /ready: failed; desktop: CLN-FEAT-7/staging/S2-desktop.png

    The screenshots are committed to the scanning repo's 'vrixel-screens' branch; the tool reads them
    with its own token and serves them to the people at G6 (they're never public URLs).

    -> the suite 'screens' (config/quality.yaml): serious and critical violations of WCAG 2.1 AA block
       the release, like a failing test (VX-9); moderate and minor are listed

Staging holds synthetic data only, so screenshots carry no real person's record.
"""

from __future__ import annotations

import re

from .quality import Finding, SuiteResult

IMPACT = {"critical": "critical", "serious": "high", "moderate": "medium", "minor": "low"}
_SCREEN = re.compile(r"^\s*[-*]\s*(S\d+)\s+(/\S*|-)\s*:\s*(passed|failed)\b(.*)$", re.M | re.I)
_VIOLATION = re.compile(r"^\s*[-*]\s*(S\d+)\s+(critical|serious|moderate|minor)\s+([\w-]+)\s*:\s*(.+)$", re.M | re.I)
_SHOT = re.compile(r"(desktop|phone)\s*:\s*([^\s;,]+\.png)", re.I)
BRANCH = "vrixel-screens"  # where the scanning workflow keeps the screenshots; the tool serves them


def check_name(feature_id: str, stage: str) -> str:
    return f"screens / {feature_id} / {stage}"


def wanted(services, state: dict) -> list[dict]:
    """The screens to visit: the approved UX spec's, with their routes and apps."""
    from .agents.parse import parse_screen_apps, parse_screen_routes, parse_screens

    ref = (state.get("artifacts") or {}).get("UX")
    if not ref:
        return []
    ux = services.artifacts.load(ref)
    names, apps, routes = parse_screens(ux), parse_screen_apps(ux), parse_screen_routes(ux)
    return [{"id": s, "name": n, "app": apps.get(s, ""), "route": routes.get(s, "")} for s, n in names.items()]


def scan_repo(services, state: dict) -> tuple[str, str]:
    """Where the scan runs, and at which commit: the acceptance repo's merged commit when the change
    set has one, else the first web app the change built."""
    acc = (state.get("change_set") or {}).get("acceptance") or {}
    if acc.get("repo") and acc.get("sha"):
        return acc["repo"], acc["sha"]
    runs = (state.get("change_set") or {}).get("runs") or {}
    for t in state.get("tasks") or []:
        if t.get("role") == "consumer" and t["repo"] in runs:
            return t["repo"], runs[t["repo"]].get("sha", "")
    return "", ""


def targets(services, state: dict, stage: str) -> list[dict]:
    """The scan for this stage (staging only), when the approved UX spec has web screens with routes and
    there's a repo to run it from; the wait checker asks that repo to run it, with every component's URL."""
    if stage != "staging":
        return []
    shown = [s for s in wanted(services, state) if s["route"] and "mobile" not in (s["app"] or "")]
    repo, sha = scan_repo(services, state)
    if not shown or not repo:
        return []
    return [{"kind": "screens", "repo": repo, "sha": sha, "feature_id": state["feature_id"], "flag": state.get("flag", ""),
             "screens": shown}]


def result(suite: str, stage: str, target: dict, summary: str, state: str, url: str) -> SuiteResult:
    shots: dict[str, dict] = {}
    seen: dict[str, str] = {}
    for sid, route, outcome, rest in _SCREEN.findall(summary or ""):
        seen[sid] = outcome.lower()
        shots[sid] = {"route": route, "result": outcome.lower(), **{k.lower(): u for k, u in _SHOT.findall(rest)}}
    findings = [Finding(suite=suite, tool="axe", rule=rule, severity=IMPACT[impact.lower()], repo=target["repo"],
                        title=f"{sid}: {text.strip()}", url=url)
                for sid, impact, rule, text in _VIOLATION.findall(summary or "")]
    missing = [s["id"] if isinstance(s, dict) else s for s in target.get("screens") or []]
    missing = [s for s in missing if s not in seen]
    msg = f"not visited: {', '.join(missing)}" if missing else ""
    st = "failed" if (state == "failure" and not findings) or missing else "passed"
    return SuiteResult(suite=suite, kind="security", stage=stage, state=st, repo=target["repo"], url=url, message=msg,
                       findings=findings, details={"screens": shots, "missing": missing})


class ScreenScans:
    """Reads the check run 'screens / <feature> / <stage>' on the scanning repo's commit."""

    def __init__(self, services) -> None:
        self.services = services

    def collect(self, suite, spec, targets, stage):
        out = []
        for t in targets:
            name = check_name(t["feature_id"], stage)
            res = self.services.github.checks(repo=t["repo"], sha=t["sha"])
            mine = [c for c in res.get("checks", []) if c.get("name", "").lower() == name.lower()]
            if not mine or mine[-1]["state"] == "pending":
                out.append(SuiteResult(suite=suite, kind="security", stage=stage, state="pending", repo=t["repo"],
                                       message=f"waiting for {name}"))
                continue
            c = mine[-1]
            out.append(result(suite, stage, t, c.get("summary", ""), c["state"], c.get("url", "")))
        return out


def g6(state: dict) -> dict | None:
    """What G6 shows: each screen's mockup version (G3's) and its staging screenshots and violations."""
    ux_screens = (state.get("mockups") or {}).get("screens") or []
    if not ux_screens:
        return None
    q = ((state.get("quality") or {}).get("staging") or {}).get("suites") or []
    row = next((r for r in q if r.get("suite") == "screens"), None)
    shots = ((row or {}).get("details") or {}).get("screens") or {}
    findings = (row or {}).get("findings") or []
    return {"scanned": row is not None, "state": (row or {}).get("state", ""), "repo": (row or {}).get("repo", ""),
            "screens": [{**s, **({"staging": shots[s["id"]]} if s["id"] in shots else {}),
                         "violations": [f for f in findings if str(f.get("title", "")).startswith(f"{s['id']}:")]}
                        for s in ux_screens]}


def screenshot(services, state: dict, sid: str, width: str) -> bytes | None:
    """One screenshot from the last staging scan, read from the scanning repo's screenshots branch."""
    shown = g6(state) or {}
    shot = next((x.get("staging") or {} for x in shown.get("screens") or [] if x["id"] == sid), {})
    path, repo = shot.get(width), shown.get("repo")
    if not path or not repo or ".." in path or not re.fullmatch(r"[\w./-]+\.png", path):
        return None
    return services.github.get_bytes(repo=repo, path=path, ref=BRANCH)

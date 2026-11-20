"""Today's screens (T-91): before the G3 prototype is drawn, the product's own scan screenshots the
screens the UX spec names as they are today in staging, so the designer starts from the app as it
is and G3 shows what this feature changes.

    the UX spec passes -> wait "today_screens": the tool asks the product's scanning repo (its
    acceptance repo, else its web app) to visit each route of the spec in staging, at the default
    branch's commit -> its workflow (the same one that scans built screens before G6) keeps the
    screenshots on the repo's "valxel-screens" branch at <feature>/today/<screen>-<width>.png and
    reports "screens / <feature> / today" -> the designer sees each screenshot with the task and
    draws the prototype on top of it; a route that didn't load is a new screen

The screenshots stay in the product's repo: the tool reads them to pass to the model for that one
call and to show at G3, and keeps nothing. Staging holds synthetic data only. Accessibility findings
on today's screens are the app's as it is, not this feature's: they're not counted.

Skipped (the prototype is drawn from the spec alone) when the spec has no web screens with routes,
no repo of the product has the scan's workflow (WORKFLOW), nothing is deployed in staging, or the
scan doesn't report within WAIT_S.
"""

from __future__ import annotations

import logging

from .screens import _SCREEN, _SHOT, BRANCH, check_name, scans

log = logging.getLogger(__name__)

STAGE = "today"
WAIT_S = 20 * 60
SCAN_ROLES = ("acceptance", "consumer")  # where the scan can run from, in this order
URL_ROLES = ("bff", "consumer")  # what the scan opens: a web app is reached through its BFF (VX-17)
MAX_IMAGES = 12  # desktop shots of the first screens (phone ones when a screen is phone only)


def wanted(services, state: dict) -> list[dict]:
    """The spec's web screens with routes: what can be visited."""
    from .screens import wanted as spec_screens

    return [s for s in spec_screens(services, state) if s["route"] and s.get("kind") != "mobile"]


def plan(services, state: dict) -> dict | None:
    """The scan to ask for, or None when there's nothing to scan or nowhere to scan it from."""
    from .provisioning import component_name

    shown = wanted(services, state)
    if not shown:
        return None
    try:
        product = services.snapshot(state["product_id"]).product
    except Exception:  # noqa: BLE001 - governance unreadable now: draw from the spec
        return None
    repos = list(product.repos or [])
    repo = next((r.name for role in SCAN_ROLES for r in repos if r.role == role and scans(services, r.name)), "")
    if not repo:
        return None
    rt = product.runtime or {}
    env: dict[str, str] = {}
    for r in repos:
        if r.role not in URL_ROLES:
            continue
        try:
            d = services.openchoreo.deployment(component=component_name(product, r.name), env="staging",
                                               project=rt.get("openchoreo_project"), namespace=rt.get("namespace"))
        except Exception as exc:  # noqa: BLE001 - not deployed yet: nothing of it to show
            log.info("today's screens: %s not found in staging: %s", r.name, exc)
            continue
        if d.get("state") == "ready" and d.get("url"):
            env[r.name.split("/", 1)[-1]] = d["url"]
    if not env:
        return None
    try:
        sha = services.github.head_sha(repo=repo)
    except Exception as exc:  # noqa: BLE001 - can't ask it today: draw from the spec
        log.info("today's screens for %s: %s unreachable: %s", state["feature_id"], repo, exc)
        return None
    if not sha:
        return None
    return {"repo": repo, "sha": sha, "feature_id": state["feature_id"], "screens": shown, "env": env,
            "check": check_name(state["feature_id"], STAGE)}


def read(summary: str) -> dict[str, dict]:
    """The scan's report: {screen: {"route", "today": bool, "desktop": path, "phone": path}}."""
    import re

    out: dict[str, dict] = {}
    text = re.sub(r"\s-\s(?=S\d+\s)", "\n- ", summary or "")  # a check run's title runs into its first line
    for sid, route, outcome, rest in _SCREEN.findall(text):
        shots = {k.lower(): p for k, p in _SHOT.findall(rest)}
        out[sid] = {"route": route, "today": outcome.lower() == "passed" and bool(shots), **shots}
    return out


def images(services, today: dict, screens: list[dict]) -> list[dict]:
    """Today's screenshots for the designer, read from the product's repo for this call only."""
    out = []
    shots = today.get("screens") or {}
    for s in screens:
        shot = shots.get(s["id"]) or {}
        if not shot.get("today"):
            continue
        width = "desktop" if shot.get("desktop") else "phone"
        data = picture(services, today.get("repo", ""), shot.get(width, ""))
        if data:
            out.append({"label": f"{s['id']} ({s['name']}) as it is today, {width} width:", "media_type": "image/png",
                        "data": data})
        if len(out) >= MAX_IMAGES:
            break
    return out


def picture(services, repo: str, path: str) -> bytes | None:
    import re

    if not repo or not path or ".." in path or not re.fullmatch(r"[\w./-]+\.png", path):
        return None
    try:
        return services.github.get_bytes(repo=repo, path=path, ref=BRANCH)
    except Exception:  # noqa: BLE001 - one missing picture doesn't stop the drawing
        return None


def instruction(today: dict, screens: list[dict]) -> str:
    """What the designer is told about today's screens."""
    shots = today.get("screens") or {}
    have = [s for s in screens if (shots.get(s["id"]) or {}).get("today")]
    if not have:
        return ""
    new = [s for s in screens if not (shots.get(s["id"]) or {}).get("today")]
    lines = ["\n\nToday's screens are attached, screenshotted in staging: " + ", ".join(
        f"{s['id']} ({s['name']}) at {s['route']}" for s in have) + ". Draw those screens as they look today (layout, "
             "header, navigation, colours, type, the parts this feature doesn't touch) and change only what the spec "
             "changes, marked data-change=\"new\" or data-change=\"changed\". The data in them is synthetic: use your "
             "own example values."]
    if new:
        lines.append(" These screens don't exist yet: " + ", ".join(f"{s['id']} ({s['name']})" for s in new)
                     + ". Draw them to fit the others, and mark their sections data-change=\"new\".")
    return "".join(lines)

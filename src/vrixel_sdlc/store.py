"""Store apps (role `mobile`): released through the App Store and Google Play, not deployed.

A store app has no OpenChoreo component. Instead of a deployment, each stage dispatches
`vrixel-release` to the app's repo (template-mobile's release.yml) and waits for the commit status
it posts:

  stage       the repo builds and                                       status
  dev         installs a test build on an emulator, runs Maestro         release / dev (and integration / report)
  staging     the same against staging, then TestFlight and Play's      release / staging (and e2e / report)
              internal track for testers
  production  submits store builds; Play starts at 1% of users           release / production

After production the rollout widens step by step (`vrixel-rollout` with a fraction): each step
waits at least `min_hours` after the last, then the repo checks the release's crash-free users and,
if healthy, widens it, posting `rollout / <n>%`. Old versions stay installed for months, so the
BFF's minimum supported version and the API's expand-then-contract rule protect them.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from itertools import pairwise

STORE_ROLES = ("mobile",)
STAGES = ("dev", "staging", "production")
DEFAULT_ROLLOUT = {"steps": [0.1, 0.5, 1.0], "min_hours": 24, "step_timeout_hours": 96, "recheck_hours": 6}


def release_context(stage: str) -> str:
    return f"release / {stage}"


def percent(fraction: float) -> str:
    p = round(float(fraction) * 100, 2)
    return f"{int(p)}%" if p == int(p) else f"{p}%"


def rollout_context(fraction: float, attempt: int = 0) -> str:
    """One status per step (and per retry of it), so an earlier step's result never answers a later one."""
    return f"rollout / {percent(fraction)}" + (f" (attempt {attempt + 1})" if attempt else "")


def rollout_policy(standards: dict | None) -> dict:
    """The product's rollout for store apps (repo_standards.roles.mobile.rollout), checked: steps
    rise, end at 1 (everyone), and are at least an hour apart."""
    raw = (((standards or {}).get("roles") or {}).get("mobile") or {}).get("rollout") or {}
    policy = {**DEFAULT_ROLLOUT, **raw}
    steps = [float(s) for s in policy["steps"]]
    if not steps or steps[-1] != 1.0 or any(b <= a for a, b in pairwise(steps)) or steps[0] <= 0:
        raise ValueError(f"rollout steps must rise to 1.0, got {policy['steps']}")
    if float(policy["min_hours"]) < 1:
        raise ValueError("rollout min_hours must be at least 1")
    return {**policy, "steps": steps}


def not_before(last: str | None, min_hours: float) -> str:
    start = datetime.fromisoformat(last) if last else datetime.now(timezone.utc)
    return (start + timedelta(hours=float(min_hours))).isoformat()

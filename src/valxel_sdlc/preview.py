"""Try the change before it merges (T-93): the change set's environment, as a link.

Before G5 or the merge train, the pipeline already builds every pull request of the change set and
runs them together in their own environment (changeset_env.py, `cs-<feature>`): the real web app
and its backend, built from the branch, on the data plane of the product's first environment, with
its test data. This gives the product's people that environment's web app as a link, at G5 and on
the feature's page (a low-risk change has no G5), so they can use the change as it was built.

    the change set works together in cs-<feature> -> the web app's address there (through its BFF,
    as the screens and acceptance tests reach it, VX-17) -> "Try the change", until the change set
    merges or the environment's time to live (runtime.change_sets.ttl_hours) runs out

Nothing new runs and the tool keeps only the link: the environment is the product's own, its sign-in
is the app's own (its test identities), and its data is test data. No link when the change set has
no web app, its environment wasn't created or didn't pass, or it's gone.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta


def of(state: dict, ttl_hours: float, *, now: datetime | None = None) -> dict | None:
    """The link to try the change: {"url", "env", "expires_at"}; or {"gone": why} once it isn't there;
    or None when there's nothing to try."""
    env = (state.get("change_set") or {}).get("env") or {}
    roles = {t["repo"]: t.get("role", "") for t in state.get("tasks") or [] if t.get("repo")}
    if not env.get("name") or "consumer" not in roles.values():
        return None
    if env.get("deleted"):
        return {"gone": "The change set merged or stopped, and its environment was removed"}
    passed = ((state.get("delivery") or {}).get("change_set") or {})
    if passed.get("status") != "success":
        return None
    urls = {r["repo"]: r.get("url", "") for r in passed.get("results") or [] if r.get("url")}
    web = next((urls[r] for r, role in roles.items() if role == "bff" and urls.get(r)), "") or \
        next((urls[r] for r, role in roles.items() if role == "consumer" and urls.get(r)), "")
    if not web:
        return None
    now = now or datetime.now(UTC)
    try:
        expires = datetime.fromisoformat(env["created"]) + timedelta(hours=float(ttl_hours))
    except (KeyError, TypeError, ValueError):
        expires = None
    if expires is not None and expires <= now:
        return {"gone": f"Its environment ran out of time ({ttl_hours:g} hours) and was removed"}
    return {"url": web, "env": env["name"], "expires_at": expires.isoformat() if expires else ""}


def for_feature(services, state: dict) -> dict | None:
    from .changeset_env import config

    try:
        runtime = services.feature_snapshot(state).product.runtime
    except Exception:  # noqa: BLE001 - governance unreadable now: the default time to live
        runtime = None
    return of(state, config(runtime)["ttl_hours"])

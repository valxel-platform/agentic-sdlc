"""Strict mode (VALXEL_STRICT=1): the server refuses to start while anything that would quietly fall
back to an offline stand-in in production still does.

Most switches already fail loudly when misconfigured (VALXEL_MODE=live without a provider,
VALXEL_DELIVERY=live without the GitHub App). Some don't: a Jira or Lucid server without
credentials runs as a stand-in, a product's flags run in memory, a missing database keeps
everything in memory. Strict mode turns each of those into a startup error, so a typo in a
token can't make G6 reconcile the matrix against a fake tracker.

`stand_ins` returns what blocks; `known_gaps` returns what runs but can't be checked, and only warns."""

from __future__ import annotations

import logging
import os

log = logging.getLogger(__name__)


class StrictModeError(RuntimeError):
    pass


def enabled(env=None) -> bool:
    return (env if env is not None else os.environ).get("VALXEL_STRICT", "").lower() in ("1", "true", "yes", "on")


def stand_ins(services, *, auth_mode: str, env=None) -> list[str]:
    """Each setting or product tool that would run as an offline stand-in."""
    from .services import flag_modes, integration_modes

    env = env if env is not None else os.environ
    s = services.settings
    out = []
    if s.mode != "live":
        out.append("VALXEL_MODE is not live: agents return canned output")
    if env.get("VALXEL_DELIVERY", "stub").lower() != "live":
        out.append("VALXEL_DELIVERY is not live: pull requests, builds and deployments stay in memory")
    if auth_mode != "github":
        out.append("VALXEL_AUTH is not github: anyone can sign in as anyone")
    if s.governance_source != "github":
        out.append("VALXEL_GOVERNANCE_SOURCE is not github: governance is read from local files")
    if not s.database_url:
        out.append("DATABASE_URL is not set: features, the catalog and the audit trail are lost on restart")
    out += weak_secrets(env, database_url=s.database_url)
    out += secrets_in_environment(env)
    secret_env = _secret_env(env)
    modes = integration_modes(services)
    for pid in sorted(s.registry):
        try:
            product = services.snapshot(pid).product
        except Exception as exc:  # governance errors are reported elsewhere; a broken product isn't a stand-in
            out.append(f"{pid}: governance doesn't load ({exc})")
            continue
        tools = product.tools or {}
        for cap in ("diagrams", "work_tracking"):
            provider = (tools.get(cap) or {}).get("provider")
            if provider and modes.get(provider, "stub") == "stub":
                out.append(f"{pid}: tools.{cap} names {provider}, which has no server URL or credentials")
        if product.runtime and tools.get("flags"):
            if flag_modes(services, [pid]).get(pid) != "flipt":
                out.append(f"{pid}: tools.flags names Flipt, but its flags run in memory "
                           f"(set VALXEL_FLIPT_TOKEN_{pid.upper()} and VALXEL_DELIVERY=live)")
        cm = tools.get("change_management") or {}
        if cm.get("provider") == "servicenow" and not secret_env.get(f"VALXEL_SERVICENOW_AUTH_{pid.upper()}"):
            out.append(f"{pid}: tools.change_management names ServiceNow, but VALXEL_SERVICENOW_AUTH_{pid.upper()} is not set")
    return out


def _secret_env(env):
    from .secret_ring import environ

    return environ(env)


def secrets_in_environment(env) -> list[str]:
    """With a secret store chosen, a secret left in the environment is readable by anything that
    can read the process's environment: refuse it (valxel_sdlc.secret_ring)."""
    from .secret_ring import in_environment, store_kind

    try:
        kind = store_kind(env)
    except ValueError as exc:
        return [str(exc)]
    if kind == "env":
        return []
    names = in_environment(env)
    return [f"VALXEL_SECRET_STORE is {kind}, but these secrets are also in the environment: {', '.join(names)}; "
            "keep them only in the store"] if names else []


def weak_secrets(env, *, database_url: str = "") -> list[str]:
    """Secrets set to published examples or too weak to protect anything (valxel_sdlc.secret_ring)."""
    from urllib.parse import urlsplit

    from .secret_ring import values, weak, weak_password

    out = []
    for i, key in enumerate(values("VALXEL_SESSION_SECRET", env)):
        why = weak(key)
        if why:
            which = "VALXEL_SESSION_SECRET" if i == 0 else "VALXEL_SESSION_SECRET's previous value"
            out.append(f"{which} is {why}: anyone who knows it can sign in as anyone and approve any gate")
    for name in ("VALXEL_GITHUB_WEBHOOK_SECRET", "VALXEL_SIGNALS_TOKEN", "VALXEL_SENTRY_CLIENT_SECRET"):
        for key in values(name, env):
            why = weak(key, min_length=20)
            if why:
                out.append(f"{name} is {why}")
    if database_url:
        try:
            password = urlsplit(database_url).password or ""
        except ValueError:
            password = ""
        why = weak_password(password) if password else ""  # none: IAM auth, a .pgpass or a socket
        if why:
            out.append(f"DATABASE_URL's password is {why}")
    return out


def env_value(name: str) -> str:
    return os.environ.get(name, "")


def known_gaps(services) -> list[str]:
    """What runs, but can't be checked: strict mode warns about these and starts anyway."""
    out = []
    from .secret_ring import store_kind

    try:
        if services.settings.mode == "live" and store_kind() == "env":
            out.append("VALXEL_SECRET_STORE is not set: secrets come from environment variables, which anything that "
                       "can read the process's environment can read; use aws or files")
    except ValueError:
        pass  # stand_ins reports a bad VALXEL_SECRET_STORE
    if not env_value("VALXEL_CONFIG_REPO"):
        out.append("VALXEL_CONFIG_REPO is not set: changes to models and settings aren't evaluated against past "
                   "work before they merge")
    s = services.settings
    pinned = getattr(getattr(s, "plugins", None), "ref", "")
    if pinned and s.mode == "live":
        from .plugins import installed_ref

        have = installed_ref(s.skills_dir or env_value("VALXEL_SKILLS_DIR"))
        if have and have != pinned:  # unmarked: a local checkout's plugins, not an image's
            out.append(f"config/plugins.yaml pins knowledge-work-plugins at {pinned[:12]}, but the installed plugins "
                       f"are at {have[:12]}: the agents follow the installed ones until the image is rebuilt from "
                       "the config's pin")
    tel = getattr(services, "telemetry", None)
    if tel is None or not tel.enabled:
        out.append("VALXEL_OTEL_ENDPOINT is not set: agent sessions aren't traced, so a session can't be "
                   "looked into from the feature page or G5/G6")
    for pid, entry in sorted(services.settings.registry.items()):
        if entry.kind != "shared":
            continue
        try:
            repos = services.snapshot(pid).product.repos
        except Exception:
            continue
        unchecked = [r.name for r in repos if r.role == "library" and not r.package]
        if unchecked:
            out.append(f"{pid}: {', '.join(unchecked)} declare no package, so G6 can't check that a release "
                       "candidate was published to GitHub Packages")
    return out


def check(services, *, auth_mode: str, env=None) -> list[str]:
    """Raise StrictModeError listing every stand-in; return the known gaps (already logged)."""
    problems = stand_ins(services, auth_mode=auth_mode, env=env)
    if problems:
        raise StrictModeError("VALXEL_STRICT is on and these would run as offline stand-ins:\n- "
                              + "\n- ".join(problems))
    gaps = known_gaps(services)
    for gap in gaps:
        log.warning("strict mode: %s", gap)
    return gaps

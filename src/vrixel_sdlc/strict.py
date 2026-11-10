"""Strict mode (VRIXEL_STRICT=1): the server refuses to start while anything that would quietly fall
back to an offline stand-in in production still does.

Most switches already fail loudly when misconfigured (VRIXEL_MODE=live without a provider,
VRIXEL_DELIVERY=live without the GitHub App). Some don't: a Jira or Lucid server without
credentials runs as a stand-in, a product's flags run in memory, a missing database keeps
everything in memory. Strict mode turns each of those into a startup error, so a typo in a
token can't make G6 reconcile the matrix against a fake tracker.

`stand_ins` returns what blocks; `known_gaps` returns stand-ins that have no live version yet and
only warn (see "Replacing the stubs" in the README)."""

from __future__ import annotations

import logging
import os

log = logging.getLogger(__name__)


class StrictModeError(RuntimeError):
    pass


def enabled(env=None) -> bool:
    return (env if env is not None else os.environ).get("VRIXEL_STRICT", "").lower() in ("1", "true", "yes", "on")


def stand_ins(services, *, auth_mode: str, env=None) -> list[str]:
    """Each setting or product tool that would run as an offline stand-in."""
    from .services import flag_modes, integration_modes

    env = env if env is not None else os.environ
    s = services.settings
    out = []
    if s.mode != "live":
        out.append("VRIXEL_MODE is not live: agents return canned output")
    if env.get("VRIXEL_DELIVERY", "stub").lower() != "live":
        out.append("VRIXEL_DELIVERY is not live: pull requests, builds and deployments stay in memory")
    if auth_mode != "github":
        out.append("VRIXEL_AUTH is not github: anyone can sign in as anyone")
    if s.governance_source != "github":
        out.append("VRIXEL_GOVERNANCE_SOURCE is not github: governance is read from local files")
    if not s.database_url:
        out.append("DATABASE_URL is not set: features, the catalog and the audit trail are lost on restart")
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
                           f"(set VRIXEL_FLIPT_TOKEN_{pid.upper()} and VRIXEL_DELIVERY=live)")
        cm = tools.get("change_management") or {}
        if cm.get("provider") == "servicenow" and not env.get(f"VRIXEL_SERVICENOW_AUTH_{pid.upper()}"):
            out.append(f"{pid}: tools.change_management names ServiceNow, but VRIXEL_SERVICENOW_AUTH_{pid.upper()} is not set")
    return out


def known_gaps(services) -> list[str]:
    """Stand-ins with no live version yet: strict mode warns about these and starts anyway."""
    from .integrations.packages import StubPackageRegistry

    out = []
    shared = sorted(pid for pid, e in services.settings.registry.items() if e.kind == "shared")
    if shared and isinstance(services.packages, StubPackageRegistry):
        out.append(f"shared lanes {', '.join(shared)} record release channels in memory: "
                   "there is no live package registry yet")
    return out


def check(services, *, auth_mode: str, env=None) -> list[str]:
    """Raise StrictModeError listing every stand-in; return the known gaps (already logged)."""
    problems = stand_ins(services, auth_mode=auth_mode, env=env)
    if problems:
        raise StrictModeError("VRIXEL_STRICT is on and these would run as offline stand-ins:\n- "
                              + "\n- ".join(problems))
    gaps = known_gaps(services)
    for gap in gaps:
        log.warning("strict mode: %s", gap)
    return gaps

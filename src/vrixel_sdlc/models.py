"""Which model does a role's work: the role's own, a fallback when its provider is unavailable,
and a second opinion from another model on reviews.

Every role names a runtime (claude_code, codex, generic) and a model (`<provider>/<name>`), so a
stage's contract (its prompt layers, its JSON reply, its caps) stays the same whichever vendor runs
it. Roles that are only fallbacks or second opinions are optional: if their provider isn't
configured (no URL or key) or their model has no price, they are left out with a warning
instead of stopping the tool.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from .config import Settings
from .gateway.pricing import has_price

log = logging.getLogger(__name__)

RUNTIMES = ("claude_code", "codex", "generic")
BUILT_IN_PROVIDERS = ("anthropic", "stub")


def optional_roles(settings: Settings) -> set[str]:
    """Roles that are only another role's fallback or second opinion."""
    used = set()
    for role in settings.roles.values():
        used |= set(role.fallback) | set(role.second_opinion)
    return used


def validate(settings: Settings) -> None:
    """Configuration mistakes that would only show up mid-feature: fail at load instead."""
    for name, role in settings.roles.items():
        if role.runtime not in RUNTIMES:
            raise ValueError(f"role {name}: runtime {role.runtime!r} is not one of {', '.join(RUNTIMES)}")
        for other in (*role.fallback, *role.second_opinion):
            if other not in settings.roles:
                raise ValueError(f"role {name}: {other!r} is not a role in models.yaml")
            if other == name:
                raise ValueError(f"role {name} names itself as its fallback or second opinion")
        if (role.provider not in BUILT_IN_PROVIDERS and role.provider not in settings.providers
                and not (role.runtime == "codex" and role.provider == "openai")):
            raise ValueError(f"role {name}: provider {role.provider!r} has no entry under providers in models.yaml")


def provider_ready(settings: Settings, provider: str, runtime: str = "generic") -> bool:
    if provider in BUILT_IN_PROVIDERS:
        return True
    pc = settings.providers.get(provider)
    if pc is not None:
        return pc.configured()
    import os
    return runtime == "codex" and provider == "openai" and bool(os.environ.get("CODEX_API_KEY") or os.environ.get("OPENAI_API_KEY"))


def available_roles(settings: Settings) -> set[str]:
    """Roles the tool can run. In stub mode, all of them. In live mode, a role whose provider isn't
    configured is left out if it is optional; otherwise the tool refuses to start. Every model
    needs a price, so budgets hold whichever vendor runs the work."""
    if settings.mode != "live":
        return set(settings.roles)
    optional = optional_roles(settings)
    out, problems = set(), []
    for name, role in settings.roles.items():
        if not provider_ready(settings, role.provider, role.runtime):
            if name in optional:
                log.warning("role %s is off: provider %s is not configured", name, role.provider)
                continue
            problems.append(f"{name}: provider {role.provider} is not configured")
            continue
        if not has_price(role.model, settings.prices):
            if name in optional:
                log.warning("role %s is off: no price for %s under prices in models.yaml", name, role.model)
                continue
            problems.append(f"{name}: no price for {role.model} (add it under prices in models.yaml)")
            continue
        out.add(name)
    if problems:
        raise RuntimeError("Model configuration: " + "; ".join(problems))
    return out


@dataclass(frozen=True)
class Choice:
    role: str | None
    skipped: str = ""  # why there is none, when role is None


def chain(settings: Settings, available: set[str], role: str, data_class: str) -> list[str]:
    """The role, then its fallbacks that can run and may see this data class, in order."""
    out = [role]
    for fb in settings.role(role).fallback:
        if fb in available and _allowed(settings, settings.role(fb).provider, data_class):
            out.append(fb)
    return out


def second_opinion(settings: Settings, available: set[str], role: str, data_class: str,
                   reviewed_by: str | None = None) -> Choice:
    """The first of the role's second-opinion roles that can run, may see this data class and
    uses a different model from the one that did the review (`reviewed_by`, when a fallback did)."""
    options = settings.role(role).second_opinion if role in settings.roles else ()
    first = settings.role(reviewed_by or role).model
    why = []
    for other in options:
        if settings.role(other).model == first:
            why.append(f"{other}: same model as the review")
        elif other not in available:
            why.append(f"{other}: provider not configured")
        elif not _allowed(settings, settings.role(other).provider, data_class):
            why.append(f"{other}: {data_class} data may not reach {settings.role(other).provider}")
        else:
            return Choice(other)
    return Choice(None, "; ".join(why))


def _allowed(settings: Settings, provider: str, data_class: str) -> bool:
    allowed = settings.data_classes.get(data_class, [])
    return "*" in allowed or provider in allowed

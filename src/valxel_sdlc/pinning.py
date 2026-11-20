"""Every model the tool uses is pinned: the name can't start meaning another model (T-84).

A provider can point a name at a newer model whenever it likes, and the agents' quality moves with
it: the most common cause of silent drift. A pinned name stays the same model until it's retired,
with notice. So:

  anthropic   Claude IDs from the 4.6 generation on are pinned as written ("each model ID identifies
              a pinned version"); older ones need their dated snapshot (claude-sonnet-4-5-20250929).
              Anything ending in -latest is an alias.
  openai      a dated snapshot only (gpt-5.4-2026-03-05): the undated name is an alias.
  others      a self-hosted server runs whatever weights it was started with, so its provider entry
              in models.yaml says which: `pinned:` a weights revision or image digest. A provider
              with no URL set isn't in use, and isn't checked.

Strict mode refuses a live config with an unpinned model (strict.stand_ins). Prices are looked up
by the undated name too (gateway.pricing), so pinning doesn't lose them.
"""

from __future__ import annotations

import re

DATED = re.compile(r"-(\d{4}-\d{2}-\d{2}|\d{8})$")
_CLAUDE = re.compile(r"^claude-[a-z]+-(\d+)(?:-(\d+))?$")
CLAUDE_PINNED_FROM = (4, 6)


def why_unpinned(model: str, providers: dict) -> str:
    """Why `model` ("<provider>/<name>") isn't pinned; "" when it is."""
    provider, _, name = model.partition("/")
    if not name:
        return f"{model}: no provider prefix"
    if name.endswith(("-latest", ":latest")):
        return f"{model} is an alias that moves to each new release"
    if provider == "anthropic":
        if DATED.search(name):
            return ""
        m = _CLAUDE.match(name)
        if m and (int(m.group(1)), int(m.group(2) or 0)) >= CLAUDE_PINNED_FROM:
            return ""
        return f"{model}: name the dated snapshot (Claude IDs before 4.6 are aliases)"
    if provider == "openai":
        return "" if DATED.search(name) else f"{model} is an alias: name the dated snapshot (e.g. {name}-YYYY-MM-DD)"
    p = providers.get(provider)
    if p is None:
        return f"{model}: no provider {provider} in models.yaml"
    if not p.url():
        return ""  # not in use here
    if not getattr(p, "pinned", ""):
        return (f"{model}: provider {provider} doesn't say which weights it serves: set `pinned:` (a weights "
                "revision or image digest) on it in models.yaml")
    return ""


def unpinned(settings) -> list[str]:
    """One line per role whose model isn't pinned."""
    out = []
    for role, rc in sorted(settings.roles.items()):
        why = why_unpinned(rc.model, settings.providers)
        if why:
            out.append(f"role {role}: {why}")
    return out


def undated(name: str) -> str:
    """The name without its snapshot date, for looking up prices and context windows."""
    return DATED.sub("", name)

"""The template contract: what every role's template provides (config/template-contract.yaml).

Security, observability, the audit trail and business metrics come with a repo's template, not
with each feature, so a template without them makes every repo of its role ship without them.
The contract says which capabilities each role needs; each template says where it implements
them, in `.valxel/capabilities.yaml` (`<capability>: <path>`). `valxel repos check` reads that
from every template in platform_repos (`check`), so a template that falls behind, or a new one
that never caught up, shows up in the daily repo-standards run.

The tool's own rules read the same list (`roles_with`): who gets the feature-flag and audit
instructions, which repos may record a PRD's Instrumentation events, and which roles report a
post-deploy suite after each deploy.
"""

from __future__ import annotations

import re
from functools import cache
from pathlib import Path

import yaml

DECLARATIONS = ".valxel/capabilities.yaml"
AREAS = ("security", "observability", "audit", "business-metrics")


class ContractError(ValueError):
    pass


def parse(text: str) -> dict:
    raw = yaml.safe_load(text or "") or {}
    groups = {k: list(v) for k, v in (raw.get("groups") or {}).items()}
    caps = {}
    for name, c in (raw.get("capabilities") or {}).items():
        if c.get("area") not in AREAS:
            raise ContractError(f"{name}: area must be one of {AREAS}")
        roles = c.get("roles")
        roles = groups.get(roles) if isinstance(roles, str) else roles
        if not roles:
            raise ContractError(f"{name}: roles must be a list or a group ({', '.join(groups)})")
        try:
            re.compile(str(c.get("marker") or ""))
        except re.error as exc:
            raise ContractError(f"{name}: marker isn't a regular expression: {exc}") from exc
        if not c.get("marker"):
            raise ContractError(f"{name}: needs a marker")
        caps[name] = {"area": c["area"], "roles": tuple(roles), "says": str(c.get("says") or ""),
                      "marker": str(c["marker"])}
    return {"capabilities": caps, "groups": groups}


@cache
def contract() -> dict:
    """The tool's contract: config/template-contract.yaml in the config directory (VALXEL_CONFIG_DIR,
    as for the rest of the config)."""
    import os

    from .config import _repo_root, config_file

    cfg = Path(os.environ.get("VALXEL_CONFIG_DIR") or _repo_root() / "config")
    return parse(config_file(cfg, "template-contract.yaml").read_text())


def required(role: str, c: dict | None = None) -> dict[str, dict]:
    c = c or contract()
    return {n: cap for n, cap in c["capabilities"].items() if role in cap["roles"]}


def roles_with(capability: str, c: dict | None = None) -> tuple[str, ...]:
    c = c or contract()
    return tuple((c["capabilities"].get(capability) or {}).get("roles") or ())


def check(gh, repo: str, role: str, c: dict | None = None) -> list[dict]:
    """What the template `repo` of `role` is missing: [{capability, area, problem, says}]."""
    c = c or contract()
    wanted = required(role, c)
    if not wanted:
        return []
    raw = gh.get_file(repo=repo, path=DECLARATIONS, ref="")
    try:
        declared = yaml.safe_load(raw or "") or {}
    except yaml.YAMLError:
        declared = {}
    if not isinstance(declared, dict):
        declared = {}
    out, texts = [], {}
    for name, cap in sorted(wanted.items()):
        path = declared.get(name)
        if not path:
            problem = f"not declared in {DECLARATIONS}" if raw is not None else f"{DECLARATIONS} is missing"
            out.append({"capability": name, "area": cap["area"], "problem": problem, "says": cap["says"]})
            continue
        if path not in texts:
            texts[path] = gh.get_file(repo=repo, path=str(path), ref="")
        if texts[path] is None:
            out.append({"capability": name, "area": cap["area"], "problem": f"{path} doesn't exist", "says": cap["says"]})
        elif not re.search(cap["marker"], texts[path]):
            out.append({"capability": name, "area": cap["area"], "says": cap["says"],
                        "problem": f"{path} doesn't contain /{cap['marker']}/"})
    return out


def matrix(gh, templates: dict[str, str], c: dict | None = None) -> dict[str, dict[str, str]]:
    """template -> capability -> 'ok' | 'missing' | 'n/a', for a report across all templates."""
    c = c or contract()
    out = {}
    for repo, role in sorted(templates.items()):
        gaps = {g["capability"] for g in check(gh, repo, role, c)}
        need = required(role, c)
        out[repo] = {n: ("n/a" if n not in need else "missing" if n in gaps else "ok") for n in c["capabilities"]}
    return out

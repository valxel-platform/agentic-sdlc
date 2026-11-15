"""Scan upkeep: what the tool does about findings on a product's default branch, after merge.

Inside a feature, the quality gates (valxel_sdlc.quality) scan each change and the agents fix what
blocks it. This package covers what turns up later, on `main`: a CVE disclosed against a package
already merged, a new CodeQL rule that flags old code, a secret that slipped past push protection.

    GitHub alerts (webhooks + a nightly sweep)  ->  the findings register (register.py)
        -> triage, by policy:  hotfix now | scheduled (fix now, release with the next release
           inside the deadline, else on its own just before it) | backlog (no deadline)
        -> a fix feature per repo (the hotfix track: starts at planning; G5 and G6 stay people's)
        -> the alert closes on main: fixed

A leaked secret opens an incident instead (on-call, rotation), and Dependabot's routine version
updates merge by themselves when they're patch or minor and green (service.py).

The policy is `upkeep:` in config/quality.yaml, tightened per data class (`floors`) and per product
(product.yaml `upkeep:`). Products may tighten, never loosen. Moving a deadline is accepting a risk:
security approvers decide, with a reason and an expiry (the same rule as waivers in a feature).
"""

from __future__ import annotations

import copy
from datetime import UTC, date, datetime, timedelta, timezone

from ..quality import SEVERITIES, sev_rank

OUTCOMES = ("hotfix", "scheduled", "backlog")
UPDATE_TYPES = ("patch", "minor", "major")

DEFAULTS: dict = {
    # Days from when a finding is first seen to when its fix must be in production. A severity
    # without a number (info) has no deadline: it's recorded, and fixed when a feature touches it.
    "fix_within_days": {"critical": 7, "high": 30, "medium": 90, "low": 180},
    "hotfix": {
        "severity": "critical",          # at or above: a hotfix now
        "exploit_likely": {"epss": 0.1, "severity": "high"},  # EPSS at or above, from this severity: a hotfix
        "secrets": True,                 # an exposed secret is always urgent (an incident: service.py)
    },
    "release_lead_days": 2,              # a held fix goes to its own G6 this many days before its deadline
    "max_open_fixes": 5,                 # fix features per product at once; the rest wait their turn
    "dependabot_grace_hours": 24,        # a dependency finding waits this long for Dependabot's own PR
    "dependabot": {"auto_merge": ["patch", "minor"]},  # version updates that merge on green checks
}


class UpkeepPolicyError(ValueError):
    pass


def load(raw: dict | None) -> dict:
    """The platform's policy (quality.yaml `upkeep:`) over the defaults, floors kept apart."""
    raw = dict(raw or {})
    floors = {k: dict(v or {}) for k, v in (raw.pop("floors", None) or {}).items()}
    problems: list[str] = []
    pol = _merge(copy.deepcopy(DEFAULTS), raw, problems, base=None, where="upkeep")
    if problems:
        raise UpkeepPolicyError("; ".join(problems))
    return {**pol, "floors": floors}


def effective(platform: dict | None, data_class: str, overrides: dict | None,
              problems: list[str] | None = None) -> dict:
    """The platform's policy, tightened by the data-class floor, then by the product."""
    problems = problems if problems is not None else []
    platform = platform or load(None)
    base = {k: v for k, v in platform.items() if k != "floors"}
    floor = (platform.get("floors") or {}).get(data_class) or {}
    pol = _merge(copy.deepcopy(base), floor, problems, base=None, where=f"upkeep.floors.{data_class}")
    if overrides:
        if not isinstance(overrides, dict):
            problems.append("upkeep must be a mapping")
            return pol
        pol = _merge(pol, overrides, problems, base=copy.deepcopy(pol), where="upkeep")
    return pol


def _merge(pol: dict, raw: dict, problems: list[str], *, base: dict | None, where: str) -> dict:
    """Apply `raw` over `pol`. With `base`, anything looser than it is a problem (a product's
    override); without, values are taken as given (the platform's own config and floors)."""
    unknown = set(raw) - set(DEFAULTS)
    if unknown:
        problems.append(f"{where} has unknown keys {sorted(unknown)}")
    days = raw.get("fix_within_days")
    if days is not None:
        if not isinstance(days, dict) or any(k not in SEVERITIES for k in days):
            problems.append(f"{where}.fix_within_days must map severities ({', '.join(SEVERITIES)}) to days")
        else:
            for sev, n in days.items():
                if not isinstance(n, int) or n < 1:
                    problems.append(f"{where}.fix_within_days.{sev} must be a whole number of days, 1 or more")
                    continue
                have = (base or pol)["fix_within_days"].get(sev)
                if base is not None and have is not None and n > have:
                    problems.append(f"{where}.fix_within_days.{sev} {n} is longer than the platform's {have}")
                    continue
                # A data-class floor only shortens the platform's deadline; the platform sets its own.
                floor = base is None and where.startswith("upkeep.floors")
                pol["fix_within_days"][sev] = min(n, have) if floor and have is not None else n
    hot = raw.get("hotfix")
    if hot is not None:
        if not isinstance(hot, dict):
            problems.append(f"{where}.hotfix must be a mapping")
        else:
            cur = pol["hotfix"]
            if "severity" in hot:
                if str(hot["severity"]).lower() not in SEVERITIES:
                    problems.append(f"{where}.hotfix.severity must be one of {list(SEVERITIES)}")
                elif base is not None and sev_rank(hot["severity"]) > sev_rank(base["hotfix"]["severity"]):
                    problems.append(f"{where}.hotfix.severity {hot['severity']} is looser than the platform's "
                                    f"{base['hotfix']['severity']}")
                else:
                    cur["severity"] = str(hot["severity"]).lower()
            if "secrets" in hot:
                if base is not None and not hot["secrets"] and base["hotfix"]["secrets"]:
                    problems.append(f"{where}.hotfix.secrets can't be turned off")
                else:
                    cur["secrets"] = bool(hot["secrets"])
            ex = hot.get("exploit_likely")
            if ex is not None:
                if not isinstance(ex, dict):
                    problems.append(f"{where}.hotfix.exploit_likely must be {{epss, severity}}")
                else:
                    if "epss" in ex:
                        if not isinstance(ex["epss"], (int, float)) or not 0 < float(ex["epss"]) <= 1:
                            problems.append(f"{where}.hotfix.exploit_likely.epss must be a share between 0 and 1")
                        elif base is not None and float(ex["epss"]) > float(base["hotfix"]["exploit_likely"]["epss"]):
                            problems.append(f"{where}.hotfix.exploit_likely.epss is looser than the platform's")
                        else:
                            cur["exploit_likely"]["epss"] = float(ex["epss"])
                    if "severity" in ex:
                        if str(ex["severity"]).lower() not in SEVERITIES:
                            problems.append(f"{where}.hotfix.exploit_likely.severity must be one of {list(SEVERITIES)}")
                        elif base is not None and sev_rank(ex["severity"]) > sev_rank(base["hotfix"]["exploit_likely"]["severity"]):
                            problems.append(f"{where}.hotfix.exploit_likely.severity is looser than the platform's")
                        else:
                            cur["exploit_likely"]["severity"] = str(ex["severity"]).lower()
    for key, tighter in (("release_lead_days", max), ("dependabot_grace_hours", min)):
        if key in raw:
            v = raw[key]
            if not isinstance(v, int) or v < 0:
                problems.append(f"{where}.{key} must be a whole number, 0 or more")
            elif base is not None and tighter(v, base[key]) != v:
                problems.append(f"{where}.{key} {v} is looser than the platform's {base[key]}")
            else:
                pol[key] = v
    if "max_open_fixes" in raw:
        v = raw["max_open_fixes"]
        if not isinstance(v, int) or v < 1:
            problems.append(f"{where}.max_open_fixes must be a whole number, 1 or more")
        else:
            pol["max_open_fixes"] = v
    dep = raw.get("dependabot")
    if dep is not None:
        types = (dep or {}).get("auto_merge") if isinstance(dep, dict) else None
        if not isinstance(types, list) or any(t not in UPDATE_TYPES for t in types):
            problems.append(f"{where}.dependabot.auto_merge must list update types from {list(UPDATE_TYPES)}")
        elif base is not None and set(types) - set(base["dependabot"]["auto_merge"]):
            problems.append(f"{where}.dependabot.auto_merge may only narrow the platform's "
                            f"{base['dependabot']['auto_merge']}")
        else:
            pol["dependabot"] = {"auto_merge": list(types)}
    return pol


# --------------------------------------------------------------------------- decisions


def _day(value) -> date:
    if isinstance(value, datetime):
        return value.astimezone(UTC).date()
    if isinstance(value, date):
        return value
    s = str(value or "")
    return datetime.fromisoformat(s.replace("Z", "+00:00")).date() if "T" in s else date.fromisoformat(s[:10])


def deadline(pol: dict, severity: str, first_seen) -> str:
    """The date the fix must be in production by, or "" when the severity has none."""
    days = (pol.get("fix_within_days") or {}).get(severity)
    return (_day(first_seen) + timedelta(days=int(days))).isoformat() if days else ""


def release_by(pol: dict, due: str) -> str:
    """When a held fix stops waiting for a release and goes to its own G6."""
    return (_day(due) - timedelta(days=int(pol.get("release_lead_days") or 0))).isoformat() if due else ""


def decide(pol: dict, finding: dict) -> tuple[str, str]:
    """(outcome, why) for an open finding: hotfix, scheduled or backlog."""
    sev = finding.get("severity") or "medium"
    hot = pol.get("hotfix") or {}
    if finding.get("kind") == "secret" and hot.get("secrets", True):
        return "hotfix", "an exposed secret"
    if sev_rank(sev) >= sev_rank(hot.get("severity", "critical")):
        return "hotfix", f"{sev} severity"
    ex = hot.get("exploit_likely") or {}
    epss = finding.get("epss")
    if epss is not None and ex and float(epss) >= float(ex.get("epss", 1)) and sev_rank(sev) >= sev_rank(ex.get("severity", "high")):
        return "hotfix", f"{sev} severity and likely to be exploited (EPSS {float(epss):.0%})"
    if not (pol.get("fix_within_days") or {}).get(sev):
        return "backlog", f"{sev} severity has no deadline"
    return "scheduled", f"{sev} severity: fixed now, released by {deadline(pol, sev, finding.get('first_seen') or datetime.now(UTC).date())}"


def update_type(old: str, new: str) -> str:
    """patch, minor or major between two version strings ("" when they don't parse)."""
    import re

    def parts(v: str) -> list[int]:
        nums = re.findall(r"\d+", (v or "").split("+", 1)[0].split("-", 1)[0])
        return [int(n) for n in nums[:3]] + [0] * (3 - len(nums[:3]))

    if not re.search(r"\d", old or "") or not re.search(r"\d", new or ""):
        return ""
    a, b = parts(old), parts(new)
    if a[0] != b[0]:
        return "major"
    # 0.x: a minor bump may break, as semver says
    if a[1] != b[1]:
        return "major" if a[0] == 0 else "minor"
    return "patch"

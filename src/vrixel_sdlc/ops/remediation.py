"""What the tool may do in production by itself to end an incident (the exception to VX-3).

The platform decides in sdlc-governance's `platform/remediation.yaml`; a product can narrow it or
turn it off in product.yaml (`runtime.remediation`), never widen it:

    autonomous:
      flag_off: {released_within_hours: 72}   # turn off a feature's flag, if it reached the failing
                                              # component that recently and its flag is on
      rollback: {released_within_hours: 24}   # roll a component back to its previous release, if a
                                              # release reached it that recently
    start_hotfix: true       # after mitigating, start the fix triage proposed (G5 and G6 stay people's)
    attempts: 1              # autonomous mitigations per incident before a person is paged
    quiet_minutes: {sev1: 10, sev2: 15, sev3: 30, sev4: 60}   # how long it must stay quiet to count
    page:
      heads_up: [sev1]       # informational page as soon as these open, while the tool works on them

Both actions undo a change rather than make one: the release being restored, or the behavior
without the feature, was already approved. Everything else (a hotfix release, an intent change,
anything not on the list) stays a person's decision, and so does any incident a person has already
decided on. A mitigation that fails, or a failure that persists after it, pages on-call.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import yaml

ACTIONS = ("flag_off", "rollback")
QUIET_MINUTES = {"sev1": 10, "sev2": 15, "sev3": 30, "sev4": 60}
AGENT = "agent:remediate"


class PolicyError(ValueError):
    pass


def load_policy(text: str) -> dict:
    """Parse and check the platform's policy; no file means no autonomy."""
    if not (text or "").strip():
        return {}
    doc = yaml.safe_load(text) or {}
    if not isinstance(doc, dict):
        raise PolicyError("remediation policy must be a mapping")
    auto = doc.get("autonomous") or {}
    if not isinstance(auto, dict) or set(auto) - set(ACTIONS):
        raise PolicyError(f"remediation autonomous actions must be among {ACTIONS}")
    for name, rule in auto.items():
        hours = (rule or {}).get("released_within_hours", 24)
        if not isinstance(hours, (int, float)) or isinstance(hours, bool) or hours <= 0:
            raise PolicyError(f"remediation {name}.released_within_hours must be a positive number")
    quiet = {**QUIET_MINUTES, **(doc.get("quiet_minutes") or {})}
    if set(quiet) - set(QUIET_MINUTES) or not all(isinstance(v, int) and v >= 5 for v in quiet.values()):
        raise PolicyError("remediation quiet_minutes are whole minutes (at least 5) per sev1..sev4")
    attempts = doc.get("attempts", 1)
    if not isinstance(attempts, int) or isinstance(attempts, bool) or not 0 <= attempts <= 3:
        raise PolicyError("remediation attempts must be 0 to 3")
    heads_up = list(((doc.get("page") or {}).get("heads_up")) or [])
    if set(heads_up) - set(QUIET_MINUTES):
        raise PolicyError("remediation page.heads_up lists severities sev1..sev4")
    return {"autonomous": {k: {"released_within_hours": float((v or {}).get("released_within_hours", 24))}
                           for k, v in auto.items()},
            "start_hotfix": doc.get("start_hotfix", True) is True, "attempts": attempts,
            "quiet_minutes": quiet, "page": {"heads_up": heads_up}}


def for_product(policy: dict, runtime: dict | None) -> dict:
    """The platform's policy as it applies to one product: `runtime.remediation.autonomous` false
    turns autonomy off, a list keeps only those actions."""
    if not policy:
        return {"autonomous": {}, "start_hotfix": False, "attempts": 0, "quiet_minutes": dict(QUIET_MINUTES),
                "page": {"heads_up": ["sev1"]}}
    own = (runtime or {}).get("remediation") or {}
    allowed = own.get("autonomous", True)
    auto = dict(policy["autonomous"])
    if allowed is False:
        auto = {}
    elif isinstance(allowed, list):
        auto = {k: v for k, v in auto.items() if k in allowed}
    return {**policy, "autonomous": auto}


def check(runtime: dict | None) -> list[str]:
    """Problems with a product's runtime.remediation."""
    own = (runtime or {}).get("remediation")
    if own is None:
        return []
    allowed = own.get("autonomous", True)
    if allowed is True or allowed is False:
        return []
    if isinstance(allowed, list) and all(a in ACTIONS for a in allowed):
        return []
    return [f"runtime.remediation.autonomous must be true, false or a list of {list(ACTIONS)}"]


def _at(value) -> datetime | None:
    try:
        t = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def choose(actions: list[dict], suspects_for, component: str, policy: dict,
           now: datetime | None = None) -> tuple[list[dict], str]:
    """The mitigation the tool may take by itself among triage's proposals, and the fix to start
    after it: ([actions], "") or ([], why a person is needed). `suspects_for(repo)` lists the
    released features that changed a component, with released_at and their flag."""
    now = now or datetime.now(timezone.utc)
    auto = policy.get("autonomous") or {}
    if not auto:
        return [], "this product's remediation policy lets the tool take no action by itself"

    def recent(s: dict, rule: dict) -> bool:
        t = _at(s.get("released_at"))
        return bool(t) and timedelta(0) <= now - t <= timedelta(hours=rule["released_within_hours"])

    chosen: dict | None = None
    for a in actions:
        if a["type"] == "flag_off" and "flag_off" in auto:
            s = next((x for x in suspects_for(component) if x.get("feature_id") == a.get("feature_id")), None)
            if s and s.get("flag") and s.get("flag_on") and recent(s, auto["flag_off"]):
                chosen = a
                break
    if chosen is None:
        for a in actions:
            if a["type"] == "rollback" and "rollback" in auto:
                target = a.get("component") or component
                if target and any(recent(s, auto["rollback"]) for s in suspects_for(target)):
                    chosen = {**a, "component": target}
                    break
    if chosen is None:
        kinds = sorted({a["type"] for a in actions}) or ["none"]
        return [], (f"no action the tool may take by itself fits (triage proposed {', '.join(kinds)}; the tool "
                    f"may {' or '.join(a.replace('_', ' ') for a in auto)} only for a change released recently)")
    fixes = [a for a in actions if a["type"] == "hotfix"] if policy.get("start_hotfix") else []
    return [chosen, *fixes[:1]], ""


def no_page_reason(signals: list[dict], actions: list[dict]) -> str:
    """Why an incident the tool can't fix should not page anyone ('' when it should): spend is
    never an outage, and a request to change what was built isn't one either."""
    live = [s for s in signals if s.get("category") != "cost"]
    if not live:
        return "a cost finding: on-call reviews it in working hours"
    if all(s.get("source") == "ticket" for s in live) and actions and all(
            a["type"] in ("intent_change", "monitor") for a in actions):
        return "users asked for a change, nothing is down: on-call reviews it in working hours"
    return ""

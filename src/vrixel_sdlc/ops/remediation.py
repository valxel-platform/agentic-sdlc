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

    analysis:                # OpenChoreo's agents, reading the product's telemetry with an AI model
      rca:  {provider: anthropic, exclude_regions: [ap-south-1, ap-south-2]}
      cost: {provider: anthropic}
      fix_agent: false       # OpenChoreo's own fix agent (REMED_AGENT): must stay off
    require_agreement: true  # act alone only when triage's suspect and a high-confidence root
                             # cause from OpenChoreo point at the same component and change
    without_analysis: page   # no analysis for this product: page (default) or act on suspects alone
    calibration: {window_days: 90, min_samples: 5, suspend_below: 0.6}
    gradual_release:         # G6 approves the plan; the tool steps up while it stays quiet
      steps: [5, 25, 50, 100]
      bake_minutes: {low: 30, high: 60, regulated: 120}

Both mitigations undo a change rather than make one: the release being restored, or the behavior
without the feature, was already approved. Everything else (a hotfix release, an intent change,
anything not on the list) stays a person's decision, and so does any incident a person has already
decided on. A mitigation that fails, or a failure that persists after it, pages on-call.

Root-cause analysis sends the component's logs, metrics and traces to the AI provider OpenChoreo's
agent is configured with, so it runs only for a data class that may go to that provider (the
tool's own `data_classes` setting) and outside excluded regions. Calibration keeps the tool honest:
an action whose automatic uses recovered the incident less often than `suspend_below` (over at
least `min_samples` in `window_days`) is suspended for that product until its record improves.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

import yaml

ACTIONS = ("flag_off", "rollback")
QUIET_MINUTES = {"sev1": 10, "sev2": 15, "sev3": 30, "sev4": 60}
ANALYSIS_WAIT = {"sev1": 5, "sev2": 8, "sev3": 10, "sev4": 10}
RISK_TIERS = ("low", "high", "regulated")
AGENT = "agent:remediate"
ROLLOUT_AGENT = "agent:rollout"


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
    analysis = doc.get("analysis") or {}
    if analysis.get("fix_agent", False) is not False:
        raise PolicyError("remediation analysis.fix_agent must be false: OpenChoreo's fix agent would change "
                          "production outside this policy and the change log")
    parsed_analysis = {}
    for kind in ("rca", "cost"):
        a = analysis.get(kind)
        if a is None:
            continue
        if not isinstance(a, dict) or not str(a.get("provider") or ""):
            raise PolicyError(f"remediation analysis.{kind} needs the provider OpenChoreo's agent uses")
        wait = {**ANALYSIS_WAIT, **(a.get("wait_minutes") or {})}
        if set(wait) - set(ANALYSIS_WAIT) or not all(isinstance(v, int) and 1 <= v <= 60 for v in wait.values()):
            raise PolicyError(f"remediation analysis.{kind}.wait_minutes are 1 to 60 per sev1..sev4")
        parsed_analysis[kind] = {"provider": str(a["provider"]), "exclude_regions": [str(r) for r in a.get("exclude_regions") or []],
                                 "wait_minutes": wait}
    without = doc.get("without_analysis", "page")
    if without not in ("page", "suspects"):
        raise PolicyError("remediation without_analysis is page or suspects")
    cal = {"window_days": 90, "min_samples": 5, "suspend_below": 0.6, **(doc.get("calibration") or {})}
    if not (isinstance(cal["min_samples"], int) and cal["min_samples"] >= 1 and 0 <= float(cal["suspend_below"]) <= 1
            and isinstance(cal["window_days"], int) and cal["window_days"] >= 1):
        raise PolicyError("remediation calibration: window_days and min_samples are whole numbers, suspend_below 0 to 1")
    gradual = doc.get("gradual_release")
    return {"autonomous": {k: {"released_within_hours": float((v or {}).get("released_within_hours", 24))}
                           for k, v in auto.items()},
            "start_hotfix": doc.get("start_hotfix", True) is True, "attempts": attempts,
            "quiet_minutes": quiet, "page": {"heads_up": heads_up}, "analysis": parsed_analysis,
            "require_agreement": doc.get("require_agreement", True) is not False, "without_analysis": without,
            "calibration": {"window_days": cal["window_days"], "min_samples": cal["min_samples"],
                            "suspend_below": float(cal["suspend_below"])},
            "gradual_release": _gradual(gradual) if gradual else None}


def _gradual(raw: dict) -> dict:
    steps = raw.get("steps") or []
    if (not isinstance(steps, list) or not steps or any(not isinstance(s, (int, float)) or isinstance(s, bool) for s in steps)
            or steps != sorted(set(steps)) or steps[-1] != 100 or steps[0] <= 0):
        raise PolicyError("gradual_release.steps rise from above 0 to 100, e.g. [5, 25, 50, 100]")
    bake = {"low": 30, "high": 60, "regulated": 120, **(raw.get("bake_minutes") or {})}
    if set(bake) - set(RISK_TIERS) or not all(isinstance(v, int) and v >= 1 for v in bake.values()):
        raise PolicyError("gradual_release.bake_minutes are whole minutes per risk tier (low, high, regulated)")
    return {"steps": [float(s) for s in steps], "bake_minutes": bake}


def for_product(policy: dict, runtime: dict | None, data_class: str = "", data_classes: dict | None = None) -> dict:
    """The platform's policy as it applies to one product: `runtime.remediation.autonomous` false
    turns autonomy off, a list keeps only those actions; `gradual_release: false` releases at once,
    `analysis: false` keeps OpenChoreo's agents away from the product's telemetry. Each analysis
    says whether it may run here (`allowed`) and why not."""
    if not policy:
        return {"autonomous": {}, "start_hotfix": False, "attempts": 0, "quiet_minutes": dict(QUIET_MINUTES),
                "page": {"heads_up": ["sev1"]}, "analysis": {}, "require_agreement": True,
                "without_analysis": "page", "calibration": {"window_days": 90, "min_samples": 5, "suspend_below": 0.6},
                "gradual_release": None}
    own = (runtime or {}).get("remediation") or {}
    allowed = own.get("autonomous", True)
    auto = dict(policy["autonomous"])
    if allowed is False:
        auto = {}
    elif isinstance(allowed, list):
        auto = {k: v for k, v in auto.items() if k in allowed}
    region = str(((runtime or {}).get("aws") or {}).get("region") or "")
    analysis = {}
    for kind, a in (policy.get("analysis") or {}).items():
        why = ""
        if own.get("analysis") is False:
            why = "this product keeps OpenChoreo's agents away from its telemetry"
        elif not provider_allowed(a["provider"], data_class, data_classes):
            why = f"{data_class or 'its'} data may not go to {a['provider']}, the provider OpenChoreo's agent uses"
        elif region and region in a["exclude_regions"]:
            why = f"its data stays in {region}; the agent's provider runs outside it"
        analysis[kind] = {**a, "allowed": not why, "why_not": why}
    gradual = policy.get("gradual_release")
    if own.get("gradual_release") is False:
        gradual = None
    elif gradual and isinstance(own.get("gradual_release"), dict) and own["gradual_release"].get("steps"):
        gradual = {**gradual, "steps": _gradual({**gradual, **own["gradual_release"]})["steps"]}
    return {**policy, "autonomous": auto, "analysis": analysis, "gradual_release": gradual}


def provider_allowed(provider: str, data_class: str, data_classes: dict | None) -> bool:
    if data_classes is None:
        return True
    allowed = data_classes.get(data_class or "general") or []
    return "*" in allowed or provider in allowed


def check(runtime: dict | None) -> list[str]:
    """Problems with a product's runtime.remediation."""
    own = (runtime or {}).get("remediation")
    if own is None:
        return []
    problems = []
    allowed = own.get("autonomous", True)
    if not (allowed is True or allowed is False or (isinstance(allowed, list) and all(a in ACTIONS for a in allowed))):
        problems.append(f"runtime.remediation.autonomous must be true, false or a list of {list(ACTIONS)}")
    if own.get("analysis", True) not in (True, False):
        problems.append("runtime.remediation.analysis is true or false")
    g = own.get("gradual_release", True)
    if g not in (True, False):
        try:
            _gradual({"steps": (g or {}).get("steps") if isinstance(g, dict) else None})
        except PolicyError as exc:
            problems.append(f"runtime.remediation.{exc}")
    return problems


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


# --- agreement: two independent sources before the tool acts alone --------------------------

_CHANGE = re.compile(r"\b(deploy\w*|release[sd]?|rollout|rolled out|version|commit\w*|image|upgrade\w*|"
                     r"config\w*|flag|change[sd]?)\b", re.I)


def _short(c: str) -> str:
    return str(c or "").rsplit("/", 1)[-1].strip("`").lower()


def report_body(report: dict | None) -> dict:
    """The RCA report itself, from what the agent returned ({status, report} or the report)."""
    if not report:
        return {}
    return report.get("report") or {} if "status" in report else report


def agreement(report: dict | None, component: str) -> tuple[bool, str]:
    """Whether OpenChoreo's root-cause analysis agrees that a recent change to this component is
    the cause: a high-confidence root cause whose evidence is in the component and that names a
    change (a release, deployment, configuration or flag). (agrees, why) for the incident's record."""
    body = report_body(report)
    if not body:
        status = (report or {}).get("status")
        return False, ("OpenChoreo's analysis failed" if status == "failed" else "OpenChoreo's analysis didn't finish in time")
    res = body.get("result") or {}
    if res.get("type") != "root_cause_identified":
        return False, f"OpenChoreo's analysis found no root cause: {str(res.get('explanation') or '')[:200]}".rstrip(": ")
    short = _short(component)
    high = [rc for rc in res.get("root_causes") or [] if str(rc.get("confidence", "")).lower() == "high"]
    if not high:
        return False, "OpenChoreo's analysis isn't confident of any root cause"
    timeline = " ".join(str(e.get("event", "")) for e in res.get("timeline") or [] if _short(e.get("component")) == short)
    for rc in high:
        comps = {_short(f.get("component")) for f in rc.get("supporting_findings") or []}
        text = f"{rc.get('summary', '')} {rc.get('analysis', '')}"
        if short and (short in comps or short in text.lower()):
            if _CHANGE.search(f"{text} {timeline}"):
                return True, f"OpenChoreo's analysis agrees (high confidence): {rc.get('summary', '')}"
            return False, f"OpenChoreo's analysis blames {short}, but not a change to it: {rc.get('summary', '')}"
    return False, f"OpenChoreo's analysis points elsewhere: {high[0].get('summary', '')}"


def applied_outside(report: dict | None) -> list[str]:
    """Actions OpenChoreo's own fix agent applied: production changed outside this policy."""
    recs = ((report_body(report).get("result") or {}).get("recommendations") or {}).get("recommended_actions") or []
    return [str(a.get("description", "")) for a in recs if isinstance(a, dict) and a.get("status") == "applied"]


# --- calibration: how often the tool's own fixes worked ---------------------------------------

def track_record(incidents: list[dict], window_days: int, now: datetime | None = None) -> dict[str, dict]:
    """Per action: {"worked", "failed"} over the window. An automatic flag off or rollback worked
    when the incident then recovered without a person; it failed when it escalated after it."""
    now = now or datetime.now(timezone.utc)
    out: dict[str, dict] = {a: {"worked": 0, "failed": 0} for a in ACTIONS}
    for inc in incidents:
        for t in inc.get("taken") or []:
            if t.get("by") != AGENT or t.get("type") not in ACTIONS:
                continue
            at = _parse(t.get("at"))
            if not at or now - at > timedelta(days=window_days):
                continue
            if not t.get("ok") or inc.get("escalated"):
                out[t["type"]]["failed"] += 1
            elif any(e.get("kind") == "recovered" for e in inc.get("events") or []):
                out[t["type"]]["worked"] += 1
    return out


def suspended(record: dict[str, dict], calibration: dict) -> dict[str, str]:
    """Actions whose record is below the bar: {action: why}."""
    out = {}
    for action, r in record.items():
        n = r["worked"] + r["failed"]
        if n >= calibration["min_samples"] and r["worked"] / n < calibration["suspend_below"]:
            out[action] = (f"the tool's {action.replace('_', ' ')} worked {r['worked']} of {n} times in "
                           f"{calibration['window_days']} days, below {int(calibration['suspend_below'] * 100)}%")
    return out


def _parse(value) -> datetime | None:
    try:
        t = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)

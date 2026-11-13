"""Features that say what they move (step 2 of the outcome loop).

    brief (OPP)   ## Product goal       - GOAL-CLN-1: <how this feature moves it>   (or - None: <why>)
    PRD           ## Instrumentation    - checkin_completed (GOAL-CLN-1): fires when ...; properties: a, b
    HLD           ## Instrumentation    - checkin_completed: acme-clinic/cln-intake-api
    build         the emitting repo records each event (OpenTelemetry counter valxel.events,
                  attribute event=<name>) and names it after 'Instrumentation:' in its patch summary
    staging       the templates log one summary line a minute, counted here after staging passes:

        valxel.events {"checkin_completed": 12, "checkin_abandoned": 1}

An event that never fired in staging means the goal can't be measured once the feature ships: G6
shows it, and the people at the gate decide. Properties are for aggregates: none may name or
identify a person, and PHI and payments products may not carry person-level ids at all.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone

MARKER = "valxel.events"
EVENT_NAME = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z0-9_]+)*$")

# Properties that name or reach a person, in any product.
PERSONAL = {"name", "first_name", "last_name", "full_name", "display_name", "username", "user_name",
            "email", "email_address", "phone", "phone_number", "mobile", "dob", "date_of_birth", "birth_date",
            "birthdate", "ssn", "social_security_number", "mrn", "address", "street", "street_address", "zip",
            "zipcode", "zip_code", "postcode", "postal_code", "ip", "ip_address", "card", "card_number", "pan",
            "cvv", "cvc", "iban", "account_number", "routing_number", "passport", "passport_number",
            "license_number", "drivers_license", "lat", "lng", "latitude", "longitude", "geo", "location"}
PERSONAL_SUFFIXES = ("_email", "_phone", "_dob", "_ssn", "_address", "_name", "_card_number")
NOT_PERSONAL = {"event_name", "screen_name", "page_name", "feature_name", "plan_name", "step_name", "clinic_name",
                "farm_name", "store_name", "product_name", "file_name", "field_name", "error_name"}
# Person-level ids: fine as a join key in general products, never in PHI or payments events.
PERSON_IDS = ("patient", "member", "beneficiary", "subscriber", "cardholder", "user", "customer", "guest",
              "client", "person", "account")


def property_problems(event: str, props: list[str], data_class: str) -> list[str]:
    out = []
    for p in props:
        key = re.sub(r"(?<!^)(?=[A-Z])", "_", p).lower().strip()
        if not re.fullmatch(r"[a-z][a-z0-9_]*", key):
            out.append(f"{event}: property {p!r} must be a plain name (letters, digits, underscores)")
            continue
        personal = key in PERSONAL or (key.endswith(PERSONAL_SUFFIXES) and key not in NOT_PERSONAL)
        if personal:
            out.append(f"{event}: property {p} names or reaches a person; events carry aggregates' inputs only")
        elif data_class in ("phi", "payments") and any(
                key in (w, f"{w}_id", f"{w}_ids", f"{w}_number") for w in PERSON_IDS):
            out.append(f"{event}: property {p} identifies a person, which {data_class} events may not carry; "
                       "count them instead, or use a category (an age band, a visit type)")
    return out


def event_problems(events: list[dict], goals: list[str], data_class: str) -> list[str]:
    """The PRD's Instrumentation against the brief's product goals."""
    out = []
    seen: set[str] = set()
    for e in events:
        name = e["event"]
        if not EVENT_NAME.match(name):
            out.append(f"Event {name} must be lowercase words joined by _ or . (e.g. checkin_completed)")
        if name in seen:
            out.append(f"Event {name} is listed twice")
        seen.add(name)
        if not e["goals"]:
            out.append(f"{name} feeds no goal: write '- {name} (GOAL-...): fires when ...'")
        for g in e["goals"]:
            if g not in goals:
                out.append(f"{name} feeds {g}, which the brief doesn't name under Product goal")
        if not e["when"] or len(e["when"].split()) < 3:
            out.append(f"{name}: say when it fires (fires when <what happened>)")
        out += property_problems(name, e["properties"], data_class)
    for g in goals:
        if not any(g in e["goals"] for e in events):
            out.append(f"No event feeds {g}: list the events that show whether the feature moves it")
    return out


# --------------------------------------------------------------------------------- staging

def parse_counts(entries) -> dict[str, int]:
    """Sum the summary lines: [(component, line)] -> event -> count."""
    out: dict[str, int] = {}
    for _, text in entries:
        i = text.find(MARKER)
        if i < 0:
            continue
        rest = text[i + len(MARKER):].strip()
        try:
            rec = json.loads(rest)
        except ValueError:
            try:
                rec = json.loads(json.loads(text).get("message", "").split(MARKER, 1)[1].strip())
            except (ValueError, AttributeError, IndexError, TypeError):
                continue
        if isinstance(rec, dict):
            for k, v in rec.items():
                if isinstance(v, (int, float)):
                    out[str(k)] = out.get(str(k), 0) + int(v)
    return out


def line(counts: dict[str, int]) -> str:
    """A summary line, for tests and templates."""
    return f"{MARKER} {json.dumps(counts)}"


def _since(state: dict) -> datetime:
    for e in reversed(state.get("events") or []):
        if e.get("node") == "deploy_staging":
            try:
                t = datetime.fromisoformat(str(e["at"]).replace("Z", "+00:00"))
                return t if t.tzinfo else t.replace(tzinfo=timezone.utc)
            except (KeyError, ValueError):
                break
    return datetime.now(timezone.utc) - timedelta(hours=2)


def staging_check(services, state: dict, events: list[dict], environment: str) -> dict:
    """Which of the feature's events fired in staging since it was deployed there.
    {"checked": bool, "environment", "fired": {event: n}, "missing": [event], "why"?: str}"""
    names = [e["event"] for e in events]
    base = {"checked": False, "environment": environment, "fired": {}, "missing": [],
            "events": [{"event": e["event"], "goals": e.get("goals", []), "repo": e.get("repo", "")} for e in events]}
    if not names:
        return {**base, "why": "the feature names no events"}
    misses = int((state.get("simulate_faults") or {}).get("instrumentation", 0))
    logs = getattr(services, "cohort_logs", None)
    if logs is None:
        if misses:  # stub mode, asked to show a gap
            return {**base, "checked": True, "fired": {n: 0 for n in names}, "missing": names[:misses]}
        return {**base, "why": "no log source to count events (set VALXEL_OPENCHOREO_OBSERVER_URL)"}
    try:
        rt = services.snapshot(state["product_id"]).product.runtime or {}
        entries = logs.entries(namespace=rt.get("namespace") or "default", project=rt.get("openchoreo_project") or "",
                               environment=environment, since=_since(state), until=datetime.now(timezone.utc),
                               phrase=MARKER)
    except Exception as exc:
        return {**base, "why": f"couldn't read the logs: {type(exc).__name__}: {exc}"[:200]}
    counts = parse_counts(entries)
    fired = {n: counts.get(n, 0) for n in names}
    return {**base, "checked": True, "fired": fired, "missing": [n for n in names if not fired[n]]}


def describe(check: dict) -> str:
    if not check or not check.get("events"):
        return ""
    if not check.get("checked"):
        return f"Instrumentation not checked: {check.get('why', '')}"
    if check["missing"]:
        return (f"Never fired in {check['environment']}: {', '.join(check['missing'])}. The goal they feed can't be "
                "measured after release until they do.")
    return f"All {len(check['fired'])} events fired in {check['environment']}"

"""Scheduled jobs (role `job`, valxel-platform/template-job): their schedules at G4 and on deploy.

A job runs as an OpenChoreo `cronjob/scheduled-task` component. Its schedule lives in two places
that must agree:

  the HLD      '## Schedules' says when each job runs and what for, so people approve the schedule
               at G4: '- acme-farm/farm-reminders: 15 3 * * * America/Chicago; sends the morning
               reminders'
  job.yaml     the repo's own file, which the coder writes from that line. On every deploy the tool
               reads it at the commit it deploys and sets the CronJob's schedule, time zone, time
               limit and suspend flag for the environment (binding_configs), so a schedule changes
               through a pull request like code.

Schedules are five numeric cron fields (minute hour day-of-month month day-of-week) and an IANA
time zone: the same rules template-job's own parser and Kubernetes apply.
"""

from __future__ import annotations

import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml

ROLE = "job"
PATH = "job.yaml"
_BOUNDS = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 7))
_FIELDS = ("minute", "hour", "day of month", "month", "day of week")
_LINE = re.compile(r"^- ([A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9._-]+)\s*:\s*(.+?)\s*$", re.M)


def check_schedule(text: str, timezone: str = "UTC") -> str:
    """'' when the schedule and time zone are usable, else what's wrong with them."""
    parts = (text or "").split()
    if len(parts) != 5:
        return f"'{text}' needs five fields: minute hour day-of-month month day-of-week"
    for part, (lo, hi), name in zip(parts, _BOUNDS, _FIELDS, strict=True):
        for item in part.split(","):
            body, _, step = item.partition("/")
            if step and (not step.isdigit() or int(step) < 1):
                return f"{name}: step '{step}' must be a whole number, 1 or more"
            if body == "*":
                continue
            a, _, b = body.partition("-")
            if not a.isdigit() or (b and not b.isdigit()):
                return f"{name}: '{item}' isn't a number, a range or '*' (names like MON aren't accepted)"
            lo_v, hi_v = int(a), int(b or a)
            if not (lo <= lo_v <= hi and lo <= hi_v <= hi) or lo_v > hi_v:
                return f"{name}: '{item}' is outside {lo}-{hi}"
    try:
        ZoneInfo(timezone or "UTC")
    except (ZoneInfoNotFoundError, ValueError):
        return f"'{timezone}' isn't a time zone (use an IANA name, e.g. America/Chicago)"
    return ""


def parse_schedules(hld: str) -> list[dict]:
    """'## Schedules' lines: repo -> schedule, time zone, what it's for (and any problem)."""
    from .agents.parse import section

    out = []
    for repo, rest in _LINE.findall(section(hld, "Schedules")):
        when, _, why = rest.partition(";")
        words = when.split()
        cron, zone = " ".join(words[:5]), (words[5] if len(words) > 5 else "UTC")
        problem = check_schedule(cron, zone) if len(words) in (5, 6) else (
            f"'{when.strip()}' isn't '<five cron fields> <time zone>'")
        out.append({"repo": repo, "schedule": cron, "timezone": zone, "why": why.strip(), "problem": problem})
    return out


def design_findings(hld: str, repos: list[dict]) -> list[str]:
    """G4: every job repo the design changes or adds has a usable schedule with a reason."""
    jobs = {r["name"] for r in repos if r.get("role") == ROLE}
    lines = parse_schedules(hld)
    out = []
    named = set()
    for s in lines:
        named.add(s["repo"])
        if s["repo"] not in jobs:
            out.append(f"Schedules: {s['repo']} isn't a job repo in this design (role job)")
        if s["problem"]:
            out.append(f"Schedules: {s['repo']}: {s['problem']}")
        if len(s["why"].split()) < 3:
            out.append(f"Schedules: {s['repo']}: say what the job does after ';'")
    for repo in sorted(jobs - named):
        out.append(f"{repo} is a job: say when it runs under '## Schedules' ('- {repo}: <minute hour day month "
                   "weekday> <time zone>; <what it does>')")
    return out


def coder_instruction(hld: str, repo: str) -> str:
    """What the coder writes into job.yaml for this repo, from the approved HLD."""
    line = next((s for s in parse_schedules(hld) if s["repo"] == repo and not s["problem"]), None)
    if line is None:
        return ""
    return (f" Set job.yaml's schedule to \"{line['schedule']}\" and its timezone to {line['timezone']}, as the HLD's "
            "Schedules section says; keep its name.")


def binding_configs(text: str, env: str) -> dict:
    """The CronJob settings for one environment from a repo's job.yaml: the environment's name (the
    job reads its own schedule there), schedule, time zone, time limit and suspend. {} when the file is missing or unusable (the type's defaults then apply)."""
    try:
        raw = yaml.safe_load(text or "") or {}
    except yaml.YAMLError:
        return {}
    if not isinstance(raw, dict) or not raw.get("schedule"):
        return {}
    zone = str(raw.get("timezone") or "UTC")
    override = (raw.get("environments") or {}).get(env) or {} if env != "production" else {}
    schedule = str(override.get("schedule") or raw["schedule"])
    if check_schedule(schedule, zone):
        return {}
    limit = raw.get("time_limit_minutes", 30)
    limit = limit if isinstance(limit, int) and 1 <= limit <= 1440 else 30
    return {"environment": env, "schedule": schedule, "timeZone": zone, "suspend": bool(override.get("suspend", False)),
            "activeDeadlineSeconds": limit * 60}


def read_configs(github, repo: str, ref: str, env: str) -> dict:
    """binding_configs from job.yaml at the commit being deployed ('' = the default branch)."""
    try:
        text = github.get_file(repo=repo, path=PATH, ref=ref or "")
    except Exception:  # noqa: BLE001 - the deploy goes on with the configs it had
        return {}
    return binding_configs(text or "", env)

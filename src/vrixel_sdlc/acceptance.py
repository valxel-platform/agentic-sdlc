"""Product acceptance tests: end-to-end tests of a feature across the repos it changes.

Each product may keep one acceptance repo (role `acceptance`, from vrixel-tools/template-acceptance):
Playwright Test in TypeScript, API and event tests for every feature, browser tests when the
feature changes a web app. Tests are tagged with the feature and acceptance criteria they prove
(`@CLN-FEAT-12`, `@CLN-FEAT-12/AC-3`), the capability (`@cap-consent`) and the components they
reach (`@comp-cln-intake-api`), and declare what they cover (`covers` annotations: routes with an
optional status, events, pages). The repo's fixtures fail a test that reaches what it doesn't declare.
The repo's `catalog.json` lists every test with those fields.

- **Plan**: for a feature that needs acceptance tests, the plan's `## Acceptance tests` section
  says per criterion whether to extend, replace or add a test. A new test that covers nothing an
  existing test of the same kind (API or browser) doesn't already cover is an overlap: extend that
  test, or say `because:` why not.
- **Run**: once the whole change set is deployed to dev (then staging), the pipeline asks the
  acceptance repo to run the feature's tests plus every test that reaches a changed component
  (and the critical journeys in staging), with every component's URL and the feature flag on.
- **Gate**: the run reports per criterion; a criterion without a passing test fails the stage,
  and G6 sees each criterion with its result.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from .agents.parse import parse_acs, section

RUNTIME_ROLES = ("provider", "event-worker", "consumer", "mcp-server", "agent")
_LINE = re.compile(
    r"^- (AC-\d+):\s*(extend|replace|add)\s+(\S+\.spec\.ts)"
    r"(?:\s+\"([^\"]+)\")?"
    r"(?:\s+covers:\s*(.+?))?"
    r"(?:\s+because:\s*(.+?))?\s*$", re.M)


@dataclass
class PlannedTest:
    ac: str
    action: str  # extend | replace | add
    file: str
    title: str = ""
    covers: list[str] = field(default_factory=list)
    because: str = ""


def parse_plan(plan: str) -> list[PlannedTest]:
    """'## Acceptance tests' lines:
    - AC-2: extend consent/capture.spec.ts "records consent"
    - AC-3: replace consent/expiry.spec.ts "expires after a year"
    - AC-4: add consent/withdraw.spec.ts covers: cln-intake-api PUT /api/v1/consents/{id}, event vrixel.consent.withdrawn.v1
    """
    out = []
    for ac, action, file, title, covers, because in _LINE.findall(section(plan, "Acceptance tests")):
        out.append(PlannedTest(ac, action, file, title or "", split_covers(covers), because or ""))
    return out


def split_covers(text: str) -> list[str]:
    return [norm_cover(c) for c in (text or "").split(",") if c.strip()]


def norm_cover(c: str) -> str:
    """'cln-intake-api  put /api/v1/x 202' -> 'cln-intake-api PUT /api/v1/x 202'; events and pages as written."""
    parts = c.split()
    if len(parts) >= 3 and parts[1].upper() in ("GET", "POST", "PUT", "PATCH", "DELETE"):
        return " ".join([parts[0], parts[1].upper(), *parts[2:]])
    return " ".join(parts)


def acceptance_repo(repos: list[dict]) -> str | None:
    return next((r["name"] for r in repos if r.get("role") == "acceptance"), None)


def runtime_repos(repos: list[dict]) -> list[dict]:
    return [r for r in repos if r.get("role") in RUNTIME_ROLES]


def needed(repos: list[dict], min_repos: int = 2) -> bool:
    """A change set needs acceptance tests when it changes min_repos runtime repos or more."""
    return len(runtime_repos(repos)) >= max(1, int(min_repos))


def component(repo: str) -> str:
    return repo.split("/", 1)[-1]


def capability_tag(capability: str) -> str:
    return "@cap-" + re.sub(r"[^a-z0-9]+", "-", capability.lower()).strip("-")


# --------------------------------------------------------------------------- catalog


def load_catalog(text: str | None) -> list[dict]:
    """catalog.json from the acceptance repo: {"tests": [{file, title, tags, capability, covers, project}]}."""
    if not text:
        return []
    try:
        raw = json.loads(text)
    except ValueError:
        return []
    tests = raw.get("tests") if isinstance(raw, dict) else raw
    out = []
    for t in tests or []:
        if isinstance(t, dict) and t.get("file") and t.get("title"):
            out.append({"file": t["file"], "title": t["title"], "tags": list(t.get("tags") or []),
                        "capability": t.get("capability") or "", "covers": [norm_cover(c) for c in t.get("covers") or []],
                        "project": t.get("project") or "api"})
    return out


def project_of(file: str) -> str:
    """Browser journeys are *.ui.spec.ts (project ui); everything else runs without a browser."""
    return "ui" if file.endswith(".ui.spec.ts") else "api"


def overlapping(covers: list[str], capability: str, catalog: list[dict], project: str = "api",
                exclude: set[str] = frozenset()) -> list[dict]:
    """Existing tests of the same project that already cover everything `covers` does (same
    capability when known). A fast API test is never redundant with a browser journey."""
    want = set(covers)
    if not want:
        return []
    return [t for t in catalog if t["file"] not in exclude and want <= set(t["covers"])
            and t.get("project", "api") == project
            and (not capability or not t["capability"] or t["capability"] == capability)]


def overlaps_in(catalog: list[dict]) -> list[dict]:
    """For a review: pairs where one test's coverage is contained in another's, same capability."""
    out = []
    for a in catalog:
        for b in catalog:
            if a is b or not a["covers"] or a["project"] != b["project"] or \
                    (a["capability"] and b["capability"] and a["capability"] != b["capability"]):
                continue
            if set(a["covers"]) < set(b["covers"]) or (set(a["covers"]) == set(b["covers"]) and
                                                       (a["file"], a["title"]) < (b["file"], b["title"])):
                out.append({"test": f"{a['file']} \"{a['title']}\"", "within": f"{b['file']} \"{b['title']}\"",
                            "covers": sorted(a["covers"])})
    return out


# --------------------------------------------------------------------------- plan check


def check_plan(plan: str, ctx: dict) -> list[str]:
    """The plan's acceptance tests: every criterion, no blind duplicates, extends what exists."""
    repos = [{"name": t.get("repo"), "role": t.get("role")} for t in ctx.get("plan_tasks") or []] or ctx.get("repos") or []
    acc = acceptance_repo(repos) or acceptance_repo(ctx.get("repos") or [])
    if not acc or not needed(repos, ctx.get("acceptance_min_repos", 2)):
        return []
    planned = parse_plan(plan)
    acs = list(parse_acs(ctx.get("parent_texts", {}).get("PRD", "")))
    out = []
    if not planned:
        return [f"Plan has no Acceptance tests section for {component(acc)}: one line per criterion, "
                "'- AC-n: extend|replace|add <file>.spec.ts [\"<title>\"] [covers: ...] [because: ...]'"]
    have = {p.ac for p in planned}
    out += [f"{ac} has no acceptance test in {component(acc)}" for ac in acs if ac not in have]
    catalog = ctx.get("acceptance_catalog") or []
    files = {t["file"] for t in catalog}
    titled = {(t["file"], t["title"]) for t in catalog}
    capability = ctx.get("capability", "")
    for p in planned:
        if p.action in ("extend", "replace"):
            if p.file not in files:
                out.append(f"{p.ac}: {p.action} {p.file}, but the acceptance catalog has no such test file")
            elif p.title and (p.file, p.title) not in titled:
                out.append(f"{p.ac}: {p.action} {p.file} \"{p.title}\", but that file has no test with this title")
        else:
            if not p.covers:
                out.append(f"{p.ac}: a new acceptance test says what it covers ('covers: <component> <METHOD> <path>, "
                           "event <type>, page <path>')")
                continue
            dup = overlapping(p.covers, capability, catalog, project_of(p.file))
            if dup and not p.because:
                names = ", ".join(f"{t['file']} \"{t['title']}\"" for t in dup[:3])
                out.append(f"{p.ac}: the new test in {p.file} covers nothing {names} doesn't already; "
                           "extend it, or say 'because:' why a separate test is needed")
    return out


# --------------------------------------------------------------------------- run


def selection(feature_id: str, repos: list[dict], stage: str) -> dict:
    """Which tests run: the feature's own, every test that reaches a changed component, and the
    critical journeys in staging; browsers only when a web app changed."""
    tags = [f"@{feature_id}"] + sorted({f"@comp-{component(r['name'])}" for r in runtime_repos(repos)})
    if stage == "staging":
        tags.append("@critical")
    ui = any(r.get("role") == "consumer" for r in repos)
    # Tags hold only letters, digits and dashes (safe in a regex); each ends at a space or the end
    # of the title, so @CLN-FEAT-1 doesn't select @CLN-FEAT-12.
    tags = [re.sub(r"[^A-Za-z0-9@-]", "-", t) for t in tags]
    grep = "|".join(t + r"(?=\s|$)" for t in tags)
    return {"grep": grep, "tags": tags, "projects": ["api", "ui"] if ui else ["api"]}


def check_name(feature_id: str, stage: str) -> str:
    return f"acceptance / {feature_id} / {stage}"


_AC_RESULT = re.compile(r"^\s*[-*]?\s*(AC-\d+)\s*:\s*(passed|failed|skipped)\b", re.M | re.I)
_DURATION = re.compile(r"duration\s*:\s*(\d+(?:\.\d+)?)\s*s", re.I)


def parse_report(summary: str) -> dict:
    """What the acceptance run writes in its check's summary: per-criterion lines and duration."""
    acs: dict[str, str] = {}
    for ac, state in _AC_RESULT.findall(summary or ""):
        state = state.lower()
        if acs.get(ac) != "failed":  # one failing test fails the criterion
            acs[ac] = state
    m = _DURATION.search(summary or "")
    return {"acs": acs, "duration_s": float(m.group(1)) if m else None}

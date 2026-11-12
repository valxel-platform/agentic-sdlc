"""Machine checks on the inception documents: what can be decided without judgment. Each returns
findings, in words the agent can fix from; none means the document goes to the critic."""

from __future__ import annotations

import re

from ..agents.parse import section
from ..repo_roles import ROLES
from ..resources import parse_resources
from . import DATA_CLASSES, ENVIRONMENTS, formats, valid_product_id

_NUMBER = re.compile(r"\d")
_STANDARD = re.compile(r"\b(WCAG|ISO|SOC ?2|PCI|HIPAA|GDPR|NIST|OWASP|FIPS)\b", re.I)
NFR_KINDS = ("performance", "availability", "reliability", "scalability", "security", "privacy", "accessibility",
             "compliance", "usability", "maintainability", "observability")
HORIZONS = ("now", "next", "later")
REPO_NAME = re.compile(r"^[a-z][a-z0-9]*(-[a-z0-9]+)+$")


def _needs(text: str, headings: tuple[str, ...]) -> list[str]:
    return [f"Missing the '## {h}' section" for h in headings if not section(text, h)]


def brief(text: str, ctx: dict) -> list[str]:
    out = _needs(text, ("Problem", "Users", "Goals", "Non-goals", "Scope", "Scale", "Availability", "Data class",
                        "Product"))
    out += _scale(text)
    if section(text, "Users") and not formats.users(text):
        out.append("Users: write each as '- U1: <who>: <their job and situation>'")
    gs = formats.goals(text)
    for g in gs:
        if not g["id"]:
            out.append(f"Goals: '{g['line']}' isn't in the form '- PG1: <outcome> (measure: <metric>)'")
        elif not _NUMBER.search(g["measure"]):
            out.append(f"{g['id']}: its measure has no number or threshold to hit ({g['measure']})")
    if section(text, "Goals") and not gs:
        out.append("Goals: none in the form '- PG1: <outcome> (measure: <metric>)'")
    dc, why = formats.data_class(text)
    if section(text, "Data class") and dc not in DATA_CLASSES:
        out.append(f"Data class: start the line with one of {', '.join(DATA_CLASSES)}, then ': <why>'")
    elif dc and len(why.split()) < 4:
        out.append("Data class: say why, from what the product stores")
    if ctx.get("research"):
        from ..research import citations

        out += citations(text, ctx["research"])
    p = formats.product(text)
    if section(text, "Product"):
        if not valid_product_id(p["id"]):
            out.append(f"Product: id '{p['id']}' must be 2 to 6 capital letters")
        elif ctx.get("existing"):
            if p["id"] != ctx.get("product_id"):
                out.append(f"Product: this is {ctx.get('product_id')}; keep its id")
        elif p["id"] in (ctx.get("registered") or ()):
            out.append(f"Product: id {p['id']} is already a registered product; choose another")
        if ctx.get("org") and p["org"] != ctx["org"]:
            out.append(f"Product: org is {ctx['org']} (the person named it), not '{p['org']}'")
        if not re.fullmatch(r"[a-z][a-z0-9]*-", p["prefix"] or ""):
            out.append("Product: repo prefix must be lowercase letters or digits ending in '-' (e.g. aqua-)")
        if p["budget_usd"] <= 0:
            out.append("Product: budget must be a number of US dollars")
    return out


def _scale(text: str) -> list[str]:
    from .. import capacity

    if not section(text, "Scale"):
        return []
    sc = capacity.scale(text)
    out = list(sc["problems"])
    if not sc["year_one"]:
        out.append("Scale: say how many after a year ('- year one: <number> <what is counted>')")
    if not sc["launch"]:
        out.append("Scale: say how many at launch ('- launch: <number> <what is counted>')")
    if sc["launch"] and sc["year_one"] and sc["year_one"] < sc["launch"]:
        out.append("Scale: year one is smaller than launch")
    if not sc["busy_factor"]:
        out.append("Scale: say how busy the busiest hour is ('- busy hour: 3 times the daily average, over a 10-hour day')")
    elif not 0 < sc["day_hours"] <= 24:
        out.append("Scale: the day is between 1 and 24 hours")
    if section(text, "Availability"):
        t, why = capacity.tier(text)
        if not t:
            out.append("Availability: write '- tier: 1|2|3: <what stops for whom when it's down>'")
        elif len(why.split()) < 4:
            out.append("Availability: say why this tier, in terms of what stops for whom")
        dc = formats.data_class(text)[0]
        if t == 3 and dc in ("phi", "payments"):
            out.append(f"Availability: {dc} products target tier 2 or better (their pilot still starts at tier 3)")
    budget = capacity.run_budget(text)
    if section(text, "Product") and set(budget) != {"pilot", "growth", "scale"}:
        out.append("Product: give the run budget a month for each stage ('- run budget: pilot <USD>; growth <USD>; "
                   "scale <USD>')")
    return out


def architecture(text: str, ctx: dict) -> list[str]:
    out = _needs(text, ("Context", "Components", "Load", "Scaling", "Failure", "NFRs", "ADRs"))
    if ctx.get("existing"):
        out += _needs(text, ("Current state", "Gap"))
    prefix, org = ctx.get("prefix", ""), ctx.get("org", "")
    comps = formats.components(text)
    names = set()
    for c in comps:
        if not c["name"]:
            out.append(f"Components: '{c['line']}' isn't in the form '- <component> (<role>): <responsibility>'")
            continue
        names.add(c["name"])
        if c["role"] not in ROLES:
            out.append(f"{c['name']}: role '{c['role']}' isn't one of {', '.join(ROLES)}")
        repo = c["repo"]
        if not repo:
            out.append(f"{c['name']}: name its repo, [repo: {org}/{prefix}{c['name']}]")
        else:
            owner, _, name = repo.partition("/")
            if org and owner != org:
                out.append(f"{c['name']}: its repo belongs in {org}, not {owner}")
            if prefix and not name.startswith(prefix) and not ctx.get("existing"):
                out.append(f"{c['name']}: repo {name} must start with the prefix {prefix}")
            if not REPO_NAME.match(name or ""):
                out.append(f"{c['name']}: repo name '{name}' must be lowercase words joined by '-'")
    if section(text, "Components") and not [c for c in comps if c.get("name")]:
        out.append("Components: none in the form '- <component> (<role>): <responsibility> [repo: ...]'")
    roles = {c.get("role") for c in comps}
    if comps and not roles & {"provider", "event-worker", "agent"}:
        out.append("Components: nothing holds the product's records or logic (a provider, event worker or agent)")

    catalog = ctx.get("catalog") or {}
    dc = ctx.get("data_class", "")
    for r in parse_resources(text):
        if not r["type"]:
            out.append(f"Resources: '{r['line']}' isn't in the form '- <name>: <type>; key=value'")
            continue
        t = (catalog.get("types") or {}).get(r["type"])
        if t is None or t.get("internal"):
            out.append(f"{r['name']}: {r['type']} isn't in the platform's AWS catalog")
            continue
        if dc and dc not in t.get("data_classes", []):
            out.append(f"{r['name']}: {r['type']} may not hold {dc} data")
        for p, spec in (t.get("parameters") or {}).items():
            if spec.get("required") and p not in r["params"]:
                out.append(f"{r['name']}: {r['type']} needs {p}=")
        for comp in r["access"]:
            if comp not in names:
                out.append(f"{r['name']}: '{comp}' uses it but isn't a component")
        for env in r["sizes"]:
            if env not in ENVIRONMENTS:
                out.append(f"{r['name']}: no environment called {env}")

    ns = formats.nfrs(text)
    for n in ns:
        if not n["id"]:
            out.append(f"NFRs: '{n['line']}' isn't in the form '- NFR-1 (<kind>): <target>'")
        elif n["kind"] not in NFR_KINDS:
            out.append(f"{n['id']}: kind '{n['kind']}' isn't one of {', '.join(NFR_KINDS)}")
        elif not (_NUMBER.search(n["target"]) or _STANDARD.search(n["target"])):
            out.append(f"{n['id']}: needs a number, a percentile, a limit or a named standard")
    pid = ctx.get("product_id", "")
    for a in formats.adrs(text):
        if not a["id"]:
            out.append(f"ADRs: '{a['line']}' isn't in the form '- ADR-{pid}-1: <decision> (alternatives: ...)'")
        elif pid and not a["id"].startswith(f"ADR-{pid}-"):
            out.append(f"{a['id']}: number it ADR-{pid}-<n>")
        elif "alternatives:" not in a["text"].lower():
            out.append(f"{a['id']}: say which alternatives were weighed (alternatives: ...)")
    from .. import capacity

    named = [c for c in comps if c.get("name")]
    out += capacity.check_doc(text, named, [r for r in parse_resources(text) if r.get("type")])
    if ctx.get("brief"):
        p = capacity.plan(ctx["brief"], text, ctx.get("capacity_platform"), catalog, named,
                          [r for r in parse_resources(text) if r.get("type")], dc)
        out += p["findings"]
    if dc in ("phi", "payments") and not re.search(r"\baudit", text, re.I):
        out.append(f"{dc} data: say where the audit records go (constitution VX-15)")
    return out


def roadmap(text: str, ctx: dict) -> list[str]:
    out = _needs(text, ("Goals", "Initiatives"))
    inis = formats.initiatives(text)
    seen: list[str] = []
    built: set[str] = set()
    served: set[str] = set()
    total = 0.0
    for i in inis:
        if not i["id"]:
            out.append(f"Initiatives: '{i['line']}' isn't in the form '- INI-1: <title> [horizon: ...; budget: ...; "
                       "goals: ...; after: ...]'")
            continue
        if i["horizon"] not in HORIZONS:
            out.append(f"{i['id']}: horizon must be now, next or later")
        if i["budget_usd"] <= 0:
            out.append(f"{i['id']}: needs a budget in US dollars")
        total += i["budget_usd"]
        for a in i["after"]:
            if a not in seen:
                out.append(f"{i['id']}: comes after {a}, which isn't listed before it")
        for g in i["goals"]:
            if g not in ctx.get("goals", ()):
                out.append(f"{i['id']}: {g} isn't a goal in the brief")
        served |= set(i["goals"])
        if not i["features"]:
            out.append(f"{i['id']}: list its candidate features")
        for f in i["features"]:
            if not f["title"]:
                out.append(f"{i['id']}: '{f['line']}' isn't in the form '- <feature>: <what it does> [builds: <component>]'")
                continue
            for c in f["builds"]:
                if c not in ctx.get("components", ()):
                    out.append(f"{f['title']}: builds '{c}', which isn't a component in the architecture")
            built |= set(f["builds"])
        seen.append(i["id"])
    if inis and inis[0].get("id"):
        first = inis[0]
        if first["after"]:
            out.append(f"{first['id']}: the first initiative can't wait for another")
        if first["horizon"] and first["horizon"] != "now":
            out.append(f"{first['id']}: the first initiative is now")
        if not ctx.get("existing"):
            reach = max((len(f.get("builds") or []) for f in first["features"]), default=0)
            if reach < 2:
                out.append(f"{first['id']}: the first initiative is the thin working version: one feature must build "
                           "an end-to-end path through at least two components")
    for g in ctx.get("goals", ()):
        if g not in served:
            out.append(f"{g}: no initiative serves it")
    for c in ctx.get("components", ()):
        if c not in built:
            out.append(f"{c}: no feature builds it")
    budget = ctx.get("budget_usd") or 0
    if budget and total > budget:
        out.append(f"Initiative budgets add up to ${total:,.0f}, over the brief's ${budget:,.0f}")
    return out


CHECKS = {"brief": brief, "architecture": architecture, "roadmap": roadmap}


def run(doc: str, text: str, ctx: dict) -> list[str]:
    return CHECKS[doc](text, ctx)

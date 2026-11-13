"""The capacity plan: from a product's volumes and load to replicas, CPU, memory, database sizes,
Kafka partitions and monthly cost, per environment and per stage. The agent writes the
assumptions (the brief's volumes, the architecture's load per journey); code does the arithmetic,
so no replica count is ever a model's guess.

    brief       ## Scale         launch: 300 clinics; year one: 2000 clinics; busy hour: 3 times the
                                 daily average, over a 10-hour day
                ## Availability  tier: 2: <why>
                ## Product       run budget: pilot 300; growth 1500; scale 4000   (US dollars a month)
    architecture
                ## Load          J1: Check a pet in (clinic): 60 a day per clinic; cln-bff 20 requests;
                                 cln-intake-api 20 requests; cln-intake-worker 4 events
                ## Scaling       <component>: stateless; scales on cpu; first limit: <what runs out first>
                ## Failure       <component> -> <dependency>: timeout 2000 ms; retries 2; idempotency: <key>;
                                 when down: <what the person sees>
                                 <resource>: recovery: <how the tier's RTO and RPO are met>
    platform    capacity.yaml (tiers, headroom, starting throughput per role, costs) and the AWS
                catalog's sizes (connections, multi_az)

    plan(...) -> {"tier", "stages": {pilot|growth|scale: {...}}, "components": [...], "databases": [...],
                  "partitions", "environments": {dev|staging: {...}}, "fixed", "findings"}

Production starts at the pilot stage and steps up when people approve it (valxel_sdlc.capacity
triggers); dev and staging are a fixed small size. The approved plan is kept as the product's
runtime/capacity.yaml, and every deployment carries its environment's part (binding_configs).
"""

from __future__ import annotations

import math
import re

import yaml

from .agents.parse import section

DEFAULTS: dict = {
    "tiers": {
        1: {"availability_pct": 99.95, "rto_minutes": 15, "rpo_minutes": 1, "zones": 3, "min_replicas": 3,
            "multi_az_database": True},
        2: {"availability_pct": 99.9, "rto_minutes": 60, "rpo_minutes": 15, "zones": 2, "min_replicas": 2,
            "multi_az_database": True},
        3: {"availability_pct": 99.5, "rto_minutes": 240, "rpo_minutes": 1440, "zones": 1, "min_replicas": 1,
            "multi_az_database": False},
    },
    "strict_data_tier": 2,
    "headroom": {"utilization": 0.6, "growth": 10},
    "stages": ["pilot", "growth", "scale"],
    "triggers": {"step_up_share": 0.5, "step_up_days": 5, "step_down_share": 0.2},
    "runtimes": {
        "provider": {"unit": "requests", "per_pod": 100, "cpu": 1.0, "memory_gib": 1.0, "pilot_cpu": 0.5, "pool": 10},
        "bff": {"unit": "requests", "per_pod": 200, "cpu": 0.5, "memory_gib": 0.5, "pilot_cpu": 0.25, "pool": 0},
        "consumer": {"unit": "requests", "per_pod": 200, "cpu": 0.5, "memory_gib": 0.5, "pilot_cpu": 0.25, "pool": 0},
        "event-worker": {"unit": "events", "per_pod": 50, "cpu": 0.5, "memory_gib": 0.5, "pilot_cpu": 0.25, "pool": 5},
        "mcp-server": {"unit": "requests", "per_pod": 100, "cpu": 0.5, "memory_gib": 0.5, "pilot_cpu": 0.25, "pool": 5},
        "agent": {"unit": "requests", "per_pod": 20, "cpu": 1.0, "memory_gib": 1.0, "pilot_cpu": 0.5, "pool": 5},
    },
    "environments": {"dev": {"replicas": 1, "database": "paused"}, "staging": {"replicas": 2, "database": "paused"}},
    "compute_usd_per_vcpu_month": 30,
    "fixed_monthly_usd": {"data_plane_cluster": 73},
    "fixed_unpriced": ["NAT gateways", "load balancers", "Kafka brokers", "production observability plane"],
    "clusters_per_product": 2,
    "awake": "Mon-Fri 07:00-19:00 America/Chicago",  # when non-production runs; it scales to zero otherwise
}
SCALED_ROLES = tuple(DEFAULTS["runtimes"])  # roles that run as pods and take load
DATABASE_TYPES = ("postgres",)  # catalog types whose sizes carry connections


class CapacityError(ValueError):
    pass


def load_platform(text: str) -> dict:
    """The platform's capacity.yaml over the defaults (a missing file means the defaults)."""
    raw = yaml.safe_load(text or "") or {}
    if not isinstance(raw, dict):
        raise CapacityError("capacity.yaml must be a mapping")
    out = {**DEFAULTS, **{k: v for k, v in raw.items() if k in DEFAULTS}}
    out["tiers"] = {int(k): {**DEFAULTS["tiers"].get(int(k), {}), **(v or {})} for k, v in (raw.get("tiers") or DEFAULTS["tiers"]).items()}
    out["runtimes"] = {**DEFAULTS["runtimes"], **(raw.get("runtimes") or {})}
    out["headroom"] = {**DEFAULTS["headroom"], **(raw.get("headroom") or {})}
    out["triggers"] = {**DEFAULTS["triggers"], **(raw.get("triggers") or {})}
    for role, r in out["runtimes"].items():
        for key in ("unit", "per_pod", "cpu", "memory_gib"):
            if key not in r:
                raise CapacityError(f"capacity.yaml: runtime {role} has no {key}")
    u = float(out["headroom"]["utilization"])
    if not 0 < u <= 1:
        raise CapacityError("capacity.yaml: headroom.utilization must be between 0 and 1")
    return out


# ------------------------------------------------------------------------------------ parsing

def _bullets(text: str, heading: str) -> list[str]:
    return [ln.strip()[2:].strip() for ln in section(text, heading).splitlines() if ln.strip().startswith("- ")]


_NUM = r"(\d[\d,]*(?:\.\d+)?)"
_COUNT = re.compile(rf"^{_NUM}\s+([A-Za-z][\w -]*?)\s*(?:\((?:[A-Z]\d+(?:,\s*)?)+\))?\s*$")
_BUSY = re.compile(rf"{_NUM}\s*(?:times|x)\b.*?(?:over\s+an?\s+)?{_NUM}\s*-?\s*hour", re.I)
_TIER = re.compile(r"^tier\s*:\s*([123])\s*(?::\s*(.*))?$", re.I)
_BUDGET = re.compile(rf"\b(pilot|growth|scale)\s*\$?\s*{_NUM}", re.I)
_JOURNEY = re.compile(rf"^(J\d+)\s*:\s*(.+?)\s*\(([^)]+)\)\s*:\s*{_NUM}\s+a day per\s+[^;]+;\s*(.+)$", re.I)
_DEMAND = re.compile(rf"^([a-z][a-z0-9-]*)\s+{_NUM}\s+(requests?|events?)$", re.I)
_SCALING = re.compile(r"^([a-z][a-z0-9-]*)\s*:\s*(stateless|stateful)\s*;\s*scales on\s+([a-z ]+?)\s*;\s*"
                      r"first limit\s*:\s*(.+)$", re.I)
_DEPEND = re.compile(r"^([a-z][a-z0-9-]*)\s*->\s*([\w./-]+)\s*:\s*(.+)$", re.I)
_RECOVERY = re.compile(r"^([a-z][a-z0-9-]*)\s*:\s*recovery\s*:\s*(.+)$", re.I)


def _n(s: str) -> float:
    return float(s.replace(",", ""))


def singular(word: str) -> str:
    w = word.strip().lower()
    return w[:-1] if w.endswith("s") and not w.endswith("ss") else w


def scale(brief: str) -> dict:
    """The brief's volumes: {launch, year_one, unit, busy_factor, day_hours, problems}."""
    out = {"launch": 0.0, "year_one": 0.0, "unit": "", "busy_factor": 0.0, "day_hours": 0.0, "where": "", "problems": []}
    for b in _bullets(brief, "Scale"):
        key, _, value = b.partition(":")
        key, value = key.strip().lower(), value.strip()
        if key in ("launch", "year one"):
            m = _COUNT.match(value)
            if not m:
                out["problems"].append(f"Scale: write '{key}: <number> <what is counted>', not '{value}'")
                continue
            out["launch" if key == "launch" else "year_one"] = _n(m.group(1))
            unit = singular(m.group(2))
            if out["unit"] and unit != out["unit"]:
                out["problems"].append(f"Scale: launch and year one count different things ({out['unit']}, {unit})")
            out["unit"] = out["unit"] or unit
        elif key == "busy hour":
            m = _BUSY.search(value)
            if not m:
                out["problems"].append("Scale: write 'busy hour: <n> times the daily average, over a <h>-hour day'")
                continue
            out["busy_factor"], out["day_hours"] = _n(m.group(1)), _n(m.group(2))
        elif key == "where":
            out["where"] = value
    return out


def tier(brief: str) -> tuple[int, str]:
    for b in _bullets(brief, "Availability"):
        m = _TIER.match(b)
        if m:
            return int(m.group(1)), (m.group(2) or "").strip()
    return 0, ""


def run_budget(brief: str) -> dict[str, float]:
    for b in _bullets(brief, "Product"):
        if b.lower().startswith("run budget"):
            return {m.group(1).lower(): _n(m.group(2)) for m in _BUDGET.finditer(b)}
    return {}


def journeys(arch: str) -> list[dict]:
    out = []
    for b in _bullets(arch, "Load"):
        m = _JOURNEY.match(b)
        if not m:
            out.append({"id": "", "line": b})
            continue
        demand, bad = [], []
        for part in [p.strip() for p in m.group(5).split(";") if p.strip()]:
            d = _DEMAND.match(part)
            if d:
                demand.append({"component": d.group(1), "count": _n(d.group(2)),
                               "unit": "events" if d.group(3).lower().startswith("event") else "requests"})
            else:
                bad.append(part)
        out.append({"id": m.group(1), "name": m.group(2), "unit": singular(m.group(3)), "per_day": _n(m.group(4)),
                    "demand": demand, "bad": bad, "line": b})
    return out


def scaling(arch: str) -> list[dict]:
    out = []
    for b in _bullets(arch, "Scaling"):
        m = _SCALING.match(b)
        out.append({"component": m.group(1), "state": m.group(2).lower(), "on": m.group(3).strip().lower(),
                    "limit": m.group(4).strip()} if m else {"component": "", "line": b})
    return out


def failures(arch: str) -> dict:
    """{"deps": [{from, to, fields}], "recovery": {resource: how}, "bad": [lines]}"""
    deps, recovery, bad = [], {}, []
    for b in _bullets(arch, "Failure"):
        m = _DEPEND.match(b)
        if m:
            fields = {}
            for part in (x.strip() for x in m.group(3).split(";") if x.strip()):
                if ":" in part:
                    k, v = part.split(":", 1)
                else:
                    k, _, v = part.partition(" ")
                fields[k.strip().lower()] = v.strip()
            deps.append({"from": m.group(1), "to": m.group(2), "fields": fields, "line": b})
            continue
        r = _RECOVERY.match(b)
        if r:
            recovery[r.group(1)] = r.group(2).strip()
        else:
            bad.append(b)
    return {"deps": deps, "recovery": recovery, "bad": bad}


# ---------------------------------------------------------------------------------- the plan

def _pods(load: float, per_pod: float) -> int:
    return max(1, math.ceil(round(load / per_pod, 6))) if per_pod > 0 else 1


def _per_second(per_day: float, hours: float, factor: float) -> float:
    return per_day / (hours * 3600) * factor if hours else 0.0


def _db_size(catalog_type: dict, connections: int, multi_az: bool, env: str = "production") -> tuple[str, dict]:
    sizes = catalog_type.get("sizes") or {}
    fits = [(float(s.get("monthly_usd", 0)), name, s) for name, s in sizes.items()
            if s.get("connections") and int(s["connections"]) >= connections
            and (not multi_az or s.get("multi_az")) and env in (s.get("environments") or [env])]
    if not fits:
        return "", {}
    _, name, s = min(fits, key=lambda x: x[0])
    return name, s


def plan(brief: str, arch: str, platform: dict | None = None, catalog: dict | None = None,
         components: list[dict] | None = None, resources: list[dict] | None = None,
         data_class: str = "general") -> dict:
    """The capacity plan, with findings for anything that doesn't add up."""
    from .inception import formats
    from .resources import parse_resources

    cfg = platform or load_platform("")
    catalog = catalog or {}
    comps = components if components is not None else [c for c in formats.components(arch) if c.get("name")]
    res = resources if resources is not None else [r for r in parse_resources(arch) if r.get("type")]
    sc, (t, _why), budget = scale(brief), tier(brief), run_budget(brief)
    findings = list(sc["problems"])
    tiers = cfg["tiers"]
    if t not in tiers:
        findings.append("Availability: name a tier, '- tier: 1|2|3: <why>'")
        t = 3
    floor = tiers[t]
    util, growth = float(cfg["headroom"]["utilization"]), float(cfg["headroom"]["growth"])
    share = float(cfg["triggers"]["step_up_share"]) or 0.5

    # Demand per component per day, per counted thing (clinic, user, store...).
    per_unit: dict[str, dict[str, float]] = {}
    for j in journeys(arch):
        if not j["id"]:
            findings.append(f"Load: '{j['line']}' isn't in the form '- J1: <journey> (<unit>): <n> a day per <unit>; "
                            "<component> <n> requests; <component> <n> events'")
            continue
        if sc["unit"] and j["unit"] != sc["unit"]:
            findings.append(f"{j['id']}: counts per {j['unit']}, but the brief's volumes count {sc['unit']}s")
        for part in j["bad"]:
            findings.append(f"{j['id']}: '{part}' isn't '<component> <n> requests' or '<component> <n> events'")
        for d in j["demand"]:
            per_unit.setdefault(d["component"], {"requests": 0.0, "events": 0.0})[d["unit"]] += j["per_day"] * d["count"]

    names = {c["name"]: c for c in comps}
    for c in per_unit:
        if c not in names:
            findings.append(f"Load: '{c}' isn't a component")
    hours, factor = sc["day_hours"], sc["busy_factor"]
    usd_cpu = float(cfg["compute_usd_per_vcpu_month"])
    rows = []
    for c in comps:
        rt = cfg["runtimes"].get(c["role"])
        if not rt:
            continue
        load = per_unit.get(c["name"])
        if not load:
            findings.append(f"{c['name']}: no journey in '## Load' sends it {rt['unit']}")
            continue
        daily = load[rt["unit"]]
        if not daily:
            other = "events" if rt["unit"] == "requests" else "requests"
            findings.append(f"{c['name']} ({c['role']}) scales on {rt['unit']}, but the load lines only give it {other}")
            continue
        launch = _per_second(daily * sc["launch"], hours, factor)
        year = _per_second(daily * sc["year_one"], hours, factor)
        top = year * growth
        cap = float(rt["per_pod"]) * util
        pilot_cap = cap * float(rt.get("pilot_cpu", rt["cpu"])) / float(rt["cpu"])
        grow_min = max(int(floor["min_replicas"]), _pods(launch, cap))
        grow_max = max(grow_min, _pods(top, cap))
        pilot_min = _pods(launch, pilot_cap)
        # The pilot's ceiling covers the launch busy hour over the step-up share (twice it, at 0.5):
        # the trigger proposes the growth stage before the pilot runs out.
        pilot_max = max(pilot_min, _pods(launch / share, pilot_cap))
        small_cpu = float(rt.get("pilot_cpu", rt["cpu"]))
        mem = float(rt["memory_gib"])
        small_mem = max(0.25, mem / 2) if small_cpu < float(rt["cpu"]) else mem
        rows.append({
            "component": c["name"], "role": c["role"], "unit": rt["unit"],
            "repo": (c.get("repo") or "").rsplit("/", 1)[-1] or c["name"],
            "busy": {"launch": round(launch, 1), "year_one": round(year, 1), "top": round(top, 1)},
            "per_pod": float(rt["per_pod"]), "pool": int(rt.get("pool", 0)),
            "cpu": float(rt["cpu"]), "pilot_cpu": float(rt.get("pilot_cpu", rt["cpu"])),
            "memory_gib": float(rt["memory_gib"]),
            "production": {"pilot": {"min": pilot_min, "max": pilot_max, "cpu": small_cpu, "memory_gib": small_mem},
                           "growth": {"min": grow_min, "max": grow_max, "cpu": float(rt["cpu"]), "memory_gib": mem},
                           "scale": {"min": grow_min, "max": grow_max, "cpu": float(rt["cpu"]), "memory_gib": mem}},
            "environments": {e: {"min": int(v["replicas"]), "max": int(v["replicas"]), "cpu": small_cpu,
                                 "memory_gib": small_mem}
                             for e, v in cfg["environments"].items()},
        })

    # Databases: every pod that reaches one opens its pool.
    dbs = []
    by_name = {r["component"]: r for r in rows}
    for r in res:
        ctype = (catalog.get("types") or {}).get(r["type"]) or {}
        if not any(r["type"].startswith(p) for p in DATABASE_TYPES):
            continue
        users = sorted(set(r["access"]) & set(by_name))
        need = {stage: sum(by_name[u]["production"][stage]["max"] * by_name[u]["pool"] for u in users)
                for stage in ("pilot", "growth", "scale")}
        entry = {"name": r["name"], "type": r["type"], "users": users, "connections": need, "sizes": {}, "monthly_usd": {}}
        for stage in ("pilot", "growth", "scale"):
            ha = bool(floor["multi_az_database"]) and stage != "pilot"
            size, s = _db_size(ctype, need[stage], ha)
            if not size:
                if ctype.get("sizes"):
                    findings.append(f"{r['name']}: no {'Multi-AZ ' if ha else ''}size of {r['type']} takes "
                                    f"{need[stage]} connections at the {stage} stage; lower the pools, add a "
                                    "connection pooler, or split the data")
                continue
            entry["sizes"][stage], entry["monthly_usd"][stage] = size, float(s.get("monthly_usd", 0))
        for env, v in cfg["environments"].items():
            want = v.get("database") or "small"
            size = want if want in (ctype.get("sizes") or {}) else ("small" if "small" in (ctype.get("sizes") or {}) else "")
            if size:
                entry["sizes"][env] = size
                entry["monthly_usd"][env] = float((ctype["sizes"][size] or {}).get("monthly_usd", 0))
        dbs.append(entry)

    workers = [r["production"]["growth"]["max"] for r in rows if r["unit"] == "events"]
    partitions = 2 ** math.ceil(math.log2(max(workers))) if workers else 0

    stages = {}
    for stage in ("pilot", "growth", "scale"):
        at_min = sum(r["production"][stage]["min"] * r["production"][stage]["cpu"] for r in rows)
        at_max = sum(r["production"][stage]["max"] * r["production"][stage]["cpu"] for r in rows)
        db = sum(d["monthly_usd"].get(stage, 0.0) for d in dbs)
        stages[stage] = {"vcpu_min": round(at_min, 2), "vcpu_max": round(at_max, 2),
                         "compute_usd": round(at_min * usd_cpu), "compute_usd_max": round(at_max * usd_cpu),
                         "database_usd": round(db), "monthly_usd": round(at_min * usd_cpu + db),
                         "monthly_usd_max": round(at_max * usd_cpu + db),
                         "availability": tiers[3] if stage == "pilot" else floor}
    envs = {}
    for env in cfg["environments"]:
        vcpu = sum(r["environments"][env]["min"] * r["environments"][env]["cpu"] for r in rows)
        db = sum(d["monthly_usd"].get(env, 0.0) for d in dbs)
        envs[env] = {"vcpu": round(vcpu, 2), "monthly_usd": round(vcpu * usd_cpu + db)}
    fixed_each = sum(float(v) for v in (cfg.get("fixed_monthly_usd") or {}).values())
    fixed = {"monthly_usd": round(fixed_each * int(cfg.get("clusters_per_product", 2))),
             "unpriced": list(cfg.get("fixed_unpriced") or [])}

    for stage, limit in budget.items():
        total = stages.get(stage, {}).get("monthly_usd", 0) + fixed["monthly_usd"]
        if stage in stages and total > limit:
            findings.append(f"The {stage} stage costs about ${total:,.0f} a month with the clusters, over the brief's "
                            f"run budget of ${limit:,.0f}")
    if data_class in ("phi", "payments") and t > int(cfg.get("strict_data_tier", 2)):
        findings.append(f"Availability: {data_class} products target tier {cfg.get('strict_data_tier', 2)} or better "
                        "(the pilot still starts at tier 3)")
    return {"tier": t, "tier_targets": floor, "scale": {k: sc[k] for k in ("launch", "year_one", "unit", "busy_factor",
                                                                           "day_hours")},
            "headroom": {"utilization": util, "growth": growth}, "components": rows, "databases": dbs,
            "partitions": partitions, "stages": stages, "environments": envs, "fixed": fixed, "budget": budget,
            "findings": findings}


# ------------------------------------------------------------------------------ the documents

def render(p: dict) -> str:
    """The '## Capacity' section the tool adds to the product architecture for the gate."""
    if not p.get("components"):
        return ""
    sc = p["scale"]
    t = p["tier_targets"]
    head = (f"## Capacity\n\nComputed by the tool from the brief's volumes ({sc['launch']:,.0f} {sc['unit']}s at launch, "
            f"{sc['year_one']:,.0f} after a year, a busy hour {sc['busy_factor']:g} times the average over a "
            f"{sc['day_hours']:g}-hour day) and the Load section, with pods at no more than "
            f"{p['headroom']['utilization']:.0%} at the busy hour. Target tier {p['tier']}: "
            f"{t['availability_pct']}% availability, recovery within {t['rto_minutes']} minutes, at most "
            f"{t['rpo_minutes']} minutes of data lost. Production starts at the pilot stage (tier 3 availability, "
            "data protection unchanged) and steps up when people approve it.\n")
    rows = ["| Component | Busy hour, launch / year one | Pilot | Growth and scale | Staging | Dev |",
            "| --- | --- | --- | --- | --- | --- |"]
    for r in p["components"]:
        pr = r["production"]
        rows.append(f"| {r['component']} ({r['role']}) | {r['busy']['launch']:g} / {r['busy']['year_one']:g} {r['unit']}/s "
                    f"| {pr['pilot']['min']}–{pr['pilot']['max']} × {pr['pilot']['cpu']:g} CPU "
                    f"| {pr['growth']['min']}–{pr['growth']['max']} × {pr['growth']['cpu']:g} CPU "
                    f"| {r['environments'].get('staging', {}).get('min', '-')} | {r['environments'].get('dev', {}).get('min', '-')} |")
    for d in p["databases"]:
        s = d["sizes"]
        rows.append(f"| {d['name']} (database) | up to {d['connections'].get('scale', 0)} connections "
                    f"| {s.get('pilot', '-')} | {s.get('growth', '-')} | {s.get('staging', '-')} | {s.get('dev', '-')} |")
    if p["partitions"]:
        rows.append(f"| Kafka topics | {p['partitions']} partitions, set now | {p['partitions']} | {p['partitions']} "
                    f"| {p['partitions']} | {p['partitions']} |")
    st = p["stages"]
    cost = ["", "| Production stage | Monthly cost (approximate) |", "| --- | --- |",
            f"| Pilot | ${st['pilot']['monthly_usd']:,} |", f"| Growth | ${st['growth']['monthly_usd']:,} |",
            f"| Scale | ${st['scale']['monthly_usd']:,} to ${st['scale']['monthly_usd_max']:,} at the maximum |",
            "", f"Dev costs about ${p['environments'].get('dev', {}).get('monthly_usd', 0):,} and staging about "
            f"${p['environments'].get('staging', {}).get('monthly_usd', 0):,} a month. The product's own clusters add "
            f"${p['fixed']['monthly_usd']:,} a month before any traffic; not yet priced: "
            f"{', '.join(p['fixed']['unpriced']) or 'nothing'}."]
    return head + "\n" + "\n".join(rows) + "\n" + "\n".join(cost) + "\n"


def capacity_yaml(p: dict, product_id: str) -> str:
    """runtime/capacity.yaml: the approved plan the pipeline applies on every deployment."""
    doc = {"product_id": product_id, "tier": p["tier"], "stage": "pilot", "partitions": p["partitions"],
           "headroom": p["headroom"],
           "components": {r["repo"]: {
               "component": r["component"], "role": r["role"], "busy": r["busy"], "memory_gib": r["memory_gib"], "per_pod": r["per_pod"], "unit": r["unit"],
               "production": r["production"], **r["environments"]} for r in p["components"]},
           "databases": {d["name"]: d["sizes"] for d in p["databases"]}}
    return dump(doc)


def load_product(text: str) -> dict:
    """A product's runtime/capacity.yaml (empty when it has none)."""
    try:
        raw = yaml.safe_load(text or "") or {}
    except yaml.YAMLError as exc:
        raise CapacityError(f"not YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise CapacityError("must be a mapping")
    if raw.get("stage", "pilot") not in DEFAULTS["stages"]:
        raise CapacityError(f"stage must be one of {', '.join(DEFAULTS['stages'])}")
    return raw


def binding_configs(capacity: dict, component: str, env: str) -> tuple[dict, dict] | None:
    """For a ReleaseBinding: (componentTypeEnvironmentConfigs, traitEnvironmentConfigs), or None
    when the product has no capacity plan for this component."""
    c = (capacity.get("components") or {}).get(component.rsplit("/", 1)[-1])
    if not c:
        return None
    if env == "production":
        stage = capacity.get("stage") or "pilot"
        spec = (c.get("production") or {}).get(stage)
    else:
        spec = c.get(env)
    if not spec:
        return None
    cpu = float(spec.get("cpu") or 0.5)
    mem = float(spec.get("memory_gib") or c.get("memory_gib") or 0.5)
    configs = {"replicas": int(spec["min"]),
               "resources": {"requests": {"cpu": f"{int(cpu * 1000)}m", "memory": f"{int(mem * 1024)}Mi"},
                             "limits": {"memory": f"{int(mem * 1024)}Mi"}}}
    traits = {}
    if int(spec["max"]) > int(spec["min"]):
        traits["autoscaler"] = {"minReplicas": int(spec["min"]), "maxReplicas": int(spec["max"]),
                                "targetCPUUtilizationPercentage": 60}
    return configs, traits


def check_doc(arch: str, comps: list[dict], resources: list[dict]) -> list[str]:
    """The Scaling and Failure sections: every scaled component and every database accounted for."""
    out = []
    names = {c["name"]: c for c in comps if c.get("name")}
    scaled = {n for n, c in names.items() if c["role"] in SCALED_ROLES}
    seen = set()
    for s in scaling(arch):
        if not s["component"]:
            out.append(f"Scaling: '{s['line']}' isn't '- <component>: stateless|stateful; scales on cpu; first limit: <what>'")
            continue
        seen.add(s["component"])
        if s["component"] not in names:
            out.append(f"Scaling: '{s['component']}' isn't a component")
        if s["on"] != "cpu":
            out.append(f"{s['component']}: scale on cpu (the platform scales pods on CPU for now, not {s['on']})")
        if len(s["limit"].split()) < 3:
            out.append(f"{s['component']}: say what runs out first when it scales, and what happens then")
    for n in sorted(scaled - seen):
        out.append(f"{n}: say how it scales under '## Scaling'")
    f = failures(arch)
    for b in f["bad"]:
        out.append(f"Failure: '{b}' isn't '- <component> -> <dependency>: timeout <n> ms; retries <n>; idempotency: "
                   "<key>; when down: <what people see>' or '- <resource>: recovery: <how>'")
    callers = set()
    for d in f["deps"]:
        callers.add(d["from"])
        fields = d["fields"]
        if d["from"] not in names:
            out.append(f"Failure: '{d['from']}' isn't a component")
        if not re.search(r"\d", fields.get("timeout", "")):
            out.append(f"{d['from']} -> {d['to']}: give the timeout in ms")
        if "retries" not in fields:
            out.append(f"{d['from']} -> {d['to']}: say how many retries (0 is an answer)")
        if not fields.get("idempotency"):
            out.append(f"{d['from']} -> {d['to']}: name the idempotency key, or 'none: <why it's safe>'")
        if len(fields.get("when down", "").split()) < 3:
            out.append(f"{d['from']} -> {d['to']}: say what people see when it's down")
    for n, c in names.items():
        if c["role"] in ("bff", "consumer", "event-worker", "agent", "mcp-server") and n not in callers:
            out.append(f"{n}: say under '## Failure' what happens when what it calls is slow or down")
    for r in resources:
        if r.get("type", "").startswith(("postgres", "dynamodb", "redis", "s3")) and r["name"] not in f["recovery"]:
            out.append(f"{r['name']}: say under '## Failure' how it recovers ('- {r['name']}: recovery: <how the tier's "
                       "RTO and RPO are met>')")
    return out


# ------------------------------------------------------------------------- after launch

_ADDED = re.compile(r"([a-z][a-z0-9-]*)\s*\+\s*(\d[\d,]*(?:\.\d+)?)\s*(requests?|events?)\s*/\s*s", re.I)


def added_load(hld: str) -> dict[str, float]:
    """A feature design's added busy-hour load after a year: '- Added load: cln-intake-api +20 requests/s'."""
    m = re.search(r"^##\s+Performance\s*$(.*?)(?=^##\s|\Z)", hld or "", re.M | re.S)
    out: dict[str, float] = {}
    for line in (m.group(1) if m else "").splitlines():
        if re.match(r"^\s*[-*]\s*added load\s*:", line, re.I):
            for name, n, _unit in _ADDED.findall(line):
                out[name] = out.get(name, 0.0) + _n(n)
    return out


def _key(cap: dict, name: str) -> str:
    comps = cap.get("components") or {}
    if name in comps:
        return name
    return next((k for k, v in comps.items() if v.get("component") == name or k.endswith(f"-{name}")), "")


def change(cap: dict, hld: str) -> list[dict]:
    """What a feature's added load does to the approved maximums: [{repo, busy_from, busy_to, max_from, max_to}]."""
    if not cap:
        return []
    h = cap.get("headroom") or DEFAULTS["headroom"]
    util, growth = float(h.get("utilization", 0.6)), float(h.get("growth", 10))
    out = []
    for name, add in added_load(hld).items():
        key = _key(cap, name)
        if not key:
            out.append({"repo": name, "error": f"{name} isn't in the product's capacity plan"})
            continue
        c = cap["components"][key]
        busy = float((c.get("busy") or {}).get("year_one", 0))
        per_pod = float(c.get("measured_per_pod") or c.get("per_pod") or 0)
        old = int(((c.get("production") or {}).get("growth") or {}).get("max", 0))
        new = max(old, _pods((busy + add) * growth, per_pod * util))
        out.append({"repo": key, "busy_from": round(busy, 1), "busy_to": round(busy + add, 1), "unit": c.get("unit", ""),
                    "max_from": old, "max_to": new})
    return out


def apply_change(cap: dict, changes: list[dict]) -> dict:
    import copy

    doc = copy.deepcopy(cap)
    for ch in changes:
        c = (doc.get("components") or {}).get(ch.get("repo") or "")
        if not c or ch.get("error"):
            continue
        c.setdefault("busy", {})["year_one"] = ch["busy_to"]
        for stage in ("growth", "scale"):
            if stage in (c.get("production") or {}):
                c["production"][stage]["max"] = ch["max_to"]
    return doc


def calibrate(cap: dict, repo: str, per_pod: float, keep: int = 9) -> dict:
    """Record a load test's measured throughput per pod; after three, their median replaces the
    starting figure the plan was computed with."""
    import copy
    import statistics

    doc = copy.deepcopy(cap)
    c = (doc.get("components") or {}).get(_key(doc, repo))
    if not c or per_pod <= 0:
        return doc
    samples = (list(c.get("measured") or []) + [round(float(per_pod), 1)])[-keep:]
    c["measured"] = samples
    if len(samples) >= 3:
        c["measured_per_pod"] = round(statistics.median(samples), 1)
    return doc


def review(cap: dict, observed: dict[str, dict], cfg: dict | None = None) -> list[dict]:
    """Production against the plan: the steps to propose. observed[repo] = {"busy": requests or
    events per second at the busy hour, "days_over": days in the last fortnight above the step-up
    share, "days_at_max": days the autoscaler sat at its maximum, "month_share": the month's highest
    busy-hour share of capacity}."""
    cfg = cfg or DEFAULTS
    t = {**DEFAULTS["triggers"], **(cfg.get("triggers") or {})}
    stage = cap.get("stage") or "pilot"
    stages = list(DEFAULTS["stages"])
    out = []
    for repo, o in observed.items():
        c = (cap.get("components") or {}).get(_key(cap, repo))
        if not c:
            continue
        spec = (c.get("production") or {}).get(stage) or {}
        per_pod = float(c.get("measured_per_pod") or c.get("per_pod") or 0)
        cpu_ratio = float(spec.get("cpu") or 1) / float(((c.get("production") or {}).get("growth") or {}).get("cpu") or 1)
        room = int(spec.get("max") or 0) * per_pod * cpu_ratio
        share = float(o.get("busy", 0)) / room if room else 0.0
        if o.get("days_at_max", 0) >= 1:
            out.append({"repo": repo, "kind": "raise_max", "share": round(share, 2),
                        "why": f"the autoscaler sat at its maximum ({spec.get('max')}) on {o['days_at_max']} day(s)"})
        if o.get("days_over", 0) >= int(t["step_up_days"]) and stage != stages[-1]:
            out.append({"repo": repo, "kind": "step_up", "to": stages[stages.index(stage) + 1], "share": round(share, 2),
                        "why": f"the busy hour used more than {t['step_up_share']:.0%} of the {stage} stage's capacity "
                               f"on {o['days_over']} days in a fortnight"})
        if float(o.get("month_share", 1)) < float(t["step_down_share"]) and stage != stages[0]:
            out.append({"repo": repo, "kind": "step_down", "to": stages[stages.index(stage) - 1],
                        "share": round(float(o["month_share"]), 2),
                        "why": f"the busy hour stayed under {t['step_down_share']:.0%} of capacity for a month"})
    return out


def with_stage(cap: dict, stage: str) -> dict:
    if stage not in DEFAULTS["stages"]:
        raise CapacityError(f"stage must be one of {', '.join(DEFAULTS['stages'])}")
    return {**cap, "stage": stage}


def stage_cost(cap: dict, stage: str, usd_per_vcpu: float = DEFAULTS["compute_usd_per_vcpu_month"]) -> float:
    """Production's compute at a stage's minimums (databases are priced from the catalog separately)."""
    return round(sum(float(((c.get("production") or {}).get(stage) or {}).get("min", 0))
                     * float(((c.get("production") or {}).get(stage) or {}).get("cpu", 0))
                     for c in (cap.get("components") or {}).values()) * usd_per_vcpu)


def dump(cap: dict) -> str:
    return ("# The capacity the product gate approved (valxel_sdlc.capacity). The pipeline applies it on every\n"
            "# deployment; `stage` is production's current stage, changed only by an approved step.\n"
            + yaml.safe_dump(cap, sort_keys=False))


def propose(services, product_id: str, new: dict, *, branch: str, title: str, why: str) -> dict | None:
    """A pull request to the product's governance repo changing runtime/capacity.yaml. The product
    owners and architects review it (CODEOWNERS); merged, the next deployment applies it."""
    gov = services.settings.entry(product_id).governance
    src = services.governance.source
    sha = src.resolve(gov.org, gov.repo, gov.ref)
    current = load_product(src.read(gov.org, gov.repo, "runtime/capacity.yaml", sha, optional=True) or "")
    if current == new:
        return None
    gh = services.github
    gh.put_file(repo=gov.full_name, branch=branch, path="runtime/capacity.yaml", content=dump(new), message=title)
    pr = gh.open_pr(repo=gov.full_name, branch=branch, title=title, files=["runtime/capacity.yaml"],
                    labels=["valxel", "capacity"], body=why + "\n\nMerge to apply it from the next deployment.")
    return {"url": pr["url"], "files": ["runtime/capacity.yaml"]}

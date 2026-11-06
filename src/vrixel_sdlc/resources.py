"""AWS managed services, from the design to every environment (constitution VX-19).

The platform catalog (`aws-resources.yaml` in the platform governance repo) lists the services a
product may use: what each is for, which data classes may use it, the IAM actions behind "reads"
and "writes", sizes with planning costs, and the emulator for tests. Each type is an OpenChoreo
ClusterResourceType backed by a Crossplane Composition (vrixel-tools/platform-resources).

1. Design (architect, checked here, approved at G4). The HLD's Resources section names each
   service a feature needs and who may use it:

       ## Resources
       - field-readings: kinesis-stream; retention_hours=168
         writes: agtech-sensor-api; reads: agtech-field-worker
       - field-state: dynamodb-table; partition_key=field_id; size production=large
         reads, writes: agtech-field-worker

   A service other than the platform defaults needs an ADR naming it. G4 shows the approver the
   services, each component's access and the monthly cost per environment.

2. Build (coders). Each component that uses a resource declares it in workload.yaml
   (`dependencies.resources`, outputs bound to environment variables) and reaches it behind a port
   with the emulator in tests.

3. Environments (pipeline). OpenChoreo creates a Resource per entry, plus a workload identity per
   component that uses one: an IAM role through EKS Pod Identity whose policy holds only the
   actions its declared access needs. Each stage binds the Resource's release to the environment
   with the environment's size, before the components that use it; agents bind dev and staging,
   production is bound after G6 on behalf of the approver. The pipeline never deletes a resource,
   and a change that would replace stored data (a new key, a queue turned FIFO) is a finding:
   a person does that outside the pipeline.
"""

from __future__ import annotations

import re

import yaml

from .agents.parse import section

ACCESS_VERBS = {"reads": "read", "writes": "write", "deletes": "delete", "publishes": "write"}
# Roles that run in the platform and may hold an AWS identity.
AWS_ROLES = ("provider", "event-worker", "agent", "mcp-server", "bff")
IDENTITY_TYPE = "workload-identity"
# Parameters whose change replaces the resource (and its data).
REPLACING = {"dynamodb-table": ("partition_key", "sort_key"), "sqs-queue": ("fifo",), "sns-topic": ("fifo",),
             "postgres-crossplane": ("database",)}
_NAME = re.compile(r"^[a-z][a-z0-9-]{2,39}$")
_ENTRY = re.compile(r"^([A-Za-z0-9_-]+)\s*:\s*([a-z0-9-]+)\s*(?:;(.*))?$")
_SERVICES = re.compile(r"\b(DynamoDB|Kinesis|SQS|SNS|S3|ElastiCache|RDS|Aurora|Lambda|Step Functions|EventBridge)\b")


class CatalogError(ValueError):
    pass


def load_catalog(text: str) -> dict:
    """The platform catalog, checked: every type has a purpose, data classes, sizes with costs."""
    raw = yaml.safe_load(text or "") or {}
    types = raw.get("types") or {}
    if not isinstance(types, dict):
        raise CatalogError("aws-resources.yaml: types must be a mapping")
    for name, t in types.items():
        for key in ("service", "use_for", "data_classes", "sizes"):
            if not t.get(key):
                raise CatalogError(f"aws-resources.yaml: {name} has no {key}")
        for size, s in (t.get("sizes") or {}).items():
            if not isinstance(s, dict) or not isinstance(s.get("monthly_usd"), (int, float)):
                raise CatalogError(f"aws-resources.yaml: {name} size {size} has no monthly_usd")
    return {"types": types, "regions": raw.get("regions") or {}, "defaults": raw.get("defaults") or {},
            "environment_sizes": raw.get("environment_sizes") or {}, "version": raw.get("version", 1)}


def check_aws(aws: dict | None, data_class: str, pipeline: list[str], catalog: dict) -> list[str]:
    """product.yaml runtime.aws: an account per environment, in a region allowed for the data class."""
    if not aws:
        return []
    out = []
    allowed = (catalog.get("regions") or {}).get(data_class)
    region = str(aws.get("region") or "")
    if not region:
        out.append("runtime.aws.region is required")
    elif allowed is not None and region not in allowed:
        out.append(f"runtime.aws.region {region} is not allowed for {data_class} data; allowed: {allowed}")
    accounts = aws.get("accounts") or {}
    for env in pipeline:
        if not re.fullmatch(r"\d{12}", str(accounts.get(env) or "")):
            out.append(f"runtime.aws.accounts.{env} must be the environment's 12-digit AWS account id")
    if len({str(accounts.get(e)) for e in pipeline}) < len(pipeline):
        out.append("runtime.aws.accounts: each environment needs its own AWS account")
    return out


# --------------------------------------------------------------------------- the design


def parse_resources(hld: str) -> list[dict]:
    """The HLD's Resources section, one entry per bullet (continuation lines carry the access)."""
    body = section(hld, "Resources")
    out: list[dict] = []
    for raw in body.splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue
        if line.lstrip().startswith("- "):
            m = _ENTRY.match(line.lstrip()[2:].strip())
            if not m:
                out.append({"name": "", "type": "", "line": line.strip(), "params": {}, "sizes": {}, "access": {}})
                continue
            entry = {"name": m.group(1), "type": m.group(2), "line": line.strip(), "params": {}, "sizes": {},
                     "access": {}}
            for part in (m.group(3) or "").split(";"):
                part = part.strip()
                if part.startswith("size "):
                    for pair in part[5:].split(","):
                        if "=" in pair:
                            env, size = (x.strip() for x in pair.split("=", 1))
                            entry["sizes"][env] = size
                elif "=" in part:
                    for pair in part.split(","):
                        if "=" in pair:
                            k, v = (x.strip() for x in pair.split("=", 1))
                            entry["params"][k] = _value(v)
            out.append(entry)
        elif out:
            _access_line(out[-1], line.strip())
    return out


def _value(v: str):
    if v.lower() in ("true", "false"):
        return v.lower() == "true"
    if re.fullmatch(r"-?\d+", v):
        return int(v)
    return v


def _access_line(entry: dict, line: str) -> None:
    """'reads, writes: a, b; reads: c' -> {a: {read, write}, b: {read, write}, c: {read}}"""
    for clause in line.split(";"):
        if ":" not in clause:
            continue
        verbs, comps = clause.split(":", 1)
        access = {ACCESS_VERBS.get(v.strip().lower(), f"?{v.strip()}") for v in verbs.split(",") if v.strip()}
        for comp in comps.split(","):
            comp = comp.strip().split("/", 1)[-1]
            if comp:
                entry["access"].setdefault(comp, set()).update(access)


def check(hld: str, repos: list[dict], ctx: dict) -> list[str]:
    """Findings for the design's Resources (VX-19)."""
    catalog = ctx.get("aws_catalog") or {}
    entries = parse_resources(hld)
    rest = hld.replace(section(hld, "Resources"), "")
    if not entries:
        named = sorted(set(_SERVICES.findall(section(rest, "ADRs") + section(rest, "Contracts"))))
        if named:
            return [f"The design names {', '.join(named)} but has no Resources section: declare each AWS "
                    "service with its type and who reads or writes it (VX-19)"]
        return []
    out: list[str] = []
    types = catalog.get("types") or {}
    if not types:
        return ["The design declares AWS resources, but the platform has no AWS catalog (aws-resources.yaml)"]
    if not ctx.get("aws"):
        out.append("The design needs AWS resources, but the product has no AWS accounts (product.yaml runtime.aws): "
                   "the platform team sets them up first (VX-19)")
    roles = {r["name"].split("/", 1)[-1]: r["role"] for r in
             [*repos, *(ctx.get("repos") or []), *(ctx.get("dependency_repos") or [])]}
    adrs = section(hld, "ADRs").lower()
    defaults = set((catalog.get("defaults") or {}).values())
    pipeline = list(ctx.get("pipeline") or ["dev", "staging", "production"])
    existing = {r["name"]: r for r in ctx.get("aws_existing") or []}
    seen: set[str] = set()
    for e in entries:
        if not e["name"]:
            out.append(f"Can't read resource line '{e['line']}': write '- <name>: <type>; key=value' with an "
                       "indented 'reads: <components>' or 'writes: <components>' line under it")
            continue
        name, kind = e["name"], e["type"]
        if not _NAME.match(name):
            out.append(f"Resource name {name}: lowercase letters, digits and hyphens, 3 to 40 characters")
        if name in seen:
            out.append(f"Resource {name} is declared twice")
        seen.add(name)
        t = types.get(kind)
        if t is None or t.get("internal"):
            out.append(f"{name}: {kind} isn't in the platform catalog; use one of "
                       f"{sorted(k for k, v in types.items() if not v.get('internal'))}, or ask the platform team "
                       "to add a type (VX-19)")
            continue
        if ctx.get("data_class") and ctx["data_class"] not in t["data_classes"]:
            out.append(f"{name}: {kind} is not approved for {ctx['data_class']} data (VX-19)")
        params = t.get("parameters") or {}
        for p, spec in params.items():
            if spec.get("required") and p not in e["params"]:
                out.append(f"{name}: {kind} needs {p}")
        for p, v in e["params"].items():
            spec = params.get(p)
            if spec is None:
                out.append(f"{name}: {kind} has no parameter {p}; it has {sorted(params) or 'none'}")
                continue
            out += [f"{name}: {m}" for m in _check_param(p, v, spec)]
        for env, size in e["sizes"].items():
            if env not in pipeline:
                out.append(f"{name}: size for {env}, which is not one of the product's environments {pipeline}")
            elif size not in t["sizes"]:
                out.append(f"{name}: {kind} has no size {size}; it has {sorted(t['sizes'])}")
        allowed = set((t.get("access") or {}).keys())
        if not e["access"]:
            out.append(f"{name}: no component reads or writes it; add 'reads: <component>' or 'writes: <component>'")
        for comp, access in sorted(e["access"].items()):
            if comp not in roles:
                out.append(f"{name}: {comp} is not a component of the design or the product")
            elif roles[comp] not in AWS_ROLES:
                out.append(f"{name}: {comp} is a {roles[comp]}, which runs outside the platform and holds no AWS "
                           "identity; reach the data through a service (VX-19)")
            bad = sorted(a for a in access if a.startswith("?"))
            if bad:
                out.append(f"{name}: '{bad[0][1:]}' is not an access; use reads, writes or deletes")
            if allowed:
                extra = sorted(a for a in access if not a.startswith("?") and a not in allowed)
                if extra:
                    out.append(f"{name}: {kind} offers {sorted(allowed)}, not {', '.join(extra)}")
        if kind not in defaults and name.lower() not in adrs and kind not in adrs:
            out.append(f"{name}: {kind} is not a platform default ({', '.join(sorted(defaults))}); add an ADR saying "
                       "why this service and not the default (VX-19)")
        prior = existing.get(name)
        if prior:
            if prior.get("type") and prior["type"] != kind:
                out.append(f"{name} already exists as a {prior['type']}: changing its type replaces it, which a "
                           "person does outside the pipeline; name a new resource instead (VX-19)")
            for p in REPLACING.get(kind, ()):
                was, now_ = (prior.get("parameters") or {}).get(p), e["params"].get(p)
                if was is not None and now_ is not None and was != now_:
                    out.append(f"{name}: changing {p} from {was} to {now_} replaces it and its data, which a person "
                               "does outside the pipeline; design around it or name a new resource (VX-19)")
    return out


def _check_param(p: str, v, spec: dict) -> list[str]:
    kind = spec.get("type", "string")
    if kind == "integer":
        if not isinstance(v, int) or isinstance(v, bool):
            return [f"{p} must be a whole number"]
        if "min" in spec and v < spec["min"] or "max" in spec and v > spec["max"]:
            return [f"{p} must be between {spec.get('min')} and {spec.get('max')}"]
    if kind == "boolean" and not isinstance(v, bool):
        return [f"{p} must be true or false"]
    if kind == "string" and not isinstance(v, str):
        return [f"{p} must be text"]
    return []


# --------------------------------------------------------------------------- cost and plan


def size_for(entry: dict, env: str, catalog: dict) -> str:
    t = catalog["types"][entry["type"]]
    size = entry["sizes"].get(env) or (catalog.get("environment_sizes") or {}).get(env) or "small"
    return size if size in t["sizes"] else sorted(t["sizes"])[0]


def summary(hld: str, catalog: dict, pipeline: list[str], aws: dict | None) -> dict | None:
    """What G4 approves: the services, each component's access, the cost per environment."""
    entries = [e for e in parse_resources(hld) if e["type"] in (catalog.get("types") or {})]
    if not entries:
        return None
    rows, totals = [], {env: 0.0 for env in pipeline}
    for e in entries:
        t = catalog["types"][e["type"]]
        sizes = {env: size_for(e, env, catalog) for env in pipeline}
        monthly = {env: float(t["sizes"][sizes[env]]["monthly_usd"]) for env in pipeline}
        for env in pipeline:
            totals[env] += monthly[env]
        rows.append({"name": e["name"], "type": e["type"], "service": t["service"], "stateful": bool(t.get("stateful")),
                     "access": {c: sorted(a) for c, a in sorted(e["access"].items())},
                     "parameters": dict(e["params"]), "sizes": sizes, "monthly_usd": monthly})
    return {"resources": rows, "monthly_usd": {k: round(v, 2) for k, v in totals.items()},
            "region": (aws or {}).get("region", ""), "accounts": dict((aws or {}).get("accounts") or {}),
            "note": "Planning figures from the platform catalog, not quotes."}


def plan(hld: str, catalog: dict, owner_target, *, product_id: str, feature_id: str, data_class: str) -> list[dict]:
    """The OpenChoreo Resources to create: one per entry, plus a workload identity per component
    that needs IAM access. `owner_target(component)` -> (project, namespace) of the component."""
    out: list[dict] = []
    identities: dict[str, dict] = {}
    for e in parse_resources(hld):
        t = (catalog.get("types") or {}).get(e["type"])
        if t is None or not e["access"]:
            continue
        actions = t.get("access") or {}
        consumers = []
        for comp, access in sorted(e["access"].items()):
            acts = sorted({a for verb in sorted(access) for a in actions.get(verb, [])})
            consumers.append({"component": comp, "access": sorted(access), "actions": acts})
            if acts:
                identities.setdefault(comp, {"component": comp})
        owner = next((c for c, a in sorted(e["access"].items()) if "write" in a), sorted(e["access"])[0])
        project, namespace = owner_target(owner)
        out.append({"name": e["name"], "type": e["type"], "project": project, "namespace": namespace,
                    "owner": owner, "stateful": bool(t.get("stateful")),
                    "parameters": {**e["params"], "consumers": consumers,
                                   "tags": {"vrixel:product": product_id, "vrixel:feature": feature_id,
                                            "vrixel:data-class": data_class}},
                    "sizes": {env: e["sizes"][env] for env in e["sizes"]},
                    "outputs": dict(t.get("outputs") or {})})
    ids = []
    for comp in sorted(identities):
        project, namespace = owner_target(comp)
        ids.append({"name": f"{comp}-identity", "type": IDENTITY_TYPE, "project": project,
                       "namespace": namespace, "owner": comp, "stateful": False,
                       "parameters": {"component": comp, "tags": {"vrixel:product": product_id,
                                                                  "vrixel:data-class": data_class}},
                    "sizes": {}, "outputs": {}})
    return ids + out


def env_name(resource: str, output: str) -> str:
    return re.sub(r"[^A-Z0-9]+", "_", f"{resource}_{output}".upper()).strip("_")


def workload_snippet(hld: str, catalog: dict, component: str) -> str:
    """What the component's workload.yaml gets under dependencies.resources."""
    lines = []
    for e in parse_resources(hld):
        t = (catalog.get("types") or {}).get(e["type"])
        if t is None or component not in e["access"]:
            continue
        lines += [f"    - ref: {e['name']}", "      envBindings:"]
        lines += [f"        {out}: {env_name(e['name'], out)}" for out in (t.get("outputs") or {})]
    return ("dependencies:\n  resources:\n" + "\n".join(lines)) if lines else ""


def instruction(ctx: dict, task: dict) -> str:
    """The coder's VX-19 rules for this task's component."""
    hld, catalog = ctx.get("hld_text") or "", ctx.get("aws_catalog") or {}
    comp = task.get("repo", "").split("/", 1)[-1]
    snippet = workload_snippet(hld, catalog, comp) if hld and catalog else ""
    if not snippet:
        return ""
    used = [e for e in parse_resources(hld) if comp in e["access"]]
    names = ", ".join(f"{e['name']} ({e['type']}, {'/'.join(sorted(e['access'][comp]))})" for e in used)
    emulators = sorted({catalog["types"][e["type"]].get("emulator", "") for e in used if e["type"] in catalog["types"]})
    return (f" This component uses {names} (VX-19). Add to workload.yaml:\n{snippet}\n"
            " Read each name from its environment variable, reach the service behind a port in the code, and use"
            " the AWS SDK's default credentials (the pod's identity): never keys or fixed names. Test against "
            + ", ".join(e for e in emulators if e) + ". Use only the access listed.")


def catalog_guide(catalog: dict, data_class: str) -> str:
    """The architect's view of the catalog."""
    types = {k: v for k, v in (catalog.get("types") or {}).items() if not v.get("internal")}
    if not types:
        return ""
    lines = ["AWS services the platform offers (VX-19). Use one only when the platform defaults "
             f"({', '.join(sorted(set((catalog.get('defaults') or {}).values())))}) can't serve, say why in an ADR, and "
             "declare it in a '## Resources' section: '- <name>: <type>; key=value, ...[; size <env>=<size>]' with an "
             "indented line such as 'writes: <component>; reads: <component>'."]
    for name, t in sorted(types.items()):
        if data_class and data_class not in t["data_classes"]:
            continue
        params = ", ".join(f"{p}{'*' if s.get('required') else ''}" for p, s in (t.get("parameters") or {}).items())
        lines.append(f"- {name} ({t['service']}): for {t['use_for']} Not for: {t.get('not_for', '')} "
                     f"Access: {', '.join(t.get('access') or {}) or 'credentials'}. Parameters: {params or 'none'}.")
    return "\n".join(lines)

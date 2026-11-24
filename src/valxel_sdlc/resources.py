"""Managed cloud services, from the design to every environment (constitution VX-19).

The platform catalog (`catalog.yaml` in the platform governance repo; `aws-resources.yaml` before
version 2) lists the services a product may use, by neutral type (key-value-table, queue...): what
each is for, which data classes may use it, and per cloud (ADR 0010) the service behind it, the
access behind "reads" and "writes", sizes with planning costs and the emulator for tests. The tool
reads only the installation's cloud's block of each type (`load_catalog`), so everything below
sees one cloud's catalog. The version 1 AWS names (dynamodb-table...) read as aliases of their
type. Each type is an OpenChoreo ClusterResourceType backed by a Crossplane Composition per cloud
(valxel-platform/platform-resources).

1. Design (architect, checked here, approved at G4). The HLD's Resources section names each
   service a feature needs and who may use it:

       ## Resources
       - field-readings: event-stream; retention_hours=168
         writes: farm-sensor-api; reads: farm-field-worker
       - field-state: key-value-table; partition_key=field_id; size production=large
         reads, writes: farm-field-worker

   A service other than the platform defaults needs an ADR naming it. G4 shows the approver the
   services, each component's access and the monthly cost per environment.

2. Build (coders). Each component that uses a resource declares it in workload.yaml
   (`dependencies.resources`, outputs bound to environment variables) and reaches it behind a port
   with the emulator in tests.

   Kafka topics are never listed here: the catalog's `from_contracts` type (kafka-topic) is
   written from the HLD's data contracts (datasets.py), one per topic, the contract's owner
   writing and its consumers reading, with a kafka-user per component they name.

3. Environments (pipeline). OpenChoreo creates a Resource per entry, plus a workload identity per
   component that uses one: an IAM role through EKS Pod Identity, or a Google service account
   through GKE Workload Identity, granted only what its declared access needs. Each stage binds the Resource's release to the environment
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
# Roles that run in the platform and may hold a cloud identity.
AWS_ROLES = ("provider", "event-worker", "agent", "mcp-server", "knowledge", "bff", "reporting", "job", "document-intake")
IDENTITY_TYPE = "workload-identity"
KAFKA_USER_TYPE = "kafka-user"
# A code-sandbox component's sandboxes (platform-resources: code-runner): not a catalog type, never
# picked by hand; one per code-sandbox component, its warm pool bound to SANDBOX_SANDBOX_TEMPLATE.
CODE_RUNNER_TYPE = "code-runner"
RETENTION_DAYS = {"d": 1, "m": 30, "y": 365}
# Parameters whose change replaces the resource (and its data): version 1 catalogs' types. Version 2
# says it per type (replaced_by_changing).
REPLACING = {"dynamodb-table": ("partition_key", "sort_key"), "sqs-queue": ("fifo",), "sns-topic": ("fifo",),
             "postgres-crossplane": ("database",)}
CLOUD_NAMES = {"aws": "AWS", "gcp": "Google Cloud", "azure": "Azure"}
_NAME = re.compile(r"^[a-z][a-z0-9-]{2,39}$")
_ENTRY = re.compile(r"^([A-Za-z0-9_-]+)\s*:\s*([a-z0-9-]+)\s*(?:;(.*))?$")
_SERVICES = re.compile(r"\b(DynamoDB|Kinesis|SQS|SNS|S3|ElastiCache|RDS|Aurora|Lambda|Step Functions|EventBridge|"
                       r"Firestore|Pub/Sub|Cloud Storage|Bigtable|Spanner|Memorystore|Cloud SQL|Cloud Run)\b")


class CatalogError(ValueError):
    pass


def load_catalog(text: str, cloud: str | None = None) -> dict:
    """The platform catalog as one cloud sees it, checked: every type has a purpose, data classes,
    and sizes with costs. Version 2 (types with `clouds`): each type that has a block for `cloud`
    (the installation's, by default), with that block's service, access, sizes, alarms and
    emulator over the type's own fields; a block's parameters narrow the type's. Version 1 (AWS
    only, aws-resources.yaml): as written."""
    raw = yaml.safe_load(text or "") or {}
    types = raw.get("types") or {}
    if not isinstance(types, dict):
        raise CatalogError("catalog: types must be a mapping")
    v2 = int(raw.get("version") or 1) >= 2 or any(isinstance(t, dict) and "clouds" in t for t in types.values())
    cloud = cloud or _installation_cloud()
    aliases: dict[str, str] = {}
    unavailable: list[str] = []
    if v2:
        view, aliases, unavailable = _cloud_view(types, cloud)
        regions = (raw.get("regions") or {}).get(cloud) or {}
    else:
        if cloud != "aws":
            raise CatalogError(f"aws-resources.yaml is the AWS catalog; this installation runs on {cloud}: "
                               "the platform needs a version 2 catalog (catalog.yaml) with a block for it")
        view, regions = dict(types), raw.get("regions") or {}
    for name, t in view.items():
        for key in ("service", "use_for", "data_classes", "sizes"):
            if not t.get(key):
                raise CatalogError(f"catalog: {name} has no {key}" + (f" on {cloud}" if v2 else ""))
        for size, s in (t.get("sizes") or {}).items():
            if not isinstance(s, dict) or not isinstance(s.get("monthly_usd"), (int, float)):
                raise CatalogError(f"catalog: {name} size {size} has no monthly_usd")
    return {"types": view, "regions": regions, "defaults": raw.get("defaults") or {},
            "environment_sizes": raw.get("environment_sizes") or {}, "version": raw.get("version", 1),
            "cloud": cloud, "aliases": aliases, "unavailable": sorted(unavailable)}


def _installation_cloud() -> str:
    from .cloud import UnknownCloud, provider

    try:
        return provider()
    except UnknownCloud:
        return "aws"  # strict mode says what's wrong with VALXEL_CLOUD


def _cloud_view(types: dict, cloud: str) -> tuple[dict[str, dict], dict[str, str], list[str]]:
    """(each type with a block for `cloud`, merged; alias -> type; the types it has no block for)."""
    view: dict[str, dict] = {}
    aliases: dict[str, str] = {}
    unavailable: list[str] = []
    for name, t in types.items():
        aliases.update({str(a): name for a in t.get("aliases") or []})
        block = (t.get("clouds") or {}).get(cloud)
        if not block:
            unavailable.append(name)
            continue
        params = {p: dict(s or {}) for p, s in (t.get("parameters") or {}).items()}
        for p, narrower in (block.get("parameters") or {}).items():
            params.setdefault(p, {}).update(narrower or {})
        view[name] = {**{k: v for k, v in t.items() if k not in ("clouds", "aliases")},
                      **{k: v for k, v in block.items() if k != "parameters"}, "parameters": params}
    return view, aliases, unavailable


def read_catalog(read, path: str) -> str:
    """The catalog's text: `read(path)` (None when the file isn't there), falling back between
    catalog.yaml and the version 1 aws-resources.yaml in the same folder, so a governance repo and
    the tool's settings can each move to the new name in their own time."""
    text = read(path)
    if text is None:
        folder, _, name = path.rpartition("/")
        other = {"catalog.yaml": "aws-resources.yaml", "aws-resources.yaml": "catalog.yaml"}.get(name)
        if other:
            text = read(f"{folder}/{other}" if folder else other)
    return text or ""


def canonical(kind: str, catalog: dict) -> str:
    """A type's name, with a version 1 alias (dynamodb-table) read as its type (key-value-table)."""
    return (catalog.get("aliases") or {}).get(kind, kind)


def replacing(kind: str, catalog: dict) -> tuple:
    t = (catalog.get("types") or {}).get(kind) or {}
    return tuple(t.get("replaced_by_changing") or REPLACING.get(kind, ()))


def cloud_name(catalog: dict) -> str:
    return CLOUD_NAMES.get(catalog.get("cloud") or "aws", str(catalog.get("cloud")))


PROJECT_ID = re.compile(r"[a-z][a-z0-9-]{4,28}[a-z0-9]")
SUBSCRIPTION_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def check_cloud(cloud: dict | None, data_class: str, pipeline: list[str], catalog: dict) -> list[str]:
    """product.yaml runtime.cloud: an account (AWS), project (Google Cloud) or subscription (Azure)
    per environment, in a region the catalog allows for the data class."""
    if not cloud:
        return []
    out = []
    provider = str(cloud.get("provider") or "aws")
    field = "runtime.aws" if cloud.get("_legacy") else "runtime.cloud"
    allowed = (catalog.get("regions") or {}).get(data_class) if catalog.get("cloud", provider) == provider else None
    region = str(cloud.get("region") or "")
    if not region:
        out.append(f"{field}.region is required")
    elif allowed is not None and region not in allowed:
        out.append(f"{field}.region {region} is not allowed for {data_class} data; allowed: {allowed}")
    accounts = cloud.get("accounts") or {}
    for env in pipeline:
        value = str(accounts.get(env) or "")
        if provider == "gcp" and not PROJECT_ID.fullmatch(value):
            out.append(f"{field}.accounts.{env} must be the environment's Google Cloud project ID")
        elif provider == "aws" and not re.fullmatch(r"\d{12}", value):
            out.append(f"{field}.accounts.{env} must be the environment's 12-digit AWS account id")
        elif provider == "azure" and not SUBSCRIPTION_ID.fullmatch(value):
            out.append(f"{field}.accounts.{env} must be the environment's Azure subscription ID (a lowercase GUID)")
    if len({str(accounts.get(e)) for e in pipeline}) < len(pipeline):
        noun = {"gcp": "project", "azure": "subscription"}.get(provider, "account")
        out.append(f"{field}.accounts: each environment needs its own {CLOUD_NAMES.get(provider, provider)} {noun}")
    return out


def check_aws(aws: dict | None, data_class: str, pipeline: list[str], catalog: dict) -> list[str]:
    """product.yaml runtime.aws (before runtime.cloud)."""
    return check_cloud({"provider": "aws", "_legacy": True, **aws} if aws else None, data_class, pipeline, catalog)


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


def resolved(hld: str, catalog: dict) -> list[dict]:
    """parse_resources, each type read through the catalog's aliases ('named' keeps what was written)."""
    out = parse_resources(hld)
    for e in out:
        e["named"], e["type"] = e["type"], canonical(e["type"], catalog)
    return out


def check(hld: str, repos: list[dict], ctx: dict) -> list[str]:
    """Findings for the design's Resources (VX-19)."""
    catalog = ctx.get("aws_catalog") or {}
    entries = resolved(hld, catalog)
    cloud = cloud_name(catalog)
    rest = hld.replace(section(hld, "Resources"), "")
    if not entries:
        named = sorted(set(_SERVICES.findall(section(rest, "ADRs") + section(rest, "Contracts"))))
        if named:
            return [f"The design names {', '.join(named)} but has no Resources section: declare each cloud "
                    "service with its type and who reads or writes it (VX-19)"]
        return []
    out: list[str] = []
    types = catalog.get("types") or {}
    if not types:
        return ["The design declares cloud resources, but the platform has no catalog (catalog.yaml)"]
    if not (ctx.get("cloud") or ctx.get("aws")):
        out.append(f"The design needs {cloud} resources, but the product has no {cloud} accounts (product.yaml "
                   "runtime.cloud): the platform team sets them up first (VX-19)")
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
        if t is not None and t.get("from_contracts"):
            out.append(f"{name}: {kind} isn't listed under Resources; it comes from the data contracts under "
                       "'## Datasets', one per topic (VX-16)")
            continue
        if t is None and kind in (catalog.get("unavailable") or []):
            out.append(f"{name}: {kind} isn't offered on {cloud} yet; use one of "
                       f"{sorted(k for k, v in types.items() if _pickable(v))} (VX-19)")
            continue
        if t is None or t.get("internal"):
            out.append(f"{name}: {kind} isn't in the platform catalog; use one of "
                       f"{sorted(k for k, v in types.items() if _pickable(v))}, or ask the platform team "
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
                out.append(f"{name}: {comp} is a {roles[comp]}, which runs outside the platform and holds no cloud "
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
            if prior.get("type") and canonical(prior["type"], catalog) != kind:
                out.append(f"{name} already exists as a {prior['type']}: changing its type replaces it, which a "
                           "person does outside the pipeline; name a new resource instead (VX-19)")
            for p in replacing(kind, catalog):
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
    if kind == "list":
        items = [i for i in str(v).split("+")]
        if not all(items):
            return [f"{p}: separate items with +, each non-empty ({spec.get('help', '')})"]
        return []
    if kind == "string" and not isinstance(v, str):
        return [f"{p} must be text"]
    return []


# --------------------------------------------------------------------------- cost and plan


def size_for(entry: dict, env: str, catalog: dict) -> str:
    t = catalog["types"][entry["type"]]
    size = entry["sizes"].get(env) or (catalog.get("environment_sizes") or {}).get(env) or "small"
    return size if size in t["sizes"] else sorted(t["sizes"])[0]


def summary(hld: str, catalog: dict, pipeline: list[str], aws: dict | None) -> dict | None:
    """What G4 approves: the services on the installation's cloud, each component's access, the cost
    per environment. `aws`: the product's runtime.cloud (or runtime.aws)."""
    entries = [e for e in resolved(hld, catalog) if e["type"] in (catalog.get("types") or {})]
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
                     "parameters": dict(e["params"]), "sizes": sizes, "monthly_usd": monthly,
                     # The standard alarms it comes with: in production they open incidents.
                     "alarms": [{"name": n, "severity": a["severity"], "says": a["says"]}
                                for n, a in (t.get("alarms") or {}).items()]})
    return {"resources": rows, "monthly_usd": {k: round(v, 2) for k, v in totals.items()},
            "region": (aws or {}).get("region", ""), "accounts": dict((aws or {}).get("accounts") or {}),
            "cloud": catalog.get("cloud") or "aws", "cloud_name": cloud_name(catalog),
            "note": "Planning figures from the platform catalog, not quotes."}


def _pickable(t: dict) -> bool:
    """A type an architect may list under Resources (not internal, not written from contracts)."""
    return not t.get("internal") and not t.get("from_contracts")


def _retention_days(value) -> int:
    m = re.fullmatch(r"(\d+)([dmy])", str(value or ""))
    return min(int(m.group(1)) * RETENTION_DAYS[m.group(2)], 3650) if m else 7


def contract_entries(hld: str, catalog: dict) -> list[dict]:
    """Resources entries for the HLD's data contracts: one kafka-topic per topic, the owner writing
    and each consumer reading, in parse_resources' shape."""
    from .datasets import consumers, extensions, parse_hld, topic

    kind = next((k for k, t in (catalog.get("types") or {}).items() if t.get("from_contracts")), "")
    if not kind:
        return []
    out = []
    for c in parse_hld(hld)[0]:
        name = re.sub(r"[^a-z0-9]+", "-", topic(c).lower()).strip("-")[:40]
        owner = str(extensions(c).get("owner") or "").split("/", 1)[-1]
        if not name or not owner:
            continue
        access: dict[str, set] = {owner: {"write"}}
        for repo in consumers(c):
            access.setdefault(repo.split("/", 1)[-1], set()).add("read")
        ext = extensions(c)
        params = {"topic": topic(c), "compacted": bool(ext.get("compacted")), "dead_letter": True,
                  "retention_days": _retention_days(ext.get("retention"))}
        out.append({"name": name, "type": kind, "line": f"contract {c.get('id')}", "params": params,
                    "sizes": {}, "access": access})
    return out


def _entries(hld: str, catalog: dict) -> list[dict]:
    return resolved(hld, catalog) + contract_entries(hld, catalog)


def plan(hld: str, catalog: dict, owner_target, *, product_id: str, feature_id: str, data_class: str,
         code_sandboxes: list[str] | tuple = (), existing: dict[str, str] | None = None) -> list[dict]:
    """The OpenChoreo Resources to create: one per entry (and per data contract's topic), plus a
    workload identity per component that needs IAM access, a Kafka identity per component a
    contract names, and the platform's sandboxes (code-runner) for each code-sandbox component.
    `owner_target(component)` -> (project, namespace) of the component. `existing`: name -> type of
    the resources OpenChoreo already runs; one made under an alias keeps that type, so the
    rename never replaces it. Tags are written as the cloud spells them (labels on Google Cloud)."""
    from .cloud import tags_for

    cloud = catalog.get("cloud") or "aws"
    existing = existing or {}
    out: list[dict] = []
    identities: dict[str, dict] = {}
    kafka_users: set[str] = set()
    for e in _entries(hld, catalog):
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
        if t.get("from_contracts"):
            kafka_users |= set(e["access"])
        owner = next((c for c, a in sorted(e["access"].items()) if "write" in a), sorted(e["access"])[0])
        project, namespace = owner_target(owner)
        params = {p: (str(v).split("+") if ((t.get("parameters") or {}).get(p) or {}).get("type") == "list" else v)
                  for p, v in e["params"].items()}
        # An existing resource keeps its type; a new one is made as the cloud's ClusterResourceType for
        # the type, which is the type's own name unless another module provides it there.
        prior = existing.get(e["name"], "")
        kind = prior if prior and canonical(prior, catalog) == e["type"] else (t.get("resource_type") or e["type"])
        out.append({"name": e["name"], "type": kind, "project": project, "namespace": namespace,
                    "owner": owner, "stateful": bool(t.get("stateful")),
                    "parameters": {**params, "consumers": consumers,
                                   "tags": tags_for(cloud, {"valxel:product": product_id, "valxel:feature": feature_id,
                                                            "valxel:resource": e["name"],
                                                            "valxel:data-class": data_class})},
                    "sizes": {env: e["sizes"][env] for env in e["sizes"]},
                    "outputs": dict(t.get("outputs") or {})})
    ids = []
    for comp in sorted(identities):
        project, namespace = owner_target(comp)
        ids.append({"name": f"{comp}-identity", "type": IDENTITY_TYPE, "project": project,
                       "namespace": namespace, "owner": comp, "stateful": False,
                       "parameters": {"component": comp, "tags": tags_for(cloud, {"valxel:product": product_id,
                                                                                  "valxel:data-class": data_class})},
                    "sizes": {}, "outputs": {}})
    user_type = (catalog.get("types") or {}).get(KAFKA_USER_TYPE)
    for comp in sorted(kafka_users) if user_type else []:
        project, namespace = owner_target(comp)
        ids.append({"name": f"{comp}-kafka", "type": KAFKA_USER_TYPE, "project": project, "namespace": namespace,
                    "owner": comp, "stateful": False,
                    "parameters": {"component": comp, "tags": tags_for(cloud, {"valxel:product": product_id,
                                                                               "valxel:data-class": data_class})},
                    "sizes": {}, "outputs": dict(user_type.get("outputs") or {})})
    for comp in sorted(set(code_sandboxes)):
        project, namespace = owner_target(comp)
        ids.append({"name": f"{comp}-runner", "type": CODE_RUNNER_TYPE, "project": project, "namespace": namespace,
                    "owner": comp, "stateful": False,
                    "parameters": {"component": comp, "tags": tags_for(cloud, {"valxel:product": product_id,
                                                                               "valxel:data-class": data_class})},
                    "sizes": {"production": "medium"}, "outputs": {"pool": "the warm pool its runs claim from"}})
    return ids + out


def env_name(resource: str, output: str) -> str:
    return re.sub(r"[^A-Z0-9]+", "_", f"{resource}_{output}".upper()).strip("_")


def workload_snippet(hld: str, catalog: dict, component: str) -> str:
    """What the component's workload.yaml gets under dependencies.resources."""
    lines = []
    kafka = False
    for e in _entries(hld, catalog):
        t = (catalog.get("types") or {}).get(e["type"])
        if t is None or component not in e["access"]:
            continue
        kafka = kafka or bool(t.get("from_contracts"))
        lines += [f"    - ref: {e['name']}", "      envBindings:"]
        lines += [f"        {out}: {env_name(e['name'], out)}" for out in (t.get("outputs") or {})]
    user = (catalog.get("types") or {}).get(KAFKA_USER_TYPE)
    if kafka and user:
        lines += [f"    - ref: {component}-kafka", "      envBindings:"]
        lines += [f"        {out}: KAFKA_{out.upper()}" for out in (user.get("outputs") or {})]
    return ("dependencies:\n  resources:\n" + "\n".join(lines)) if lines else ""


def instruction(ctx: dict, task: dict) -> str:
    """The coder's VX-19 rules for this task's component."""
    hld, catalog = ctx.get("hld_text") or "", ctx.get("aws_catalog") or {}
    comp = task.get("repo", "").split("/", 1)[-1]
    snippet = workload_snippet(hld, catalog, comp) if hld and catalog else ""
    if not snippet:
        return ""
    every = [e for e in _entries(hld, catalog) if comp in e["access"]]
    kafka = [e for e in every if ((catalog.get("types") or {}).get(e["type"]) or {}).get("from_contracts")]
    used = [e for e in every if e not in kafka]
    topics = (" Its Kafka topics come from the data contracts: " + ", ".join(
        f"{e['params']['topic']} ({'/'.join(sorted(e['access'][comp]))})" for e in kafka)
        + ". Read the topic and bootstrap servers from their environment variables and sign in as KAFKA_AUTH"
          " says: iam (MSK) with the pod's identity through SASL AWS_MSK_IAM, scram (elsewhere) with SCRAM-SHA-512"
          " and the credentials in the Secret KAFKA_SECRET names. Name every consumer group starting with "
          f"'{comp}': Kafka refuses any other group, and any topic the contracts don't give it.") if kafka else ""
    if not used:
        return f" This component uses Kafka topics (VX-16). Add to workload.yaml:\n{snippet}\n" + topics.strip()
    names = ", ".join(f"{e['name']} ({e['type']}, {'/'.join(sorted(e['access'][comp]))})" for e in used)
    emulators = sorted({catalog["types"][e["type"]].get("emulator", "") for e in used if e["type"] in catalog["types"]})
    if (catalog.get("cloud") or "aws") == "gcp":
        how = (" Read each name from its environment variable, reach the service behind a port in the code, and"
               " use the Google Cloud client library's Application Default Credentials (the pod's identity through"
               " GKE Workload Identity): never keys or fixed names. Test against "
               + ", ".join(e for e in emulators if e) + ". Use only the access listed. Trace every Google Cloud"
               " call with OpenTelemetry (Java: the client"
               " library's own tracing, Firestore's FirestoreOpenTelemetryOptions or Pub/Sub's"
               " setEnableOpenTelemetryTracing, as the event worker template's StoreConfig does; Python:"
               " opentelemetry-instrumentation-grpc), so the calls show in OpenChoreo's traces.")
    elif (catalog.get("cloud") or "aws") == "azure":
        how = (" Read each name from its environment variable, reach the service behind a port in the code, and"
               " use the Azure SDK's DefaultAzureCredential (the pod's identity through AKS Workload Identity):"
               " never connection strings, keys or fixed names. Test against "
               + ", ".join(e for e in emulators if e) + ". Use only the access listed. Trace every Azure call with"
               " OpenTelemetry (Java: azure-core-tracing-opentelemetry; Python: azure-core-tracing-opentelemetry),"
               " so the calls show in OpenChoreo's traces.")
    else:
        how = (" Read each name from its environment variable, reach the service behind a port in the code, and use"
               " the AWS SDK's default credentials (the pod's identity): never keys or fixed names. Test against "
               + ", ".join(e for e in emulators if e) + ". Use only the access listed. Trace every AWS call with"
               " OpenTelemetry's AWS SDK instrumentation on the client (Java: opentelemetry-aws-sdk-2.2's"
               " AwsSdkTelemetry interceptor, as the event worker template's StoreConfig does; Python:"
               " opentelemetry-instrumentation-botocore), so the calls show in OpenChoreo's traces.")
    return f" This component uses {names} (VX-19). Add to workload.yaml:\n{snippet}\n" + how + topics


def catalog_guide(catalog: dict, data_class: str) -> str:
    """The architect's view of the catalog."""
    types = {k: v for k, v in (catalog.get("types") or {}).items() if _pickable(v)}
    if not types:
        return ""
    defaults = ", ".join(sorted(set((catalog.get("defaults") or {}).values())))
    lines = [f"{cloud_name(catalog)} services the platform offers (VX-19). Use one only when the platform defaults "
             f"({defaults}) can't serve, say why in an ADR, and "
             "declare it in a '## Resources' section: '- <name>: <type>; key=value, ...[; size <env>=<size>]' with an "
             "indented line such as 'writes: <component>; reads: <component>'."]
    for name, t in sorted(types.items()):
        if data_class and data_class not in t["data_classes"]:
            continue
        params = ", ".join(f"{p}{'*' if s.get('required') else ''}" for p, s in (t.get("parameters") or {}).items())
        lines.append(f"- {name} ({t['service']}): for {t['use_for']} Not for: {t.get('not_for', '')} "
                     f"Access: {', '.join(t.get('access') or {}) or 'credentials'}. Parameters: {params or 'none'}.")
    gone = [k for k in catalog.get("unavailable") or [] if k not in types]
    if gone:
        lines.append(f"Not offered on {cloud_name(catalog)} yet: {', '.join(gone)}.")
    return "\n".join(lines)

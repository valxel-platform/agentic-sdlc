"""Connectors to third parties (role `connector`, valxel-platform/template-integration).

A connector is the only component that talks to a vendor (a lab, a payer, a CRM, a payment
processor) for the product. What it may do is approved at G4 and then enforced three times:

  the HLD            '## Integrations' names, per connector, the vendor, the most sensitive data
                     that crosses, whether a business associate agreement is signed, the webhooks
                     the vendor calls, the hosts the connector calls and how it authenticates:
                     '- acme-emr/emr-labs: example-labs; data phi; baa yes; webhooks lab-results;
                     calls api.example-labs.com, auth.example-labs.com; auth oauth2-client-credentials;
                     pulls test-catalog:http-cursor, protocols:object-store:example-labs-drop'
                     `pulls` (optional) names the sources the connector polls, each with its adapter
                     (and, for object-store, the bucket). An http-cursor source is read through the
                     hosts under `calls`. Only the adapters the platform team has approved (ADAPTERS)
                     pass G4.
  integration.yaml   the repo's own file, which the coder writes from that line. The build fails
                     when the file on the branch allows more than the line (config_findings).
  the platform       on every deploy the tool reads integration.yaml at the deployed commit and
                     gives the component's binding the webhook paths (the only routes exposed) and
                     the hosts its egress policy allows (binding_configs).
"""

from __future__ import annotations

import ipaddress
import re

import yaml

ROLE = "connector"
PATH = "integration.yaml"
DATA_CLASSES = ("general", "payments", "phi")
AUTHS = ("oauth2-client-credentials", "api-key", "mtls", "none")
# Pull adapters in template-integration that the platform team has approved. A new one (Graph,
# Confluence, Drive) is added here only with the platform team's approval, after the template has it.
ADAPTERS = ("http-cursor", "object-store")
_BUCKET = re.compile(r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")
_LINE = re.compile(r"^- ([A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9._-]+)\s*:\s*(.+?)\s*$", re.M)
_NAME = re.compile(r"^[a-z][a-z0-9-]{0,62}$")
_HOST = re.compile(r"^(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z][a-z0-9-]{0,62}$")


def _list(value: str) -> list[str]:
    v = value.strip().lower()
    return [] if v in ("", "none", "-") else [x.strip() for x in v.split(",") if x.strip()]


def check_host(host: str) -> str:
    """'' for a host name the connector may call, else what's wrong with it."""
    if "://" in host or "/" in host or ":" in host:
        return f"'{host}': a host name only, no scheme, port or path"
    if "*" in host:
        return f"'{host}': no wildcards: name each host"
    try:
        ipaddress.ip_address(host)
        return f"'{host}': name the host, not an IP address"
    except ValueError:
        pass
    if not _HOST.match(host):
        return f"'{host}' isn't a host name"
    if host.endswith((".local", ".internal", ".svc", ".cluster.local", "localhost")):
        return f"'{host}' is inside the platform: a connector calls the vendor; other components are reached by events"
    return ""


def _pulls(value: str, problems: list[str]) -> list[dict]:
    out, seen = [], set()
    for item in _list(value):
        name, _, rest = item.partition(":")
        adapter, _, bucket = rest.partition(":")
        name, adapter, bucket = name.strip(), adapter.strip(), bucket.strip()
        if not _NAME.match(name) or name in seen:
            problems.append(f"pull '{item}': name each source once (lowercase, dashes), as <name>:<adapter>")
        seen.add(name)
        if adapter not in ADAPTERS:
            problems.append(f"pull {name}: adapter '{adapter or '?'}' isn't one the platform team has approved "
                            f"({', '.join(ADAPTERS)}); a new adapter is theirs to approve")
        elif adapter == "object-store" and not _BUCKET.match(bucket):
            problems.append(f"pull {name}: name the bucket it reads, as {name}:object-store:<bucket>")
        elif adapter == "http-cursor" and bucket:
            problems.append(f"pull {name}: an http-cursor source names no bucket; its hosts are under 'calls'")
        out.append({"name": name, "adapter": adapter, "bucket": bucket})
    return out


def parse_integrations(hld: str) -> list[dict]:
    """'## Integrations' lines: repo -> vendor, data class, baa, webhooks, hosts, auth (and problems)."""
    from .agents.parse import section

    out = []
    for repo, rest in _LINE.findall(section(hld, "Integrations")):
        parts = [p.strip() for p in rest.split(";")]
        vendor = parts[0].lower() if parts else ""
        fields: dict[str, str] = {}
        for p in parts[1:]:
            key, _, value = p.partition(" ")
            fields[key.lower()] = value.strip()
        line = {"repo": repo, "vendor": vendor, "data": fields.get("data", "").lower(),
                "baa": fields.get("baa", "").lower() in ("yes", "true"), "webhooks": _list(fields.get("webhooks", "")),
                "hosts": _list(fields.get("calls", "")), "auth": fields.get("auth", "").lower(), "problems": []}
        p = line["problems"]
        line["pulls"] = _pulls(fields.get("pulls", ""), p)
        if any(x["adapter"] == "http-cursor" for x in line["pulls"]) and not line["hosts"]:
            p.append("an http-cursor pull is read through the hosts it calls: name them under 'calls'")
        if not _NAME.match(vendor):
            p.append("name the vendor first (lowercase, dashes)")
        if line["data"] not in DATA_CLASSES:
            p.append(f"'data <{'|'.join(DATA_CLASSES)}>': the most sensitive data that crosses")
        if "baa" not in fields:
            p.append("say 'baa yes' or 'baa no'")
        elif line["data"] == "phi" and not line["baa"]:
            p.append("PHI crosses to this vendor: it needs a signed business associate agreement (baa yes)")
        p += [f"webhook '{w}' isn't a name (lowercase, dashes)" for w in line["webhooks"] if not _NAME.match(w)]
        p += [problem for h in line["hosts"] if (problem := check_host(h))]
        if line["auth"] not in AUTHS:
            p.append(f"'auth <{'|'.join(AUTHS)}>': how the connector proves who it is to the vendor")
        elif line["auth"] == "none" and line["data"] in ("phi", "payments"):
            p.append(f"auth none can't carry {line['data']} data")
        if line["auth"] not in ("", "none") and not line["hosts"]:
            p.append(f"auth {line['auth']} but 'calls none': name the hosts it calls, or auth none")
        if not line["webhooks"] and not line["hosts"] and not line["pulls"]:
            p.append("nothing crosses: name its webhooks, the hosts it calls or the sources it pulls")
        out.append(line)
    return out


def design_findings(hld: str, repos: list[dict]) -> list[str]:
    """G4: every connector the design changes or adds says who's on the other side and what's allowed."""
    connectors = {r["name"] for r in repos if r.get("role") == ROLE}
    out, named = [], set()
    for line in parse_integrations(hld):
        named.add(line["repo"])
        if line["repo"] not in connectors:
            out.append(f"Integrations: {line['repo']} isn't a connector repo in this design (role {ROLE}): "
                       "only connectors talk to third parties")
        out += [f"Integrations: {line['repo']}: {p}" for p in line["problems"]]
    for repo in sorted(connectors - named):
        out.append(f"{repo} is a connector: say what crosses under '## Integrations' ('- {repo}: <vendor>; data "
                   "<general|payments|phi>; baa <yes|no>; webhooks <name, ...|none>; calls <host, ...|none>; "
                   "auth <oauth2-client-credentials|api-key|mtls|none>'; optionally "
                   "'pulls <name>:<adapter>[:<bucket>], ...')")
    return out


def endpoint_findings(hld: str, endpoints: dict[str, dict]) -> list[str]:
    """VX-17 for connectors: external exactly when the vendor calls its webhooks."""
    out = []
    for line in parse_integrations(hld):
        e = endpoints.get(line["repo"])
        if e is None:
            continue
        external = "external" in e["visibility"]
        if line["webhooks"] and not external:
            out.append(f"{line['repo']} receives webhooks ({', '.join(line['webhooks'])}): make it external in "
                       "Endpoints; only its /webhooks/<name> routes are exposed (VX-17)")
        if external and not line["webhooks"]:
            out.append(f"{line['repo']} is external but receives no webhooks: make it none (VX-17)")
    return out


def coder_instruction(hld: str, repo: str) -> str:
    """What the coder writes into integration.yaml for this repo, from the approved HLD."""
    line = next((x for x in parse_integrations(hld) if x["repo"] == repo and not x["problems"]), None)
    if line is None:
        return ""
    hooks = ", ".join(f"{w} at /webhooks/{w}" for w in line["webhooks"]) or "none"
    hosts = ", ".join(line["hosts"]) or "none"
    return (f" integration.yaml follows the HLD's Integrations line: vendor {line['vendor']}, data_class "
            f"{line['data']}, baa {str(line['baa']).lower()}; webhooks {hooks}, each signed (hmac-sha256) with a "
            f"mounted secret; outbound allowed_hosts exactly {hosts} with auth {line['auth']}. Secret names only, "
            "never values. Map the vendor's shapes in src/integration/mapping.py to and from the datasets' "
            "contracts: ids and codes, no more of a record than the contract allows. No other component calls "
            "this vendor (VX-17)." + _pull_instruction(line))


def _pull_instruction(line: dict) -> str:
    if not line.get("pulls"):
        return " No pull: section."
    named = "; ".join(f"{x['name']} ({x['adapter']}" + (f", bucket {x['bucket']}" if x["bucket"] else "") + ")"
                      for x in line["pulls"])
    return (f" pull: exactly these sources: {named}. One @pull handler each in mapping.py, returning Emit for "
            "a record and Delete for one the source says is gone; set deletions (tombstone, or full-listing for "
            "an object store) and backfill_from, and permissions_field when the dataset carries who may see a "
            "record. Each dataset a source feeds needs its contract under contracts/.")


def pull_sources(hld: str) -> dict[str, list[str]]:
    """repo -> the pull sources its approved Integrations line names (for staging reconciliation)."""
    return {x["repo"]: [p["name"] for p in x["pulls"]] for x in parse_integrations(hld)
            if not x["problems"] and x["pulls"]}


def _config(text: str) -> dict | None:
    try:
        raw = yaml.safe_load(text or "")
    except yaml.YAMLError:
        return None
    return raw if isinstance(raw, dict) else None


def config_findings(text: str, line: dict) -> list[str]:
    """integration.yaml (on a branch) against its approved HLD line: anything more is a finding."""
    raw = _config(text)
    if raw is None:
        return [f"{line['repo']}: {PATH} is missing or isn't a YAML mapping"]
    out = []
    if str(raw.get("vendor") or "").lower() != line["vendor"]:
        out.append(f"{line['repo']}: {PATH} names vendor {raw.get('vendor')!r}, the HLD {line['vendor']!r}")
    order = {c: i for i, c in enumerate(DATA_CLASSES)}
    have = str(raw.get("data_class") or "")
    if have not in order or order[have] < order.get(line["data"], 0):
        out.append(f"{line['repo']}: {PATH} says data_class {have!r}; the HLD approved {line['data']}")
    if line["data"] == "phi" and not raw.get("baa"):
        out.append(f"{line['repo']}: {PATH} must say baa: true for PHI")
    hooks = {str(w.get("name")) for w in ((raw.get("inbound") or {}).get("webhooks") or []) if isinstance(w, dict)}
    extra = sorted(hooks - set(line["webhooks"]))
    if extra:
        out.append(f"{line['repo']}: {PATH} adds webhooks the HLD doesn't approve: {', '.join(extra)}")
    outbound = raw.get("outbound") or {}
    hosts = {str(h).lower() for h in outbound.get("allowed_hosts") or []}
    extra = sorted(hosts - set(line["hosts"]))
    if extra:
        out.append(f"{line['repo']}: {PATH} allows hosts the HLD doesn't approve: {', '.join(extra)}")
    auth = str((outbound.get("auth") or {}).get("type") or ("none" if not outbound else ""))
    if outbound and auth != line["auth"]:
        out.append(f"{line['repo']}: {PATH} authenticates with {auth!r}; the HLD approved {line['auth']}")
    return out + _pull_findings(raw, line)


def _pull_findings(raw: dict, line: dict) -> list[str]:
    approved = {x["name"]: x for x in line.get("pulls") or []}
    pulls = raw.get("pull") or []
    if not isinstance(pulls, list):
        return [f"{line['repo']}: {PATH}: pull must be a list of sources"]
    out = []
    for p in pulls:
        if not isinstance(p, dict):
            continue
        name, adapter = str(p.get("name") or ""), str(p.get("adapter") or "")
        want = approved.get(name)
        if want is None:
            out.append(f"{line['repo']}: {PATH} pulls {name!r}, a source the HLD doesn't approve")
            continue
        if adapter != want["adapter"]:
            out.append(f"{line['repo']}: {PATH} reads {name} with {adapter!r}; the HLD approved {want['adapter']}")
        store = p.get("store")
        bucket = str(store.get("bucket") or "") if isinstance(store, dict) else ""
        if want["adapter"] == "object-store" and bucket != want["bucket"]:
            out.append(f"{line['repo']}: {PATH} reads {name} from bucket {bucket!r}; the HLD approved {want['bucket']}")
    return out


def build_findings(github, hld: str, results: dict) -> list[str]:
    """After the coder: each connector branch's integration.yaml stays within its approved line."""
    lines = {x["repo"]: x for x in parse_integrations(hld) if not x["problems"]}
    out = []
    for tid, r in sorted(results.items()):
        if r.get("role") != ROLE or not r.get("branch") or r.get("repo") not in lines:
            continue
        try:
            text = github.get_file(repo=r["repo"], path=PATH, ref=r["branch"])
        except Exception:  # noqa: BLE001 - unreadable counts as missing
            text = ""
        out += [f"{tid}: {f}" for f in config_findings(text or "", lines[r["repo"]])]
    return out


def binding_configs(text: str) -> dict:
    """What the platform enforces for a deployed connector: the webhook paths its route exposes (and
    nothing else), the hosts its egress policy allows, and its data class. {} when the file is
    missing or unusable: the component type then exposes nothing and allows no egress."""
    raw = _config(text)
    if raw is None:
        return {}
    paths = sorted({str(w.get("path")) for w in ((raw.get("inbound") or {}).get("webhooks") or [])
                    if isinstance(w, dict) and re.match(r"^/webhooks/[a-z0-9-]+$", str(w.get("path") or ""))})
    hosts = sorted({h for h in (str(x).lower() for x in ((raw.get("outbound") or {}).get("allowed_hosts") or []))
                    if not check_host(h)})
    data = str(raw.get("data_class") or "")
    return {"webhookPaths": paths, "egressHosts": hosts, **({"dataClass": data} if data in DATA_CLASSES else {})}


def read_configs(github, repo: str, ref: str) -> dict:
    """binding_configs from integration.yaml at the commit being deployed ('' = the default branch)."""
    try:
        text = github.get_file(repo=repo, path=PATH, ref=ref or "")
    except Exception:  # noqa: BLE001 - the deploy goes on with the type's closed defaults
        return {}
    return binding_configs(text or "")

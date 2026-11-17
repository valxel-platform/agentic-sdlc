"""Connectors to third parties (role `connector`, valxel-platform/template-integration).

A connector is the only component that talks to a vendor (a lab, a payer, a CRM, a payment
processor) for the product. What it may do is approved at G4 and then enforced three times:

  the HLD            '## Integrations' names, per connector, the vendor, the most sensitive data
                     that crosses, whether a business associate agreement is signed, the webhooks
                     the vendor calls, the hosts the connector calls and how it authenticates:
                     '- acme-emr/emr-labs: example-labs; data phi; baa yes; webhooks lab-results;
                     calls api.example-labs.com, auth.example-labs.com; auth oauth2-client-credentials'
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
        if not line["webhooks"] and not line["hosts"]:
            p.append("nothing crosses: name its webhooks or the hosts it calls")
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
                   "auth <oauth2-client-credentials|api-key|mtls|none>')")
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
            "this vendor (VX-17).")


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

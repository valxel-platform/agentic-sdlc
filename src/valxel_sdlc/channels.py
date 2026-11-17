"""Conversation channels (role `channel`, valxel-platform/template-channel).

People talk to the product over Slack, Teams, SMS or email through one channel repo. What it may
say where is approved at G4 and enforced on the branch and on every deploy:

  the HLD        '## Channels' names, per channel repo, each channel, what it carries (full text, or
                 a sign-in notice) and, for full text in a PHI product, that a BAA with the provider
                 is signed: '- acme-emr/emr-chat: slack full baa; sms notice; email notice'
  Endpoints      external: each provider calls its own /channels/<kind> route (no ADR needed)
  channel.yaml   the repo's own file, which the coder writes from the line. The build fails when it
                 has another channel, full text where the design said notice, or a BAA the design
                 didn't record (config_findings)
  the platform   on every deploy the tool reads channel.yaml at the deployed commit and gives the
                 binding its routes (/channels/<kind> only), the providers' hosts as its only
                 egress and the BFF as its only caller inside the product (binding_configs), on the
                 same component type as a connector.
"""

from __future__ import annotations

import re

import yaml

ROLE = "channel"
PATH = "channel.yaml"
KINDS = ("slack", "teams", "sms", "email")
# The same hosts template-channel's config.HOSTS allows: the platform's egress matches the code's.
HOSTS = {"slack": ("slack.com",), "teams": ("login.microsoftonline.com", "smba.trafficmanager.net"),
         "sms": ("api.twilio.com",), "email": ("api.mailgun.net",)}
_LINE = re.compile(r"^- ([A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9._-]+)\s*:\s*(.+?)\s*$", re.M)


def parse_channels(hld: str, data_class: str = "general") -> list[dict]:
    """'## Channels' lines: repo -> {kind: {content, baa}} (and problems)."""
    from .agents.parse import section

    out = []
    for repo, rest in _LINE.findall(section(hld, "Channels")):
        line: dict = {"repo": repo, "channels": {}, "problems": []}
        for part in (p.strip() for p in rest.split(";") if p.strip()):
            words = part.lower().split()
            kind = words[0]
            content = words[1] if len(words) > 1 else ""
            baa = "baa" in words[2:]
            if kind not in KINDS or kind in line["channels"]:
                line["problems"].append(f"'{part}': each channel once, one of {', '.join(KINDS)}")
                continue
            if content not in ("full", "notice"):
                line["problems"].append(f"{kind}: say full (the text) or notice (a sign-in notice)")
            elif content == "full" and data_class == "phi" and not baa:
                line["problems"].append(f"{kind}: PHI goes in full only with a BAA with the provider ('{kind} full "
                                        "baa'), or use notice")
            line["channels"][kind] = {"content": content, "baa": baa}
        if not line["channels"]:
            line["problems"].append("name its channels ('slack full; sms notice')")
        out.append(line)
    return out


def design_findings(hld: str, repos: list[dict], data_class: str = "general") -> list[str]:
    from .agents.parse import parse_endpoints

    mine = {r["name"] for r in repos if r.get("role") == ROLE}
    out, named = [], set()
    for line in parse_channels(hld, data_class):
        named.add(line["repo"])
        if line["repo"] not in mine:
            out.append(f"Channels: {line['repo']} isn't a channel repo in this design (role {ROLE})")
        out += [f"Channels: {line['repo']}: {p}" for p in line["problems"]]
    for repo in sorted(mine - named):
        out.append(f"{repo} is a channel: say which channels and what they carry under '## Channels' "
                   f"('- {repo}: slack full; sms notice; email notice')")
    for e in parse_endpoints(hld):
        if e["repo"] in mine and "external" not in e["visibility"]:
            out.append(f"{e['repo']} is a channel: providers call it, so it's external in Endpoints (only its "
                       "/channels/<kind> routes are exposed) (VX-17)")
    return out


def coder_instruction(hld: str, repo: str, data_class: str = "general") -> str:
    line = next((x for x in parse_channels(hld, data_class) if x["repo"] == repo and not x["problems"]), None)
    if line is None:
        return ""
    said = ", ".join(f"{k} (content {v['content']}, baa {str(v['baa']).lower()})" for k, v in line["channels"].items())
    return (f" channel.yaml's channels are exactly the HLD's Channels line: {said}. Secret names only. Unlinked "
            "people get only a sign-in link; STOP, START and HELP are honoured; replies go only to linked people "
            "who haven't opted out; addresses are never logged, published or audited (VX-15, VX-17).")


def _config(text: str) -> dict | None:
    try:
        raw = yaml.safe_load(text or "")
    except yaml.YAMLError:
        return None
    return raw if isinstance(raw, dict) else None


def config_findings(text: str, line: dict) -> list[str]:
    raw = _config(text)
    if raw is None:
        return [f"{line['repo']}: {PATH} is missing or isn't a YAML mapping"]
    out = []
    for c in raw.get("channels") or []:
        if not isinstance(c, dict):
            continue
        kind = str(c.get("kind") or "")
        approved = line["channels"].get(kind)
        if approved is None:
            out.append(f"{line['repo']}: {PATH} adds the {kind!r} channel, which the HLD doesn't approve")
            continue
        if c.get("content") == "full" and approved["content"] != "full":
            out.append(f"{line['repo']}: {PATH} sends full text on {kind}; the HLD says notice")
        if bool(c.get("baa")) and not approved["baa"]:
            out.append(f"{line['repo']}: {PATH} claims a BAA on {kind} that the HLD doesn't record")
    return out


def build_findings(github, hld: str, results: dict, data_class: str = "general") -> list[str]:
    lines = {x["repo"]: x for x in parse_channels(hld, data_class) if not x["problems"]}
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
    """Routes and egress for a deployed channel service: /channels/<kind> for its channels, and their
    providers' hosts. {} when the file is unusable: then nothing is exposed and nothing reachable."""
    raw = _config(text)
    if raw is None:
        return {}
    kinds = sorted({str(c.get("kind")) for c in raw.get("channels") or [] if isinstance(c, dict)} & set(KINDS))
    data = str(raw.get("data_class") or "")
    return {"webhookPaths": [f"/channels/{k}" for k in kinds],
            "egressHosts": sorted({h for k in kinds for h in HOSTS[k]}),
            "callerRoles": ["bff"],  # people confirm a channel link through the product's BFF
            **({"dataClass": data} if data in ("general", "payments", "phi") else {})}


def read_configs(github, repo: str, ref: str) -> dict:
    try:
        text = github.get_file(repo=repo, path=PATH, ref=ref or "")
    except Exception:  # noqa: BLE001 - the deploy goes on with the type's closed defaults
        return {}
    return binding_configs(text or "")

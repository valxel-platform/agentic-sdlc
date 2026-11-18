"""What a product's agent may do with its tools (role `agent`, valxel-platform/template-agent; T-19).

An agent repo calls the product's MCP servers. Which tools it may call, and which of those run
without a person approving each call, is approved at G4 and enforced on the branch and on every
deploy, like a connector's hosts:

  the HLD       '## Agent tools' names, per agent repo, the tools it reads with (run at once) and
                the tools it proposes (a person approves each call):
                '- acme-emr/emr-triage: read care__find_patient, care__list_open_tasks; propose care__create_task'
  agent.yaml    the repo's own file, which the coder writes from that line: `read_only` and
                `approval`, lists of `<server>__<tool>`. The build fails when the file lets the
                agent read without approval, or call at all, a tool the line doesn't (config_findings).
  the platform  on every deploy the tool reads agent.yaml at the deployed commit and sets the
                agent's AGENT_ALLOWED_TOOLS and AGENT_READ_ONLY_TOOLS through its binding
                (binding_configs). Without the file the agent may call nothing.

A read tool is named exactly: a wildcard on the read list would let every tool the server ever adds
run without a person, so only the approval list may use `<server>__*`. The template itself never
trusts a server's readOnlyHint, and refuses a tool the server marks as writing even if the read
list names it.
"""

from __future__ import annotations

import json
import re

import yaml

ROLE = "agent"
PATH = "agent.yaml"
_LINE = re.compile(r"^- ([A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9._-]+)\s*:\s*(.+?)\s*$", re.MULTILINE)
_TOOL = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}__[a-z0-9_][a-z0-9_-]{0,62}$")
_WILD = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}__\*$")


def _names(value: str) -> list[str]:
    v = value.strip()
    return [] if v.lower() in ("", "none", "-") else [x.strip() for x in v.split(",") if x.strip()]


def parse_agent_tools(hld: str) -> list[dict]:
    """'## Agent tools' lines: repo -> read and propose lists (and problems)."""
    from .agents.parse import section

    out = []
    for repo, rest in _LINE.findall(section(hld, "Agent tools")):
        line: dict = {"repo": repo, "read": [], "propose": [], "problems": []}
        for part in (p.strip() for p in rest.split(";") if p.strip()):
            word, _, value = part.partition(" ")
            if word not in ("read", "propose"):
                line["problems"].append(f"'{part}': say read <tools> or propose <tools>")
                continue
            for name in _names(value):
                if word == "read" and not _TOOL.match(name):
                    line["problems"].append(
                        f"read {name}: name each tool exactly, <server>__<tool> (a wildcard would let tools the "
                        "server adds later run without a person)" if "*" in name else
                        f"read {name}: not a tool name (<server>__<tool>)")
                elif word == "propose" and not (_TOOL.match(name) or _WILD.match(name)):
                    line["problems"].append(f"propose {name}: not a tool name (<server>__<tool> or <server>__*)")
                else:
                    line[word].append(name)
        both = set(line["read"]) & set(line["propose"])
        if both:
            line["problems"].append(f"{', '.join(sorted(both))}: either read or propose, not both")
        if not line["read"] and not line["propose"]:
            line["problems"].append("names no tools: say read <tools>; propose <tools> (either may be none)")
        out.append(line)
    return out


def design_findings(hld: str, repos: list[dict]) -> list[str]:
    mine = {r["name"] for r in repos if r.get("role") == ROLE}
    out, named = [], set()
    for line in parse_agent_tools(hld):
        named.add(line["repo"])
        if line["repo"] not in mine:
            out.append(f"Agent tools: {line['repo']} isn't an agent repo in this design (role {ROLE})")
        out += [f"Agent tools: {line['repo']}: {p}" for p in line["problems"]]
    for repo in sorted(mine - named):
        out.append(f"{repo} is an agent: say which tools it reads with and which it proposes under '## Agent tools' "
                   f"('- {repo}: read care__find_patient; propose care__create_task')")
    return out


def coder_instruction(hld: str, repo: str) -> str:
    line = next((x for x in parse_agent_tools(hld) if x["repo"] == repo and not x["problems"]), None)
    if line is None:
        return ""
    return (f" agent.yaml's tools are exactly the HLD's Agent tools line: read_only {line['read'] or []}, approval "
            f"{line['propose'] or []}. Nothing else in either list; a read tool is never a wildcard. The platform "
            "sets AGENT_ALLOWED_TOOLS and AGENT_READ_ONLY_TOOLS from this file on deploy: don't set them elsewhere.")


def _config(text: str) -> dict | None:
    try:
        raw = yaml.safe_load(text or "")
    except yaml.YAMLError:
        return None
    return raw if isinstance(raw, dict) else None


def _lists(raw: dict) -> tuple[list[str], list[str]]:
    tools = raw.get("tools") if isinstance(raw.get("tools"), dict) else {}
    read = [str(x) for x in tools.get("read_only") or [] if isinstance(x, str)]
    approval = [str(x) for x in tools.get("approval") or [] if isinstance(x, str)]
    return read, approval


def _covered(name: str, approved: list[str]) -> bool:
    return name in approved or any(a.endswith("__*") and name.startswith(a[:-1]) for a in approved)


def config_findings(text: str, line: dict) -> list[str]:
    raw = _config(text)
    if raw is None:
        return [f"{line['repo']}: {PATH} is missing or isn't a YAML mapping"]
    read, approval = _lists(raw)
    out = []
    for name in read:
        if name not in line["read"]:
            out.append(f"{line['repo']}: {PATH} lets {name} run without a person; the HLD "
                       + ("has it proposed" if _covered(name, line["propose"]) else "doesn't name it"))
    for name in approval:
        if not (_covered(name, line["propose"]) or name in line["read"]):
            out.append(f"{line['repo']}: {PATH} allows {name}, which the HLD doesn't name")
    return out


def build_findings(github, hld: str, results: dict) -> list[str]:
    """After the coder: each agent branch's agent.yaml stays within its approved line."""
    lines = {x["repo"]: x for x in parse_agent_tools(hld) if not x["problems"]}
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
    """The agent's tool lists as its binding sets them (JSON, as the template reads its settings).
    Missing or unusable: no tools at all, so the agent can call nothing until agent.yaml says."""
    raw = _config(text) or {}
    read, approval = _lists(raw)
    read = sorted({n for n in read if _TOOL.match(n)})  # a wildcard never reaches the read list
    allowed = sorted({n for n in [*read, *approval] if _TOOL.match(n) or _WILD.match(n)})
    return {"allowedTools": json.dumps(allowed), "readOnlyTools": json.dumps(read)}


def read_configs(github, repo: str, ref: str) -> dict:
    """binding_configs from agent.yaml at the commit being deployed ('' = the default branch)."""
    try:
        text = github.get_file(repo=repo, path=PATH, ref=ref or "")
    except Exception:  # noqa: BLE001 - the deploy goes on with no tools allowed
        text = ""
    return binding_configs(text or "")

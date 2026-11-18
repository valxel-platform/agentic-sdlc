"""Code-execution sandboxes (role `code-sandbox`, valxel-platform/template-code-sandbox).

An agent never runs model-written code in its own pod (the agent component type says so). When a
design has an agent run code (a calculation over a table, a chart, a file conversion), the code
goes to the product's code sandbox: one MCP tool, run_code, and a fresh single-use sandbox per run
(gVisor, no network, no service account token, limits).

At G4 (design_findings, endpoint_findings):
  - a design whose agents run code has a code-sandbox, and those agents call it in Endpoints;
  - a code sandbox is project-visible and calls nothing: it's reached by agents only, and reaches
    nothing but its own sandboxes;
  - only agents call it.
For the coder (coder_instruction): its SANDBOX_ALLOWED_CLIENTS are exactly the agents the design
says call it.
"""

from __future__ import annotations

import re

ROLE = "code-sandbox"
# How designs say an agent runs code it writes.
_RUNS_CODE = re.compile(
    r"\b(code[- ]execution|code interpreter|run_code|(?:runs?|running|executes?|executing)\s+"
    r"(?:the\s+|its\s+own\s+|model[- ]written\s+|generated\s+|python\s+|llm[- ]written\s+)+(?:code|scripts?|python))\b",
    re.I)


def _short(name: str) -> str:
    return name.split("/", 1)[-1]


def runs_code(hld: str) -> bool:
    return bool(_RUNS_CODE.search(hld or ""))


def design_findings(hld: str, repos: list[dict], product_repos: list[dict] | None = None) -> list[str]:
    """G4: agents that run code have a sandbox to run it in."""
    from .agents.parse import parse_endpoints

    every = repos + [r for r in product_repos or [] if r["name"] not in {x["name"] for x in repos}]
    agents = [r for r in repos if r.get("role") == "agent"]
    sandboxes = {_short(r["name"]) for r in every if r.get("role") == ROLE}
    if not agents or not runs_code(hld):
        return []
    if not sandboxes:
        return ["The design has agents run code: add a code sandbox (role code-sandbox, "
                "valxel-platform/template-code-sandbox) and have the agents call it; an agent never runs "
                "model-written code in its own pod (VX-17)"]
    calls = {e["repo"]: set(e["calls"]) for e in parse_endpoints(hld)}
    if not any(calls.get(a["name"], set()) & sandboxes for a in agents):
        return [f"The design has agents run code, but no agent calls {', '.join(sorted(sandboxes))} in Endpoints "
                "(VX-17)"]
    return []


def endpoint_findings(endpoints: dict[str, dict], roles: dict[str, str]) -> list[str]:
    """VX-17 for code sandboxes: project-visible, calling nothing, called by agents only."""
    sandboxes = {_short(n) for n, role in roles.items() if role == ROLE}
    out = []
    for repo, e in endpoints.items():
        if roles.get(repo) == ROLE:
            if e["visibility"] != ["project"]:
                out.append(f"{repo} is a code sandbox: its visibility is project (agents in the product reach it; "
                           "nothing outside does) (VX-17)")
            if e["calls"]:
                out.append(f"{repo} is a code sandbox: it calls nothing (its sandboxes have no network) (VX-17)")
        elif roles.get(repo) and roles[repo] != "agent":
            wrong = sorted(set(e["calls"]) & sandboxes)
            if wrong:
                out.append(f"{repo} calls the code sandbox {', '.join(wrong)}: only agents run code there (VX-17)")
    return out


def allowed_agents(hld: str, repo: str) -> list[str]:
    """The agents the design says call this sandbox."""
    from .agents.parse import parse_endpoints

    me = _short(repo)
    return sorted(_short(e["repo"]) for e in parse_endpoints(hld) if me in e["calls"])


def coder_instruction(hld: str, repo: str) -> str:
    agents = allowed_agents(hld, repo)
    who = ", ".join(agents) if agents else "none yet (refuse everyone)"
    comp = _short(repo)
    return (f" SANDBOX_ALLOWED_CLIENTS in workload.yaml is exactly the agents the HLD's Endpoints say call this "
            f"sandbox: {who}. Keep run_code's limits or lower them; never give the runner network, mounts, "
            "secrets or a service account; audit sandbox.run with the code's SHA-256, never the code, input or "
            "output (VX-15, VX-17). Runs go to the platform's sandboxes (the code-runner resource the pipeline "
            f"creates): set SANDBOX_EXECUTOR=kubernetes and bind the warm pool in workload.yaml, "
            f"dependencies.resources: - ref: {comp}-runner, envBindings: pool: SANDBOX_SANDBOX_TEMPLATE. "
            "Never define a SandboxTemplate, warm pool or network policy here: the platform owns what a sandbox is.")

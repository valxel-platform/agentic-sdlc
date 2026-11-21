"""`valxel dev cluster`: the laptop tier with OpenChoreo instead of plain Docker (ADR 0007).

The default laptop mode builds and runs products on this machine's Docker (lite/local.py). This
mode runs OpenChoreo itself in a local k3d cluster, the same version the platform installs, so
products go through the cluster tier's path: components, workflow runs, releases and release
bindings over OpenChoreo's MCP API. Use it to test what the tool asks of OpenChoreo before it
reaches the shared platform.

- `up` creates the cluster with OpenChoreo's own k3d installer (pinned below, checked by hash),
  with the workflow plane for builds; gives builds read access to your products' repos (the GitHub
  token in .env, stored in the cluster's OpenBao, never on a command line); adds the names the
  tool's repo standards use (laptop only, see `_aliases`); and points .env at it.
- `check` compares what the tool needs with what this OpenChoreo offers: the MCP tools it calls,
  and the component types and workflows config/repos.yaml names.
- `status` and `down`.

The tool's page moves to port 8090 in this mode: OpenChoreo's gateway takes 8080.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

import yaml

from . import home, read_env, write_env

OPENCHOREO_VERSION = "1.3.3"  # platform-foundation gitops/openchoreo/*/release.yaml
INSTALLER = f"https://raw.githubusercontent.com/openchoreo/openchoreo/v{OPENCHOREO_VERSION}/install/k3d/k3d-install.sh"
INSTALLER_SHA256 = "0e5f42a123c7df8d01c050b467bf81a43c96fba8a548d39693b5c0385766f7af"
CLUSTER = "valxel"
CONTEXT = f"k3d-{CLUSTER}"
NAMESPACE = "default"
GIT_SECRET_REF = "valxel-github"
API = "http://api.openchoreo.localhost:8080"
TOOL_PORT = "8090"

# What .env gets in this mode. The client is the installer's development system app (its id and
# secret are public, in OpenChoreo's k3d values); fine for a cluster that only this machine reaches.
SETTINGS = {
    "VALXEL_LOCAL_RUNTIME": "openchoreo",
    "VALXEL_OPENCHOREO_MCP_URL": f"{API}/mcp",
    "VALXEL_OPENCHOREO_TOKEN_URL": "http://thunder.openchoreo.localhost:8080/oauth2/token",
    "VALXEL_OPENCHOREO_CLIENT_ID": "openchoreo-system-app",
    "VALXEL_OPENCHOREO_CLIENT_SECRET": "openchoreo-system-app-secret",
    "VALXEL_OPENCHOREO_RESOURCE": f"{API}/mcp",
    "VALXEL_OPENCHOREO_CONSOLE_URL": "http://openchoreo.localhost:8080",
    "VALXEL_OPENCHOREO_OBSERVER_MCP_URL": "http://observer.openchoreo.localhost:11080/mcp",
    "VALXEL_OPENCHOREO_GIT_SECRET_REF": GIT_SECRET_REF,
    "VALXEL_PORT": TOOL_PORT,
}

# Laptop-only names: the tool's repo standards build with `docker` and deploy web apps as
# `web-app`; a fresh OpenChoreo calls these `dockerfile-builder` and `web-application`. The
# platform's own `docker` workflow signs with a cloud KMS key, so it can't run here.
WORKFLOW_ALIASES = {"docker": "dockerfile-builder"}
TYPE_ALIASES = {"web-app": "web-application"}


def _run(cmd: list[str], *, input: str | None = None, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, input=input, capture_output=True, text=True, check=check)


def _kubectl(*args: str) -> list[str]:
    return ["kubectl", "--context", CONTEXT, *args]


def missing_tools(which=shutil.which) -> list[str]:
    return [t for t in ("docker", "k3d", "kubectl", "helm") if not which(t)]


def fetch_installer(dest: Path, *, opener=urllib.request.urlopen) -> Path:
    """OpenChoreo's k3d installer at the pinned release, refused if it isn't the file we reviewed."""
    with opener(INSTALLER, timeout=60) as resp:
        body = resp.read()
    digest = hashlib.sha256(body).hexdigest()
    if digest != INSTALLER_SHA256:
        raise RuntimeError(f"the OpenChoreo installer's hash changed ({digest}); not running it")
    dest.write_bytes(body)
    dest.chmod(0o700)
    return dest


def install(*, run=_run, opener=urllib.request.urlopen) -> None:
    with tempfile.TemporaryDirectory() as d:
        script = fetch_installer(Path(d) / "k3d-install.sh", opener=opener)
        # Its own output (progress, then the console's address) goes straight to the terminal.
        subprocess.run(["bash", str(script), "--version", OPENCHOREO_VERSION, "--with-build",
                        "--cluster-name", CLUSTER], check=True)


def store_git_token(token: str, *, run=_run) -> None:
    """Builds clone private repos with HTTPS basic auth (OpenChoreo's checkout step). The token goes
    to the cluster's OpenBao on stdin, and a SecretReference names it for workflow runs."""
    for key, value in (("valxel-github-username", "x-access-token"), ("valxel-github-password", token)):
        run(_kubectl("exec", "-i", "-n", "openbao", "openbao-0", "--", "sh", "-c",
                     f"BAO_TOKEN=root bao kv put secret/{key} value=-"), input=value)
    ref = {
        "apiVersion": "openchoreo.dev/v1alpha1", "kind": "SecretReference",
        "metadata": {"name": GIT_SECRET_REF, "namespace": NAMESPACE},
        "spec": {"template": {"type": "kubernetes.io/basic-auth"}, "refreshInterval": "1h",
                 "data": [{"secretKey": "username", "remoteRef": {"key": "valxel-github-username", "property": "value"}},
                          {"secretKey": "password", "remoteRef": {"key": "valxel-github-password", "property": "value"}}]},
    }
    run(_kubectl("apply", "-f", "-"), input=json.dumps(ref))


def _copy(obj: dict, name: str) -> dict:
    meta = obj.get("metadata") or {}
    return {"apiVersion": obj["apiVersion"], "kind": obj["kind"], "spec": obj["spec"],
            "metadata": {"name": name, "labels": meta.get("labels") or {},
                         "annotations": {**(meta.get("annotations") or {}),
                                         "valxel.dev/laptop-alias-of": meta.get("name", "")}}}


def aliases(*, run=_run) -> list[str]:
    """Apply the laptop-only names (WORKFLOW_ALIASES, TYPE_ALIASES) and allow the `docker` workflow
    on every component type. Returns what it applied."""
    out: list[dict] = []
    for alias, source in WORKFLOW_ALIASES.items():
        out.append(_copy(json.loads(run(_kubectl("get", "clusterworkflow", source, "-o", "json")).stdout), alias))
    types = json.loads(run(_kubectl("get", "clustercomponenttypes", "-o", "json")).stdout).get("items") or []
    by_name = {t["metadata"]["name"]: t for t in types}
    for alias, source in TYPE_ALIASES.items():
        if source in by_name:
            by_name[alias] = _copy(by_name[source], alias)
    for t in by_name.values():
        allowed = t["spec"].setdefault("allowedWorkflows", [])
        for alias in WORKFLOW_ALIASES:
            if {"kind": "ClusterWorkflow", "name": alias} not in allowed:
                allowed.append({"kind": "ClusterWorkflow", "name": alias})
        out.append(t)
    for o in out:
        for k in ("resourceVersion", "uid", "creationTimestamp", "generation", "managedFields"):
            o["metadata"].pop(k, None)
        o.pop("status", None)
    run(_kubectl("apply", "-f", "-"), input=json.dumps({"apiVersion": "v1", "kind": "List", "items": out}))
    return [f"{o['kind']}/{o['metadata']['name']}" for o in out]


def point_env(target: Path, *, on: bool) -> None:
    env = read_env(target / ".env")
    for k, v in SETTINGS.items():
        if on:
            env[k] = v
        else:
            env.pop(k, None)
    write_env(target / ".env", env)


def up(target: Path | None = None, *, run=_run, opener=urllib.request.urlopen, installer=None) -> int:
    target = target or home()
    lacking = missing_tools()
    if lacking:
        print(f"Install first: {', '.join(lacking)} (OpenChoreo needs Docker, k3d, kubectl and Helm)", file=sys.stderr)
        return 1
    env = read_env(target / ".env")
    if not env:
        print("Run `valxel dev init` first.", file=sys.stderr)
        return 1
    have = run(["k3d", "cluster", "list", "-o", "json"], check=False)
    exists = have.returncode == 0 and any(c.get("name") == CLUSTER for c in json.loads(have.stdout or "[]"))
    if not exists:
        (installer or install)(run=run, opener=opener)
    if env.get("VALXEL_GITHUB_TOKEN"):
        store_git_token(env["VALXEL_GITHUB_TOKEN"], run=run)
    else:
        print("No VALXEL_GITHUB_TOKEN in .env: builds can only clone public repos.", file=sys.stderr)
    applied = aliases(run=run)
    point_env(target, on=True)
    print(f"OpenChoreo {OPENCHOREO_VERSION} is up (console http://openchoreo.localhost:8080, "
          "admin@openchoreo.dev / Admin@123).")
    print(f"Laptop names added: {', '.join(applied)}.")
    print(f"Next: valxel dev up (the tool moves to http://localhost:{TOOL_PORT}), then valxel dev cluster check.")
    return 0


def down(target: Path | None = None, *, run=_run) -> int:
    target = target or home()
    run(["k3d", "cluster", "delete", CLUSTER], check=False)
    if (target / ".env").exists():
        point_env(target, on=False)
    print("Cluster deleted; the laptop is back on plain Docker. Run valxel dev up to restart the tool.")
    return 0


def status(*, run=_run) -> int:
    r = run(_kubectl("get", "pods", "-A", "--no-headers"), check=False)
    if r.returncode != 0:
        print("No local OpenChoreo cluster (valxel dev cluster up).")
        return 1
    rows = [line.split() for line in r.stdout.splitlines() if line.strip()]
    bad = [f"{ns}/{name} {state}" for ns, name, _ready, state, *_ in rows if state not in ("Running", "Completed")]
    print(f"{len(rows)} pods, {len(rows) - len(bad)} running or done.")
    for b in bad:
        print(f"  {b}")
    return 0 if not bad else 1


# ------------------------------------------------------------------ check


def repos_yaml(*, run=_run) -> str:
    """config/repos.yaml: from a checkout (or VALXEL_ROOT), else from the running tool's image,
    since an installed wheel doesn't carry config/."""
    from ..config import _repo_root as repo_root

    path = repo_root() / "config" / "repos.yaml"
    if path.exists():
        return path.read_text()
    return run(["docker", "compose", "-p", "valxel", "exec", "-T", "api", "cat", "/app/config/repos.yaml"]).stdout


def repo_standards(text: str) -> tuple[set[str], set[str]]:
    """The component types and workflows config/repos.yaml asks OpenChoreo for."""
    data = yaml.safe_load(text) or {}
    types, flows = set(), set()

    def walk(node):
        if isinstance(node, dict):
            oc = node.get("openchoreo")
            if isinstance(oc, dict):
                if oc.get("component_type"):
                    types.add(str(oc["component_type"]).split("/", 1)[-1])
                if oc.get("workflow"):
                    flows.add(str(oc["workflow"]))
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(data)
    return types, flows


def check(*, tool_names: set[str], installed_types: set[str], installed_flows: set[str],
          wanted_types: set[str], wanted_flows: set[str], observer_tools: set[str] | None = None) -> list[str]:
    """What the tool needs and this OpenChoreo lacks, one line each (empty: nothing). observer_tools:
    what the observer's MCP server offers, when one is set (None: not checked)."""
    from ..integrations.openchoreo import OBSERVER_TOOLS, tools_used

    out = []
    if observer_tools is not None:
        out += [f"Observer MCP tool missing: {t}" for t in OBSERVER_TOOLS if t not in observer_tools]
    for names in tools_used():
        if not tool_names & set(names):
            out.append(f"MCP tool missing: {' or '.join(names)}")
    out += [f"Component type missing: {t}" for t in sorted(wanted_types - installed_types)]
    out += [f"Workflow missing: {w}" for w in sorted(wanted_flows - installed_flows)]
    return out


def live_check(target: Path | None = None, *, run=_run) -> int:
    from ..integrations.mcp_client import ClientCredentials, McpHttp

    env = {**SETTINGS, **read_env((target or home()) / ".env")}
    creds = ClientCredentials(env["VALXEL_OPENCHOREO_TOKEN_URL"], env["VALXEL_OPENCHOREO_CLIENT_ID"],
                              env["VALXEL_OPENCHOREO_CLIENT_SECRET"], resource=env["VALXEL_OPENCHOREO_RESOURCE"])
    names = set(McpHttp("openchoreo", env["VALXEL_OPENCHOREO_MCP_URL"], auth=creds).tool_names())

    def kinds(kind: str) -> set[str]:
        items = json.loads(run(_kubectl("get", kind, "-o", "json")).stdout).get("items") or []
        return {i["metadata"]["name"] for i in items}

    observer = None
    if env.get("VALXEL_OPENCHOREO_OBSERVER_MCP_URL"):
        ocreds = ClientCredentials(env["VALXEL_OPENCHOREO_TOKEN_URL"], env["VALXEL_OPENCHOREO_CLIENT_ID"],
                                   env["VALXEL_OPENCHOREO_CLIENT_SECRET"],
                                   resource=env["VALXEL_OPENCHOREO_OBSERVER_MCP_URL"])
        try:
            observer = set(McpHttp("openchoreo-observer", env["VALXEL_OPENCHOREO_OBSERVER_MCP_URL"],
                                   auth=ocreds).tool_names())
        except Exception:  # noqa: BLE001 - reported as missing tools below
            observer = set()
    wanted_types, wanted_flows = repo_standards(repos_yaml(run=run))
    found = check(tool_names=names, installed_types=kinds("clustercomponenttypes"),
                  installed_flows=kinds("clusterworkflows"), wanted_types=wanted_types, wanted_flows=wanted_flows,
                  observer_tools=observer)
    print(f"OpenChoreo {OPENCHOREO_VERSION}: {len(names)} MCP tools offered.")
    for line in found:
        print(f"  {line}")
    if not found:
        print("  Everything the tool calls and names is here.")
    return 0 if not found else 1


def main(args) -> int:
    from pathlib import Path as _P

    target = _P(args.home) if getattr(args, "home", None) else None
    action = (args.services or ["status"])[0]
    if action == "up":
        return up(target)
    if action == "down":
        return down(target)
    if action == "status":
        return status()
    if action == "check":
        return live_check(target)
    print("valxel dev cluster up | down | status | check", file=sys.stderr)
    return 2


__all__ = ["OPENCHOREO_VERSION", "SETTINGS", "check", "down", "status", "up"]

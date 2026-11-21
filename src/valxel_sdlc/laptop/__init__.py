"""`valxel dev`: the laptop tier (ADR 0007), the tool end to end on one machine.

- `init` copies the bundle (compose.yaml and friends, beside this file) to ~/.valxel/laptop (or
  VALXEL_HOME), writes its .env from the model key and GitHub token you give it, makes the tool's
  gateway key and writes the gateway policy with only that key's SHA-256. Keys are never printed.
- `doctor` checks what `up` needs: Docker and Compose, the socket's group, the keys, free ports.
- `up` / `down` / `logs` run Docker Compose on the bundle.
- `urls` lists what this machine serves: the tool, and each product environment's external endpoints.
- `github-apps` registers this laptop's own Delivery and Provisioning GitHub Apps, so the sign-up
  wizard's GitHub step runs for real (laptop/github_apps.py).
- `cluster up|down|status|check` runs products through OpenChoreo in a local k3d cluster instead of
  plain Docker (laptop/cluster.py).

Model keys stay in the gateway: the tool's containers get only the gateway URL and its own key.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import shutil
import socket
import stat
import subprocess
import sys
from pathlib import Path

BUNDLE_FILES = ("compose.yaml", "worker.Dockerfile", "gateway-policy.yaml", ".env.example", "initdb/10-audit.sql")
ENV_KEYS = ("ANTHROPIC_API_KEY", "VALXEL_GATEWAY_KEY", "VALXEL_GITHUB_TOKEN", "VALXEL_GOVERNANCE_SOURCE", "DOCKER_GID")
TOOL_PORT = 8080


def tool_port(target: Path | None = None) -> int:
    return int(read_env((target or home()) / ".env").get("VALXEL_PORT") or TOOL_PORT)


def home() -> Path:
    return Path(os.environ.get("VALXEL_HOME") or Path.home() / ".valxel") / "laptop"


def bundle() -> Path:
    return Path(__file__).resolve().parent / "bundle"


def read_env(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if path.exists():
        for line in path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip()
    return out


def write_env(path: Path, values: dict[str, str]) -> None:
    """Fill the example's keys, keeping its comments; extra keys go at the end. Owner-only."""
    lines, seen = [], set()
    for line in (bundle() / ".env.example").read_text().splitlines():
        k = line.split("=", 1)[0].strip() if "=" in line and not line.lstrip().startswith("#") else ""
        if k:
            seen.add(k)
            lines.append(f"{k}={values.get(k, '')}")
        else:
            lines.append(line)
    lines += [f"{k}={v}" for k, v in values.items() if k not in seen]
    path.write_text("\n".join(lines) + "\n")
    path.chmod(stat.S_IRUSR | stat.S_IWUSR)


def gateway_policy(tool_key: str) -> str:
    digest = hashlib.sha256(tool_key.encode()).hexdigest()
    return (bundle() / "gateway-policy.yaml").read_text().replace("{tool_key_sha256}", digest)


def docker_gid(sock: str = "/var/run/docker.sock") -> str:
    try:
        return str(os.stat(sock).st_gid)
    except OSError:
        return "0"


def init(*, target: Path | None = None, anthropic_key: str = "", github_token: str = "",
         governance: str = "", force: bool = False) -> Path:
    """Lay out the bundle and its secrets. Re-running keeps the keys already set unless given anew."""
    target = target or home()
    target.mkdir(parents=True, exist_ok=True)
    target.chmod(0o700)
    for rel in BUNDLE_FILES:
        dst = target / rel
        if dst.exists() and not force and rel != "compose.yaml" and rel != "worker.Dockerfile":
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(bundle() / rel, dst)
    env = read_env(target / ".env")
    if anthropic_key:
        env["ANTHROPIC_API_KEY"] = anthropic_key
    if github_token:
        env["VALXEL_GITHUB_TOKEN"] = github_token
    if governance:
        env["VALXEL_GOVERNANCE_SOURCE"] = governance
    env.setdefault("VALXEL_GOVERNANCE_SOURCE", "local")
    if not env.get("VALXEL_GATEWAY_KEY"):
        env["VALXEL_GATEWAY_KEY"] = "vxg_" + secrets.token_urlsafe(32)
    env["DOCKER_GID"] = docker_gid()
    write_env(target / ".env", env)
    policy = target / "gateway-policy.local.yaml"
    policy.write_text(gateway_policy(env["VALXEL_GATEWAY_KEY"]))
    policy.chmod(0o644)  # read by the gateway's container user; holds only a hash
    launcher_token(target)
    return target


def launcher_token(target: Path) -> Path:
    """The worker's token for the session launcher, once (T-68). Readable by the containers' user,
    which Compose mounts it for; not in .env."""
    d = target / "secrets"
    d.mkdir(exist_ok=True)
    d.chmod(0o755)
    f = d / "session-launcher-token"
    if not f.exists() or not f.read_text().strip():
        f.write_text("vxl_" + secrets.token_urlsafe(32) + "\n")
    f.chmod(0o644)
    return f


def _run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, check=False, **kw)


def _port_free(port: int) -> bool:
    with socket.socket() as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def doctor(target: Path | None = None, *, run=_run, port_free=_port_free) -> list[tuple[str, bool, str]]:
    """Each check: (what, ok, how to fix)."""
    target = target or home()
    checks: list[tuple[str, bool, str]] = []
    have_docker = shutil.which("docker") is not None
    checks.append(("Docker CLI", have_docker, "Install Docker Desktop, OrbStack, Colima or Docker Engine"))
    if have_docker:
        info = run(["docker", "info", "--format", "{{.ServerVersion}}"])
        checks.append(("Docker running", info.returncode == 0, "Start Docker"))
        comp = run(["docker", "compose", "version", "--short"])
        checks.append(("Docker Compose v2", comp.returncode == 0, "Install the Compose plugin (docker compose)"))
    checks.append(("Initialised", (target / ".env").exists(), "Run: valxel dev init"))
    env = read_env(target / ".env")
    checks.append(("Model key (for the gateway)", bool(env.get("ANTHROPIC_API_KEY")),
                   "valxel dev init --anthropic-key-stdin"))
    checks.append(("GitHub token or App", bool(env.get("VALXEL_GITHUB_TOKEN") or env.get("VALXEL_GITHUB_APP_ID")),
                   ("valxel dev init --github-token-stdin (fine-grained: Administration, Contents, Pull "
                    "requests, Workflows)")))
    checks.append(("Gateway policy", (target / "gateway-policy.local.yaml").exists(), "Run: valxel dev init"))
    checks.append(("Session launcher token", (target / "secrets" / "session-launcher-token").exists(),
                   "Run: valxel dev init"))
    port = tool_port(target)
    running = have_docker and run(["docker", "compose", "-p", "valxel", "ps", "-q", "api"]).stdout.strip()
    checks.append((f"Port {port} free", bool(running) or port_free(port),
                   f"Stop what listens on 127.0.0.1:{port}"))
    if env.get("VALXEL_LOCAL_RUNTIME") == "openchoreo":
        ctx = have_docker and run(["kubectl", "config", "get-contexts", "-o", "name"]).stdout.split()
        checks.append(("Local OpenChoreo cluster", bool(ctx) and "k3d-valxel" in ctx, "valxel dev cluster up"))
    return checks


def compose(target: Path, *args: str) -> list[str]:
    return ["docker", "compose", "-p", "valxel", "-f", str(target / "compose.yaml"),
            "--env-file", str(target / ".env"), *args]


def urls(*, run=_run, port: int = TOOL_PORT) -> dict[str, str]:
    """The tool, and every product endpoint published on this machine (lite/local.py)."""
    out = {"valxel": f"http://localhost:{port}"}
    ps = run(["docker", "ps", "--filter", "label=dev.valxel.component", "--format", "{{json .Labels}}\t{{.Names}}"])
    for line in ps.stdout.splitlines():
        raw, _, name = line.partition("\t")
        labels = dict(kv.split("=", 1) for kv in json.loads(raw).split(",") if "=" in kv)
        for k, v in labels.items():
            if k.startswith("dev.valxel.url."):
                out[f"{labels.get('dev.valxel.env', '')}/{name}/{k.removeprefix('dev.valxel.url.')}"] = v
    return out


def main(args) -> int:
    target = Path(args.home) if getattr(args, "home", None) else home()
    if args.action == "github-apps":
        from .github_apps import main as apps_main
        return apps_main(args)
    if args.action == "cluster":
        from .cluster import main as cluster_main
        return cluster_main(args)
    if args.action == "init":
        # Keys come on stdin (a pipe or a paste), never as arguments, so they stay out of shell history.
        key = sys.stdin.readline().strip() if args.anthropic_key_stdin else ""
        tok = sys.stdin.readline().strip() if args.github_token_stdin else ""
        init(target=target, anthropic_key=key, github_token=tok, governance=args.governance or "", force=args.force)
        print(f"Ready in {target}. Next: valxel dev doctor, then valxel dev up.")
        return 0
    if args.action == "doctor":
        checks = doctor(target)
        for what, ok, fix in checks:
            print(f"{'ok ' if ok else 'NO '} {what}" + ("" if ok else f"  -> {fix}"))
        from .github_apps import installed_on

        for app, where in installed_on(read_env(target / ".env")).items():
            # Information, not a blocker: the sign-up wizard is where the Apps get installed.
            if isinstance(where, str):
                print(f"NO  {app} GitHub App: {where}  -> valxel dev github-apps")
            else:
                print(f"--  {app} GitHub App installed on: {', '.join(where) or 'nothing yet (the wizard installs it)'}")
        return 0 if all(ok for _, ok, _ in checks) else 1
    if args.action == "up":
        bad = [c for c in doctor(target) if not c[1]]
        if bad:
            print("Not ready: " + "; ".join(f"{w} ({f})" for w, _, f in bad), file=sys.stderr)
            return 1
        rc = subprocess.call(compose(target, "up", "-d", "--build", "--wait"))
        if rc == 0:
            print(f"Valxel is up: http://localhost:{tool_port(target)}")
        return rc
    if args.action == "down":
        return subprocess.call(compose(target, "down", *(["--volumes"] if args.wipe else [])))
    if args.action == "logs":
        return subprocess.call(compose(target, "logs", "-f", "--tail", "200", *args.services))
    if args.action == "urls":
        for k, v in urls(port=tool_port(target)).items():
            print(f"{k}\t{v}")
        return 0
    return 2


def add_parser(sub) -> None:
    dv = sub.add_parser("dev", help="The laptop tier: the tool end to end on this machine (init, doctor, up, "
                                    "down, logs, urls, github-apps; cluster up|down|status|check for a local OpenChoreo)")
    dv.add_argument("action", choices=["init", "doctor", "up", "down", "logs", "urls", "cluster", "github-apps"])
    dv.add_argument("services", nargs="*", help="With logs: only these services. With cluster: up, down, status "
                                                "or check")
    dv.add_argument("--home", help="Where the bundle lives (default ~/.valxel/laptop)")
    dv.add_argument("--anthropic-key-stdin", action="store_true", help="init: read the model key from stdin")
    dv.add_argument("--github-token-stdin", action="store_true", help="init: read the GitHub token from stdin "
                                                                     "(after the model key, if both)")
    dv.add_argument("--governance", choices=["local", "github"], help="init: where governance is read from")
    dv.add_argument("--force", action="store_true", help="init: overwrite the policy template and examples")
    dv.add_argument("--wipe", action="store_true", help="down: also delete the databases and artifacts")
    dv.add_argument("--org", help="github-apps: register the Apps under this GitHub org (the one you'll install "
                                  "them on); default your own account")

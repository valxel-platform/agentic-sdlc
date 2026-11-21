"""The single-VM tier (ADR 0007, T-69): the bundle is hardened, init generates the tool's own secrets
and leaves the operator's for doctor to name, upgrades go by digest with a signature check, a backup
first and a way back, and the tool alerts when backups stop."""

from __future__ import annotations

import subprocess
import time
from datetime import UTC, datetime

import pytest
import yaml

from valxel_sdlc import vm

D1 = "ghcr.io/valxel-platform/agentic-sdlc@sha256:" + "a" * 64
D2 = "ghcr.io/valxel-platform/agentic-sdlc@sha256:" + "b" * 64


def compose():
    return yaml.safe_load((vm.bundle() / "compose.yaml").read_text())


def test_the_bundle_is_hardened():
    c = compose()
    s = c["services"]
    assert {n for n, v in s.items() if v.get("ports")} == {"caddy"}  # only TLS is published
    sock = [n for n, v in s.items() if any("docker.sock" in str(x) for x in v.get("volumes") or [])]
    assert sock == ["session-launcher"]  # the tool itself never reaches Docker
    env = s["api"]["environment"]
    assert env["VALXEL_TIER"] == "vm" and env["VALXEL_STRICT"] == "1" and env["VALXEL_SECRET_STORE"] == "files"
    assert env["VALXEL_SESSION_RUNTIME"] == "runsc" and env["VALXEL_PUBLIC_URL"].startswith("https://")
    assert env["VALXEL_TENANCY"] == "rls" and env["VALXEL_BACKUP_MARKER"] == "/backups/last-success"
    for name in ("api", "worker", "provisioner", "session-launcher", "egress-proxy"):
        v = s[name]
        assert v["read_only"] and v["cap_drop"] == ["ALL"] and "no-new-privileges:true" in v["security_opt"]
    holders = [n for n, v in s.items() if "VALXEL_GITHUB_PROVISIONER_PRIVATE_KEY" in (v.get("secrets") or [])]
    assert holders == ["provisioner"]  # only it can sign for the Provisioning App (S4)
    from valxel_sdlc.secret_ring import is_secret

    in_env = [f"{n}.{k}" for n, v in s.items() for k in (v.get("environment") or {}) if is_secret(k)]
    assert in_env == []  # secrets are files, never environment variables
    for name in ("postgres", "kafka", "audit-sink", "api", "worker"):
        assert "sessions" not in (s[name].get("networks") or [])
    assert c["networks"]["sessions"]["internal"] is True


def test_init_generates_the_tools_secrets_and_leaves_the_operators(tmp_path, monkeypatch):
    monkeypatch.setattr(vm, "docker_gid", lambda: "991")
    t = vm.init(target=tmp_path / "vm", domain="valxel.acme.dev", email="ops@acme.dev")
    sec = t / "secrets"
    for name in vm.GENERATED:
        assert len((sec / name).read_text()) >= 32
    db = (sec / "VALXEL_DB_PASSWORD").read_text()
    assert (sec / "DATABASE_URL").read_text() == f"postgresql://valxel:{db}@postgres:5432/valxel"
    assert all((sec / n).read_text() == "" for n in vm.OPERATOR)
    env = vm.read_env(t / ".env")
    assert env["VALXEL_DOMAIN"] == "valxel.acme.dev" and env["DOCKER_GID"] == "991"
    assert oct((t / ".env").stat().st_mode)[-3:] == "600" and oct(sec.stat().st_mode)[-3:] == "700"
    gw = (sec / "VALXEL_LLM_GATEWAY_API_KEY").read_text()
    assert gw not in (t / "gateway-policy.local.yaml").read_text()  # only its hash
    before = {n: (sec / n).read_text() for n in vm.GENERATED}
    vm.init(target=t, domain="", email="")
    assert {n: (sec / n).read_text() for n in vm.GENERATED} == before  # kept
    assert vm.read_env(t / ".env")["VALXEL_DOMAIN"] == "valxel.acme.dev"


def ok(cmd, **kw):
    if cmd[:2] == ["docker", "info"]:
        return subprocess.CompletedProcess(cmd, 0, stdout='{"runc":{},"runsc":{}}', stderr="")
    return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")


def test_doctor_names_what_is_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(vm.shutil, "which", lambda _: "/usr/bin/docker")
    monkeypatch.setattr(vm, "docker_gid", lambda: "991")
    t = vm.init(target=tmp_path, domain="valxel.acme.dev", email="ops@acme.dev")
    missing = [w for w, good, _ in vm.doctor(t, run=ok) if not good]
    assert "VALXEL_IMAGE pinned by digest" in missing and "secret VALXEL_GITHUB_APP_PRIVATE_KEY" in missing
    assert "Model key for the gateway" in missing and "gVisor (runsc) in Docker" not in missing
    env = vm.read_env(t / ".env")
    env.update({k: D1.replace("agentic-sdlc", k.lower()) for k in vm.IMAGES})
    env.update({"VALXEL_GITHUB_APP_ID": "1", "VALXEL_GITHUB_PROVISIONER_APP_ID": "2", "VALXEL_GITHUB_OAUTH_CLIENT_ID": "c"})
    vm.write_env(t / ".env", env)
    for n in vm.OPERATOR:
        (t / "secrets" / n).write_text("x")
    (t / "secrets" / "gateway.env").write_text("ANTHROPIC_API_KEY=sk-ant-x\n")
    assert all(good for _, good, _ in vm.doctor(t, run=ok))

    def no_gvisor(cmd, **kw):
        out = ok(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout='{"runc":{}}', stderr="") if cmd[:2] == ["docker", "info"] else out

    assert [w for w, good, _ in vm.doctor(t, run=no_gvisor) if not good] == ["gVisor (runsc) in Docker"]


class Docker:
    """Answers the upgrade's commands; the api is healthy only on the given image."""

    def __init__(self, target, healthy_on):
        self.target, self.healthy_on, self.cmds = target, healthy_on, []

    def __call__(self, cmd, **kw):
        self.cmds.append(cmd)
        if cmd[:2] == ["cosign", "verify"]:
            return subprocess.CompletedProcess(cmd, 0 if cmd[2] != D2 + "x" else 1, stdout="", stderr="")
        if "ps" in cmd:
            return subprocess.CompletedProcess(cmd, 0, stdout="cid\n", stderr="")
        if cmd[:2] == ["docker", "inspect"]:
            on = vm.read_env(self.target / ".env")["VALXEL_IMAGE"]
            return subprocess.CompletedProcess(cmd, 0, stdout="healthy" if on == self.healthy_on else "starting",
                                               stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")


def test_an_upgrade_checks_the_signature_backs_up_and_goes_back_if_unhealthy(tmp_path, monkeypatch):
    monkeypatch.setattr(vm.shutil, "which", lambda _: "/usr/bin/cosign")
    t = tmp_path
    vm.write_env(t / ".env", {"VALXEL_IMAGE": D1})
    with pytest.raises(ValueError, match="by digest"):
        vm.upgrade(t, "ghcr.io/valxel-platform/agentic-sdlc:latest")
    good = Docker(t, healthy_on=D2)
    assert vm.upgrade(t, D2, run=good, sleep=lambda s: None) == f"Upgraded to {D2}"
    assert ["cosign", "verify", D2] == good.cmds[0][:3]
    assert any("/backup.sh" in c for c in good.cmds)  # a backup before switching
    assert vm.read_env(t / ".env")["VALXEL_IMAGE_PREVIOUS"] == D1

    vm.write_env(t / ".env", {"VALXEL_IMAGE": D1})
    clock = iter(range(0, 10_000, 30))
    bad = Docker(t, healthy_on=D1)
    out = vm.upgrade(t, D2, run=bad, sleep=lambda s: None, clock=lambda: next(clock))
    assert out == f"{D2} wasn't healthy in time: went back to {D1}"
    assert vm.read_env(t / ".env")["VALXEL_IMAGE"] == D1


def test_the_tool_alerts_when_backups_stop(tmp_path, monkeypatch):
    import shutil

    from test_roadmap import ROOT, make

    gov = tmp_path / "gov"
    shutil.copytree(ROOT / "examples" / "governance", gov)
    eng, _ = make(tmp_path, gov)
    monkeypatch.delenv("VALXEL_BACKUP_MARKER", raising=False)
    assert eng.check_backups() == "no backups to check here"
    marker = tmp_path / "last-success"
    monkeypatch.setenv("VALXEL_BACKUP_MARKER", str(marker))
    with pytest.raises(RuntimeError, match="No backup has finished yet"):
        eng.check_backups()
    now = time.time()
    marker.write_text(datetime.fromtimestamp(now - 3 * 3600, UTC).strftime("%Y-%m-%dT%H:%M:%SZ"))
    assert eng.check_backups(now=now).startswith("last backup 3.0 hours ago")
    with pytest.raises(RuntimeError, match="30 hours ago"):
        eng.check_backups(now=now + 27 * 3600)
    [job] = [j for j in eng.watcher_jobs() if j.name == "check_backups"]
    assert job.every == 3600 and job.stale_hours == 2  # alerts within two hours of backups going stale

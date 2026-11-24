"""The laptop tier's bundle and `valxel dev` (valxel_sdlc.laptop), and loopback publishing."""

import hashlib
import stat
import subprocess

import yaml

from valxel_sdlc import laptop
from valxel_sdlc.cli import _main
from valxel_sdlc.lite.local import local_port, publish_on_loopback


def _compose():
    return yaml.safe_load((laptop.bundle() / "compose.yaml").read_text())


def test_model_key_reaches_only_the_gateway():
    services = _compose()["services"]
    holders = [n for n, s in services.items()
               if "ANTHROPIC_API_KEY" in (s.get("environment") or {}) or s.get("env_file")]
    assert holders == ["llm-gateway"]
    for name in ("api", "worker"):
        env = services[name]["environment"]
        assert env["VALXEL_LLM_GATEWAY_URL"] == "http://llm-gateway:8080"
        assert env["VALXEL_TIER"] == "laptop" and env["VALXEL_DELIVERY"] == "local"


def test_only_the_worker_and_the_session_launcher_reach_docker_and_only_the_api_is_published():
    services = _compose()["services"]
    sock = [n for n, s in services.items() if any("docker.sock" in str(v) for v in s.get("volumes") or [])]
    assert sock == ["worker", "session-launcher"]  # products on the laptop; sessions
    published = {n: s["ports"] for n, s in services.items() if s.get("ports")}
    # 8090 beside a local OpenChoreo; moto only in AWS mode (`valxel dev aws on`), for this machine's aws CLI
    assert published == {"api": ["127.0.0.1:${VALXEL_PORT:-8080}:8080"], "moto": ["127.0.0.1:5050:5000"]}


def test_init_writes_owner_only_env_and_a_policy_with_only_the_hash(tmp_path, monkeypatch):
    monkeypatch.setattr(laptop, "docker_gid", lambda: "991")
    t = laptop.init(target=tmp_path / "l", anthropic_key="sk-ant-test", github_token="ghp_test")
    env = laptop.read_env(t / ".env")
    assert env["ANTHROPIC_API_KEY"] == "sk-ant-test" and env["VALXEL_GITHUB_TOKEN"] == "ghp_test"
    assert env["DOCKER_GID"] == "991" and env["VALXEL_GOVERNANCE_SOURCE"] == "local"
    assert env["VALXEL_GATEWAY_KEY"].startswith("vxg_")
    assert stat.S_IMODE((t / ".env").stat().st_mode) == 0o600
    policy = (t / "gateway-policy.local.yaml").read_text()
    assert env["VALXEL_GATEWAY_KEY"] not in policy and "sk-ant-test" not in policy
    parsed = yaml.safe_load(policy)
    assert parsed["callers"]["sdlc-tool"]["key_sha256"] == hashlib.sha256(env["VALXEL_GATEWAY_KEY"].encode()).hexdigest()
    assert parsed["providers"]["anthropic"]["baa"] is False
    assert (t / "compose.yaml").exists() and (t / "initdb" / "10-audit.sql").exists()

    # Again, with nothing new: the keys stay.
    laptop.init(target=t)
    again = laptop.read_env(t / ".env")
    assert again["VALXEL_GATEWAY_KEY"] == env["VALXEL_GATEWAY_KEY"] and again["ANTHROPIC_API_KEY"] == "sk-ant-test"


def test_doctor_names_what_is_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(laptop.shutil, "which", lambda _: "/usr/bin/docker")

    def ok(cmd):
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    checks = {w: f for w, good, f in laptop.doctor(tmp_path, run=ok, port_free=lambda p: True) if not good}
    assert "Initialised" in checks and "Model key (for the gateway)" in checks
    monkeypatch.setattr(laptop, "docker_gid", lambda: "0")
    laptop.init(target=tmp_path, anthropic_key="k", github_token="t")
    assert all(good for _, good, _ in laptop.doctor(tmp_path, run=ok, port_free=lambda p: True))
    busy = [w for w, good, _ in laptop.doctor(tmp_path, run=ok, port_free=lambda p: False) if not good]
    assert busy == ["Port 8080 free"]


def test_cli_init_reads_keys_from_stdin(tmp_path, monkeypatch, capsys):
    import io
    monkeypatch.setattr("sys.stdin", io.StringIO("sk-ant-x\nghp_y\n"))
    rc = _main(["dev", "init", "--home", str(tmp_path), "--anthropic-key-stdin", "--github-token-stdin"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "sk-ant-x" not in out and "ghp_y" not in out
    env = laptop.read_env(tmp_path / ".env")
    assert env["ANTHROPIC_API_KEY"] == "sk-ant-x" and env["VALXEL_GITHUB_TOKEN"] == "ghp_y"


def test_external_endpoints_publish_on_loopback_at_stable_ports():
    spec = {"services": {
        "web": {"labels": {"dev.valxel.port.http": "3000", "dev.valxel.component": "farm-web"},
                "deploy": {"replicas": 2}},
        "api": {"labels": {"dev.valxel.component": "farm-api"}},
    }}
    urls = publish_on_loopback(spec, project="farm-development")
    port = local_port("farm-development", "web", "http")
    assert spec["services"]["web"]["ports"] == [f"127.0.0.1:{port}:3000"]
    assert "deploy" not in spec["services"]["web"]
    assert "ports" not in spec["services"]["api"]
    assert urls == {"web/http": f"http://127.0.0.1:{port}"}
    assert spec["services"]["web"]["labels"]["dev.valxel.url.http"] == urls["web/http"]
    assert local_port("farm-development", "web", "http") == port
    assert local_port("farm-staging", "web", "http") != port


def test_sessions_run_in_containers_of_their_own_on_an_internal_network(tmp_path, monkeypatch):
    c = _compose()
    s = c["services"]
    assert s["worker"]["environment"]["VALXEL_SESSION_EXECUTOR"] == "docker"
    assert s["session-launcher"]["command"] == ["valxel", "session-launcher"]
    assert "/var/run/docker.sock:/var/run/docker.sock" in s["session-launcher"]["volumes"]
    assert set(s["session-launcher"]["networks"]) == {"default", "sessions"}
    assert set(s["egress-proxy"]["networks"]) == {"default", "sessions"}
    assert "sessions" in s["llm-gateway"]["networks"]
    for name in ("postgres", "kafka", "audit-sink"):  # nothing else of the tool's is on the sessions network
        assert "sessions" not in (s[name].get("networks") or [])
    assert c["networks"]["sessions"] == {"name": "valxel-sessions", "internal": True}
    monkeypatch.setattr(laptop, "docker_gid", lambda: "0")
    t = laptop.init(target=tmp_path / "l")
    token = (t / "secrets" / "session-launcher-token").read_text().strip()
    assert token.startswith("vxl_") and token not in (t / ".env").read_text()
    laptop.init(target=t)
    assert (t / "secrets" / "session-launcher-token").read_text().strip() == token  # kept

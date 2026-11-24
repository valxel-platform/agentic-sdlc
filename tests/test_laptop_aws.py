"""`valxel dev aws`: the laptop tier's AWS on moto (valxel_sdlc.laptop.aws; tracker CP-116)."""

from pathlib import Path

import yaml

from valxel_sdlc import laptop
from valxel_sdlc.laptop import aws

BUNDLE = Path(laptop.__file__).resolve().parent / "bundle"


def test_on_and_off_put_the_settings_in_env_and_take_them_out(tmp_path, capsys):
    t = laptop.init(target=tmp_path / "l", anthropic_key="k", github_token="t")
    assert aws.main(t, ["status"]) == 0 and capsys.readouterr().out.strip() == "off"
    assert aws.main(t, ["on"]) == 0
    env = laptop.read_env(t / ".env")
    assert {k: env[k] for k in aws.SETTINGS} == aws.SETTINGS and aws.is_on(env)
    assert env["ANTHROPIC_API_KEY"] == "k"  # the rest of .env stays
    assert aws.main(t, ["off"]) == 0
    env = laptop.read_env(t / ".env")
    assert not any(k in env for k in aws.SETTINGS) and env["ANTHROPIC_API_KEY"] == "k"


def test_seed_and_connect_run_in_the_api_container_and_only_when_on(tmp_path):
    t = laptop.init(target=tmp_path / "l")
    ran = []
    assert aws.main(t, ["seed"], run=ran.append) == 1 and ran == []  # off: refused
    aws.main(t, ["on"])
    assert aws.main(t, ["seed"], run=lambda cmd: ran.append(cmd) or 0) == 0
    assert aws.main(t, ["connect", "acme", "lite"], run=lambda cmd: ran.append(cmd) or 0) == 0
    assert ran[0][-3:] == ["valxel", "aws-local", "seed"]
    assert ran[1][-4:] == ["aws-local", "connect", "acme", "lite"]
    assert "exec" in ran[0] and "api" in ran[0]
    assert aws.main(t, ["connect"], run=ran.append) == 2  # no workspace


def test_the_bundle_runs_moto_only_in_aws_mode_and_passes_the_settings_through():
    c = yaml.safe_load((BUNDLE / "compose.yaml").read_text())
    moto = c["services"]["moto"]
    assert moto["profiles"] == ["aws"] and moto["container_name"] == aws.MOTO_CONTAINER
    assert all(p.startswith("127.0.0.1:") for p in moto["ports"])  # this machine only
    env = c["x-tool"]["environment"]
    for k in aws.SETTINGS:
        if k.startswith("VALXEL_DEV_AWS_"):  # AWS_* inside, from .env's own names, never the shell's AWS_*
            assert env[k.removeprefix("VALXEL_DEV_")] == f"${{{k}:-}}"
        elif k != "COMPOSE_PROFILES":
            assert env[k] == f"${{{k}:-}}"
    assert not any(v == "${AWS_ACCESS_KEY_ID:-}" or "{AWS_" in str(v) for v in env.values())
    assert c["x-tool"]["depends_on"]["moto"]["required"] is False  # the tool starts without it


def test_strict_mode_refuses_the_laptop_aws_settings_as_the_containers_see_them():
    from valxel_sdlc.cloud import strict_problems

    inside = {k.removeprefix("VALXEL_DEV_"): v for k, v in aws.SETTINGS.items()}
    assert strict_problems(inside) and "moto" in strict_problems(inside)[0]

"""AWS on the laptop tier, without an AWS account: the tool's AWS calls go to moto, a local AWS
emulator running beside it (docs/dev-aws.md, tracker CP-116 and CP-118).

    valxel dev aws on              put the AWS settings in .env (the next `valxel dev up` starts moto)
    valxel dev aws off             take them out again
    valxel dev aws seed            make what the settings name in moto (`valxel dev up` does it too)
    valxel dev aws connect <ws>    connect a workspace to moto's account, as a customer would

`seed` and `connect` run `valxel aws-local` inside the tool's api container, where boto3 and the
database are (valxel_sdlc.cloud.aws.local). Moto keeps everything in memory, so `valxel dev up`
seeds again after a restart; with `valxel dev cluster up --aws`, Crossplane makes its resources
again by itself. Strict mode refuses an AWS endpoint override (valxel_sdlc.cloud.strict_problems)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from ..cloud.aws.local import ACCOUNT, ANCHORS, CHECKPOINTS, REGION

MOTO_CONTAINER = "valxel-moto"

# What .env gets in AWS mode. The AWS_* settings are named VALXEL_DEV_AWS_* here and become AWS_*
# only inside the containers (compose.yaml): Compose prefers the shell's variables to .env's, so
# a developer's own AWS keys in their shell never reach the tool. The template URL is never fetched
# on a laptop (`connect` deploys the file itself); it only has to look like one, so the AWS connect
# flow is switched on.
SETTINGS = {
    "COMPOSE_PROFILES": "aws",
    "VALXEL_DEV_AWS_ENDPOINT_URL": "http://moto:5000",
    "VALXEL_DEV_AWS_ACCESS_KEY_ID": "local",
    "VALXEL_DEV_AWS_SECRET_ACCESS_KEY": "local",
    "VALXEL_DEV_AWS_REGION": REGION,
    "VALXEL_CLOUD": "aws",
    "VALXEL_CHECKPOINT_ARCHIVE_BUCKET": CHECKPOINTS,
    "VALXEL_AUDIT_ANCHOR_BUCKET": ANCHORS,
    "VALXEL_AWS_ACCOUNT_ID": ACCOUNT,
    "VALXEL_AWS_STACK_TEMPLATE_URL": "https://valxel-local.invalid/customer-stack.yaml",
    "VALXEL_AWS_COST_EXPLORER": "1",
}


def is_on(env: dict[str, str]) -> bool:
    return env.get("VALXEL_DEV_AWS_ENDPOINT_URL") == SETTINGS["VALXEL_DEV_AWS_ENDPOINT_URL"]


def point_env(target: Path, *, on: bool) -> None:
    from . import read_env, write_env

    env = read_env(target / ".env")
    for k, v in SETTINGS.items():
        if on:
            env[k] = v
        else:
            env.pop(k, None)
    write_env(target / ".env", env)


def in_api(target: Path, *args: str, run=subprocess.call) -> int:
    from . import compose

    return run(compose(target, "exec", "-T", "api", "valxel", "aws-local", *args))


def main(target: Path, args: list[str], *, run=subprocess.call) -> int:
    from . import read_env

    action = (args or ["status"])[0]
    if action in ("on", "off"):
        if not (target / ".env").exists():
            print("Run `valxel dev init` first.", file=sys.stderr)
            return 1
        point_env(target, on=action == "on")
        print("AWS mode is on: AWS calls go to moto. Next: valxel dev up." if action == "on"
              else "AWS mode is off. Next: valxel dev up.")
        return 0
    if action == "status":
        print("on (moto)" if is_on(read_env(target / ".env")) else "off")
        return 0
    return _in_moto(target, action, args, run=run)


def _in_moto(target: Path, action: str, args: list[str], *, run) -> int:
    from . import read_env

    if not is_on(read_env(target / ".env")):
        print("AWS mode is off: valxel dev aws on, then valxel dev up.", file=sys.stderr)
        return 1
    if action == "seed":
        return in_api(target, "seed", run=run)
    if action == "connect" and len(args) >= 2:
        return in_api(target, "connect", *args[1:3], run=run)
    print("valxel dev aws on | off | status | seed | connect <workspace> [lite|full]", file=sys.stderr)
    return 2

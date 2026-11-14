"""The tool's secrets: where they come from, and key rings for rotating them.

**Where they come from** (VALXEL_SECRET_STORE), behind one small interface, `SecretStore`:

    aws    AWS Secrets Manager, read when needed and cached briefly. Each secret is
           `<prefix>/platform/<NAME>`, or `<prefix>/products/<product>/<NAME>` for a product's own
           (Flipt, ServiceNow, metrics), or `<prefix>/orgs/<org>/<NAME>`, so access policies can be
           scoped per customer. VALXEL_SECRET_PREFIX, default "valxel". The current version is
           AWSCURRENT and the previous AWSPREVIOUS, as Secrets Manager's own rotation leaves them.
    files  VALXEL_SECRETS_DIR/<NAME> and <NAME>.previous, as a secret manager or Vault Agent
           mounts them; re-read when they change. Works with any cloud's secret manager.
    env    <NAME> and <NAME>_PREVIOUS in the environment. For laptops and tests.

The default is `files` when VALXEL_SECRETS_DIR is set, else `env`. Other stores (HashiCorp
Vault, Azure Key Vault, Google Secret Manager) are another `SecretStore` with `versions(name)`.

**Key rings:** a secret has a current value and, during a rotation, the previous one. Checks
accept either; signing uses the current. Rotating, e.g. the GitHub webhook secret: make the new
value current and keep the old as previous, change the secret at GitHub, then drop previous once
nothing sends the old one. Sessions signed with the previous value stay valid until they expire.

`environ()` is a drop-in for os.environ whose secret names resolve through the store, so code
that takes an `env` mapping reads secrets from the right place unchanged. `weak` says why a value
isn't fit to protect anything.
"""

from __future__ import annotations

import hmac
import os
import re
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path
from typing import Protocol

# Values published in this repository or its docs: never acceptable outside a laptop.
PUBLISHED = frozenset({
    "local-dev-only-session-secret-change-me-0000",
})
WEAK_PASSWORDS = frozenset({"", "valxel", "postgres", "password", "admin", "changeme", "change-me", "secret"})

# What the tool treats as a secret: read through the store, never expected in the environment
# once a store is set (strict mode checks).
SECRET_NAMES = frozenset({
    "DATABASE_URL", "VALXEL_SESSION_SECRET", "VALXEL_GITHUB_OAUTH_CLIENT_SECRET", "VALXEL_GITHUB_WEBHOOK_SECRET",
    "VALXEL_SENTRY_CLIENT_SECRET", "VALXEL_SIGNALS_TOKEN", "VALXEL_OPENCHOREO_CLIENT_SECRET",
    "VALXEL_SMTP_PASSWORD", "VALXEL_PAGERDUTY_ROUTING_KEYS", "VALXEL_GLITCHTIP_TOKENS", "VALXEL_LANGFUSE_PUBLIC_KEY",
    "VALXEL_LANGFUSE_SECRET_KEY", "VALXEL_GITHUB_TOKEN", "VALXEL_GITHUB_APP_PRIVATE_KEY", "VALXEL_OTEL_HEADERS",
    "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN", "OPENAI_API_KEY", "CODEX_API_KEY",
})
PER_PRODUCT = ("VALXEL_FLIPT_TOKEN_", "VALXEL_SERVICENOW_AUTH_", "VALXEL_METRICS_DSN_", "VALXEL_PROMETHEUS_TOKEN_",
               "VALXEL_POSTHOG_KEY_")
PER_ORG = "VALXEL_GITHUB_TOKEN__"
_SECRET_SHAPE = re.compile(r"_(MCP_AUTH|API_KEY|CLIENT_SECRET|PASSWORD)$")


def is_secret(name: str) -> bool:
    return (name in SECRET_NAMES or name.startswith(PER_PRODUCT) or name.startswith(PER_ORG)
            or bool(_SECRET_SHAPE.search(name)))


class SecretStore(Protocol):
    def versions(self, name: str) -> list[str]: ...  # [current] or [current, previous]; [] if unset


class EnvStore:
    def __init__(self, env: Mapping[str, str]) -> None:
        self.env = env

    def versions(self, name: str) -> list[str]:
        return [self.env.get(name, ""), self.env.get(f"{name}_PREVIOUS", "")]


class FileStore:
    def __init__(self, folder: str | Path) -> None:
        self.folder = Path(folder)

    def versions(self, name: str) -> list[str]:
        return [_read(self.folder / name), _read(self.folder / f"{name}.previous")]


def secret_id(name: str, prefix: str = "valxel") -> str:
    """Where a secret lives in a path-based store: scoped by product or org where it belongs to one."""
    for p in PER_PRODUCT:
        if name.startswith(p):
            return f"{prefix}/products/{name[len(p):].lower()}/{name}"
    if name.startswith(PER_ORG):
        return f"{prefix}/orgs/{name[len(PER_ORG):].lower()}/{name}"
    return f"{prefix}/platform/{name}"


class AwsSecretsManagerStore:
    """AWS Secrets Manager: AWSCURRENT and AWSPREVIOUS of `secret_id(name)`, cached for `ttl` seconds.
    Credentials come from the process's AWS role, never from a secret."""

    def __init__(self, *, prefix: str = "valxel", client=None, region: str = "", ttl: float = 60.0,
                 clock: Callable[[], float] = time.monotonic) -> None:
        if client is None:
            import boto3  # lazy: pip install .[aws]

            client = boto3.client("secretsmanager", **({"region_name": region} if region else {}))
        self.client, self.prefix, self.ttl, self.clock = client, prefix, ttl, clock
        self._cache: dict[str, tuple[float, list[str]]] = {}
        self._lock = threading.Lock()

    def _get(self, sid: str, stage: str) -> str:
        try:
            got = self.client.get_secret_value(SecretId=sid, VersionStage=stage)
        except Exception as exc:  # botocore ClientError: not found, or no such stage
            code = (getattr(exc, "response", None) or {}).get("Error", {}).get("Code", "")
            if code in ("ResourceNotFoundException", "InvalidRequestException"):
                return ""
            raise
        return str(got.get("SecretString") or "")

    def versions(self, name: str) -> list[str]:
        now = self.clock()
        with self._lock:
            hit = self._cache.get(name)
            if hit and hit[0] > now:
                return list(hit[1])
        sid = secret_id(name, self.prefix)
        current = self._get(sid, "AWSCURRENT")
        found = [current, self._get(sid, "AWSPREVIOUS") if current else ""]
        with self._lock:
            self._cache[name] = (now + self.ttl, found)
        return found


STORES = ("env", "files", "aws")
_aws: dict[tuple[str, str], AwsSecretsManagerStore] = {}


def store_kind(env: Mapping[str, str] | None = None) -> str:
    env = env if env is not None else os.environ
    kind = (env.get("VALXEL_SECRET_STORE", "") or ("files" if env.get("VALXEL_SECRETS_DIR") else "env")).lower()
    if kind not in STORES:
        raise ValueError(f"VALXEL_SECRET_STORE is one of {', '.join(STORES)}, not {kind!r}")
    return kind


def store_for(env: Mapping[str, str] | None = None) -> SecretStore:
    env = env if env is not None else os.environ
    kind = store_kind(env)
    if kind == "files":
        return FileStore(env.get("VALXEL_SECRETS_DIR", "") or "/run/secrets/valxel")
    if kind == "aws":
        key = (env.get("VALXEL_SECRET_PREFIX", "") or "valxel", env.get("AWS_REGION", ""))
        if key not in _aws:
            _aws[key] = AwsSecretsManagerStore(prefix=key[0], region=key[1])
        return _aws[key]
    return EnvStore(env)


class SecretEnv(Mapping):
    """os.environ, except that secret names resolve through the secret store."""

    def __init__(self, base: Mapping[str, str] | None = None) -> None:
        self.base = base if base is not None else os.environ

    def __getitem__(self, key: str) -> str:
        if is_secret(key):
            value = current(key, self.base)
            if not value:
                raise KeyError(key)
            return value
        return self.base[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self.base)

    def __len__(self) -> int:
        return len(self.base)


def environ(base: Mapping[str, str] | None = None) -> SecretEnv:
    return SecretEnv(base)


def get(name: str, default: str = "", env: Mapping[str, str] | None = None) -> str:
    """A secret's current value through the store, whatever its name (e.g. one a config file names)."""
    return current(name, env) or default


def in_environment(env: Mapping[str, str]) -> list[str]:
    """Secret names set in the environment, which a secret store should hold instead."""
    return sorted(k for k, v in env.items() if v and (is_secret(k) or is_secret(k.removesuffix("_PREVIOUS"))))


_cache: dict[Path, tuple[int, str]] = {}
_lock = threading.Lock()


def _read(path: Path) -> str:
    """A secret file's value, re-read only when the file changes."""
    try:
        mtime = path.stat().st_mtime_ns
    except FileNotFoundError:
        return ""
    with _lock:
        hit = _cache.get(path)
        if hit and hit[0] == mtime:
            return hit[1]
    value = path.read_text().strip()
    with _lock:
        _cache[path] = (mtime, value)
    return value


def values(name: str, env: Mapping[str, str] | None = None) -> list[str]:
    """The secret's values, current first: [current] or [current, previous]; [] if unset."""
    out: list[str] = []
    for v in store_for(env).versions(name):
        v = (v or "").strip()
        if v and v not in out:
            out.append(v)
    return out


def current(name: str, env=None) -> str:
    vals = values(name, env)
    return vals[0] if vals else ""


def any_matches(name: str, check: Callable[[str], bool], env=None) -> bool:
    """Whether any of the secret's values passes `check` (each compared in constant time by it)."""
    return any(check(v) for v in values(name, env))


def bearer_ok(name: str, header: str, env=None) -> bool:
    """An Authorization header carrying any of the secret's values as a bearer token."""
    return any_matches(name, lambda v: hmac.compare_digest(header or "", f"Bearer {v}"), env)


def weak(secret: str, *, min_length: int = 32) -> str:
    """Why `secret` can't protect anything, or "" when it can."""
    s = secret or ""
    if s in PUBLISHED or "change-me" in s.lower() or "changeme" in s.lower():
        return "an example value published in this repository"
    if len(s) < min_length:
        return f"shorter than {min_length} characters"
    if len(set(s)) < 10:
        return "too few distinct characters to be random"
    return ""


def weak_password(password: str) -> str:
    """Why a database password is unfit outside a laptop, or ""."""
    if password.lower() in WEAK_PASSWORDS:
        return "a default or example password"
    if len(password) < 16:
        return "shorter than 16 characters"
    return ""

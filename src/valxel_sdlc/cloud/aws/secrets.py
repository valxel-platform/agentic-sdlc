"""AWS Secrets Manager: the tool's secrets (secret_ring) and tool connection tokens (oauth_connections)."""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable

from ...secret_ring import secret_id


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
        # [] when unset, [current] with no previous version (the SecretStore port, conformance).
        found = [v for v in (current, self._get(sid, "AWSPREVIOUS") if current else "") if v]
        with self._lock:
            self._cache[name] = (now + self.ttl, found)
        return found


class AwsVault:
    """AWS Secrets Manager. Credentials come from the process's role; a deleted connection's secret
    goes after a 7-day recovery window."""

    def __init__(self, client=None, region: str = "") -> None:
        if client is None:
            import boto3  # the 'aws' extra

            client = boto3.client("secretsmanager", **({"region_name": region} if region else {}))
        self.client = client

    @staticmethod
    def _code(exc: Exception) -> str:
        return (getattr(exc, "response", None) or {}).get("Error", {}).get("Code", "")

    def get(self, path: str) -> dict | None:
        try:
            got = self.client.get_secret_value(SecretId=path)
        except Exception as exc:
            if self._code(exc) in ("ResourceNotFoundException", "InvalidRequestException"):
                return None
            raise
        return json.loads(got.get("SecretString") or "null")

    def put(self, path: str, value: dict) -> None:
        body = json.dumps(value)
        try:
            self.client.put_secret_value(SecretId=path, SecretString=body)
        except Exception as exc:
            if self._code(exc) != "ResourceNotFoundException":
                raise
            self.client.create_secret(Name=path, SecretString=body,
                                      Tags=[{"Key": "valxel:kind", "Value": "connection-token"}])

    def delete(self, path: str) -> None:
        try:
            self.client.delete_secret(SecretId=path, RecoveryWindowInDays=7)
        except Exception as exc:
            if self._code(exc) != "ResourceNotFoundException":
                raise

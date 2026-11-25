"""Azure for the release rehearsal: a Flexible Server point-in-time restore, and its report in Blob
Storage.

The copy is restored from the source server as of a few minutes ago, in its resource group and
region; a server in a virtual network is restored into the same delegated subnet and private DNS
zone. The ARM API is called directly (REST) with the process's own credentials (AKS Workload
Identity): the 'azure' extra's azure-identity, no management library.

`source` is the source server's resource id
(/subscriptions/<id>/resourceGroups/<rg>/providers/Microsoft.DBforPostgreSQL/flexibleServers/<name>);
the copy's `name` is a server name in the same resource group."""

from __future__ import annotations

import json
import secrets
import time
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen

ARM = "https://management.azure.com"
API = "2024-08-01"


def _token() -> str:
    from . import credential

    return str(credential().get_token("https://management.azure.com/.default").token)


class FlexibleServerCopies:
    def __init__(self, source_id: str = "", *, call=None, token=_token, sleep=time.sleep, now=None) -> None:
        self.source_id, self._call, self.token, self.sleep = source_id, call, token, sleep
        self.now = now or (lambda: datetime.now(UTC))

    def call(self, method: str, path: str, body: dict | None = None) -> dict:
        if self._call is not None:
            return self._call(method, path, body)
        req = Request(f"{ARM}{path}?api-version={API}", method=method,  # noqa: S310 - fixed host
                      data=json.dumps(body).encode() if body is not None else None,
                      headers={"Authorization": f"Bearer {self.token()}", "Content-Type": "application/json"})
        with urlopen(req, timeout=30) as resp:  # noqa: S310 - fixed host
            raw = resp.read()
            return json.loads(raw) if raw else {}

    @staticmethod
    def _sibling(source: str, name: str) -> str:
        return source.rsplit("/", 1)[0] + "/" + name

    def _ready(self, server: str) -> dict:
        for _ in range(240):  # two hours
            got = self.call("GET", server)
            if (got.get("properties") or {}).get("state") == "Ready":
                return got
            self.sleep(30)
        raise TimeoutError(f"{server} wasn't ready within two hours")

    def copy(self, source: str, name: str) -> dict:
        src = self.call("GET", source)
        server = self._sibling(source, name)
        when = (self.now() - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.call("PUT", server, {"location": src["location"], "tags": {"purpose": "release-rehearsal"},
                                  "properties": {"createMode": "PointInTimeRestore",
                                                 "sourceServerResourceId": source, "pointInTimeUTC": when}})
        got = self._ready(server)
        return {"host": got["properties"]["fullyQualifiedDomainName"], "port": 5432,
                "user": src["properties"]["administratorLogin"]}

    def set_password(self, name: str, user: str) -> str:
        server = self._sibling(self.source_id, name)
        password = secrets.token_urlsafe(24)
        self.call("PATCH", server, {"properties": {"administratorLoginPassword": password}})
        self.sleep(10)
        self._ready(server)
        return password

    def delete(self, name: str) -> None:
        try:
            self.call("DELETE", self._sibling(self.source_id, name))
        except HTTPError as exc:
            if exc.code != 404:
                raise


class BlobReports:
    def __init__(self, container_url: str, *, client: Any = None) -> None:
        # https://<account>.blob.core.windows.net/<container>
        self.account_url, _, self.container = container_url.rstrip("/").rpartition("/")
        if not self.account_url.startswith("https://") or not self.container:
            raise ValueError(f"On Azure a report bucket is https://<account>.blob.core.windows.net/<container>, "
                             f"not {container_url!r}")
        self._client = client

    @property
    def client(self) -> Any:
        if self._client is None:
            from azure.storage.blob import BlobServiceClient  # the 'azure' extra

            from . import credential

            self._client = BlobServiceClient(self.account_url, credential=credential())
        return self._client

    def put(self, key: str, data: bytes, content_type: str = "application/json") -> None:
        from azure.storage.blob import ContentSettings  # the 'azure' extra

        self.client.get_blob_client(self.container, key).upload_blob(
            data, overwrite=True, content_settings=ContentSettings(content_type=content_type))

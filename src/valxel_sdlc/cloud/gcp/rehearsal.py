"""Google Cloud for the release rehearsal: a Cloud SQL clone, and its report in Cloud Storage.

A clone is Cloud SQL's copy of an instance as it is now, in the source's project, region and
network (private IP), with its settings; deletion protection is turned off on the copy so the
rehearsal can delete it. The Cloud SQL Admin API is called directly (REST), with the process's own
credentials (GKE Workload Identity): the 'gcp' extra's google-auth, no client library."""

from __future__ import annotations

import json
import secrets
import time
from typing import Any
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

API = "https://sqladmin.googleapis.com/v1"


def _token() -> str:
    import google.auth
    import google.auth.transport.requests

    creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/sqlservice.admin"])
    creds.refresh(google.auth.transport.requests.Request())  # type: ignore[no-untyped-call]
    return str(creds.token)


class CloudSqlCopies:
    def __init__(self, project: str, *, call=None, token=_token, sleep=time.sleep) -> None:
        self.project, self._call, self.token, self.sleep = project, call, token, sleep

    def call(self, method: str, path: str, body: dict | None = None) -> dict:
        if self._call is not None:
            return self._call(method, path, body)
        req = Request(f"{API}/projects/{quote(self.project)}{path}", method=method,  # noqa: S310 - fixed host
                      data=json.dumps(body).encode() if body is not None else None,
                      headers={"Authorization": f"Bearer {self.token()}", "Content-Type": "application/json"})
        with urlopen(req, timeout=30) as resp:  # noqa: S310 - fixed host
            raw = resp.read()
            return json.loads(raw) if raw else {}

    def _done(self, op: dict) -> None:
        name = op.get("name", "")
        for _ in range(240):  # two hours
            got = self.call("GET", f"/operations/{quote(name)}") if name else op
            if got.get("status") == "DONE":
                if got.get("error"):
                    raise RuntimeError(f"Cloud SQL operation {name} failed: {got['error']}")
                return
            self.sleep(30)
        raise TimeoutError(f"Cloud SQL operation {name} didn't finish within two hours")

    def copy(self, source: str, name: str) -> dict:
        self._done(self.call("POST", f"/instances/{quote(source)}/clone",
                             {"cloneContext": {"destinationInstanceName": name}}))
        self._done(self.call("PATCH", f"/instances/{quote(name)}", {
            "settings": {"deletionProtectionEnabled": False, "userLabels": {"purpose": "release-rehearsal"}}}))
        inst = self.call("GET", f"/instances/{quote(name)}")
        private = [a["ipAddress"] for a in inst.get("ipAddresses", []) if a.get("type") == "PRIVATE"]
        if not private:
            raise RuntimeError(f"{name} has no private IP: the rehearsal reaches its copy only inside the network")
        return {"host": private[0], "port": 5432, "user": "postgres"}

    def set_password(self, name: str, user: str) -> str:
        password = secrets.token_urlsafe(24)
        self._done(self.call("PUT", f"/instances/{quote(name)}/users?name={quote(user)}",
                             {"name": user, "password": password}))
        return password

    def delete(self, name: str) -> None:
        try:
            self._done(self.call("DELETE", f"/instances/{quote(name)}"))
        except HTTPError as exc:
            if exc.code != 404:
                raise


class GcsReports:
    def __init__(self, bucket: str, *, client: Any = None) -> None:
        self.bucket, self._client = bucket.removeprefix("gs://"), client

    @property
    def client(self) -> Any:
        if self._client is None:
            from google.cloud import storage  # the 'gcp' extra

            self._client = storage.Client()
        return self._client

    def put(self, key: str, data: bytes, content_type: str = "application/json") -> None:
        self.client.bucket(self.bucket).blob(key).upload_from_string(data, content_type=content_type)

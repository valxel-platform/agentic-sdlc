"""Feature flags. A feature gets its flag when its design is approved (G4): created off in
production and on in the environments before it, so testers see the feature and users don't.
G6 turns it on, or a launch turns on a group of flags together; on-call can turn one off.

Each product runs its own Flipt (v2) in its own runtime and names it in product.yaml
(`tools.flags`). The product's code reads flags through OpenFeature with the OFREP provider
(the templates set this up); the pipeline changes them through Flipt's environments API.

A product without a flag service still ships: its features get no flag, launch-group members
wait after staging until the launch is approved (like a train), and incidents roll back instead
of turning a flag off. With stub delivery, products without Flipt get in-memory flags.
"""

from __future__ import annotations

import json
import os
import re
import threading
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable, Protocol


def flag_key(product_id: str, feature_id: str, title: str) -> str:
    """e.g. cln.cln-feat-3.check-in-kiosk. Flipt keys allow letters, digits, '-', '_' and '.'."""
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:40].strip("-")
    return f"{product_id.lower()}.{feature_id.lower()}.{slug}"


class FlagsUnavailable(RuntimeError):
    """The product has no flag service."""


class FlagService(Protocol):
    def available(self, product_id: str) -> bool: ...
    def ensure(self, product_id: str, key: str, *, name: str, description: str, actor: str) -> None: ...
    def set(self, product_id: str, key: str, on: bool, *, actor: str) -> None: ...  # off also ends a rollout
    def get(self, product_id: str, key: str) -> bool | None: ...  # on for anyone
    # Gradual release: on for this share of users (by the OpenFeature targeting key); 100 is on for all.
    def set_percent(self, product_id: str, key: str, percent: float, *, actor: str) -> None: ...
    def percent(self, product_id: str, key: str) -> float | None: ...


class StubFlags:
    """In-memory flags for stub delivery and tests."""

    def __init__(self) -> None:
        self.flags: dict[tuple[str, str], bool] = {}
        self.percents: dict[tuple[str, str], float] = {}
        self.created: dict[tuple[str, str], dict] = {}
        self.log: list[dict] = []
        self._lock = threading.Lock()

    def available(self, product_id) -> bool:
        return True

    def ensure(self, product_id, key, *, name, description, actor) -> None:
        with self._lock:
            if (product_id, key) not in self.created:
                self.created[(product_id, key)] = {"name": name, "description": description, "actor": actor}
                self.flags.setdefault((product_id, key), False)

    def set(self, product_id, key, on, *, actor) -> None:
        self.set_percent(product_id, key, 100.0 if on else 0.0, actor=actor)

    def get(self, product_id, key):
        return self.flags.get((product_id, key))

    def set_percent(self, product_id, key, percent, *, actor) -> None:
        with self._lock:
            pct = max(0.0, min(100.0, float(percent)))
            self.percents[(product_id, key)] = pct
            self.flags[(product_id, key)] = pct > 0
            self.log.append({"product_id": product_id, "key": key, "on": pct > 0, "percent": pct, "actor": actor})

    def percent(self, product_id, key):
        if (product_id, key) not in self.flags:
            return None
        return self.percents.get((product_id, key), 100.0 if self.flags[(product_id, key)] else 0.0)


class NoFlags:
    """A product with no flag service."""

    def available(self, product_id) -> bool:
        return False

    def ensure(self, product_id, key, **_):
        raise FlagsUnavailable(f"{product_id} has no flag service")

    def set(self, product_id, key, on, **_):
        raise FlagsUnavailable(f"{product_id} has no flag service")

    def get(self, product_id, key):
        return None

    def set_percent(self, product_id, key, percent, **_):
        raise FlagsUnavailable(f"{product_id} has no flag service")

    def percent(self, product_id, key):
        return None


# --- Flipt v2 -------------------------------------------------------------------------------

FLAG_TYPE = "flipt.core.Flag"
Transport = Callable[[str, str, dict | None], tuple[int, dict]]


class FliptError(RuntimeError):
    pass


def urllib_transport(base_url: str, token: str, timeout: float = 10.0) -> Transport:
    def call(method: str, path: str, body: dict | None) -> tuple[int, dict]:
        req = urllib.request.Request(base_url + path, method=method,
                                     data=json.dumps(body).encode() if body is not None else None)
        req.add_header("Accept", "application/json")
        if body is not None:
            req.add_header("Content-Type", "application/json")
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
                return resp.status, (json.loads(raw) if raw else {})
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                return e.code, json.loads(raw) if raw else {}
            except ValueError:
                return e.code, {"message": raw.decode(errors="replace")[:300]}
    return call


class FliptFlags:
    """One product's flags in its Flipt v2 server, through the environments API:

    GET  /api/v2/environments/{env}/namespaces                         -> {items, revision}
    POST /api/v2/environments/{env}/namespaces                         {key, name, revision}
    GET  /api/v2/environments/{env}/namespaces/{ns}/resources/flipt.core.Flag/{key}
                                                                       -> {resource: {payload}, revision}
    POST /api/v2/environments/{env}/namespaces/{ns}/resources          {key, revision, payload}  (create)
    PUT  /api/v2/environments/{env}/namespaces/{ns}/resources          {key, revision, payload}  (update)

    Writes carry the revision they read (optimistic concurrency); a conflicting write is retried
    once with a fresh revision. Flags are boolean flags whose `enabled` is the value users get.
    """

    def __init__(self, transport: Transport, *, namespace: str, environments: dict[str, str],
                 production: str = "production") -> None:
        self.http = transport
        self.namespace = namespace
        self.environments = dict(environments)  # runtime environment -> Flipt environment
        self.production = self.environments.get(production) or production

    def _call(self, method: str, path: str, body: dict | None = None, *, ok=(200,)) -> tuple[int, dict]:
        status, data = self.http(method, path, body)
        if status not in ok:
            raise FliptError(f"Flipt {method} {path} answered {status}: {data.get('message') or data}")
        return status, data

    def _ns(self, env: str) -> str:
        return f"/api/v2/environments/{urllib.parse.quote(env)}/namespaces"

    def _flag(self, env: str, key: str) -> tuple[dict | None, str]:
        status, data = self._call("GET", f"{self._ns(env)}/{self.namespace}/resources/{FLAG_TYPE}/{key}",
                                  ok=(200, 404))
        if status == 404:
            return None, ""
        return (data.get("resource") or {}).get("payload") or {}, data.get("revision", "")

    def _namespace_revision(self, env: str) -> str:
        """The environment's revision, creating the product's namespace if it isn't there yet."""
        _, data = self._call("GET", self._ns(env))
        if not any(n.get("key") == self.namespace for n in data.get("items") or []):
            _, made = self._call("POST", self._ns(env), {"key": self.namespace, "name": self.namespace,
                                                         "revision": data.get("revision", "")})
            return made.get("revision", "")
        return data.get("revision", "")

    def _write(self, env: str, method: str, key: str, payload: dict, revision: str) -> None:
        body = {"key": key, "revision": revision, "payload": {"@type": FLAG_TYPE, **payload}}
        status, data = self.http(method, f"{self._ns(env)}/{self.namespace}/resources", body)
        if status == 200:
            return
        if status in (409, 412) or "revision" in str(data.get("message", "")).lower():
            # Someone else changed the environment since we read it: read again and retry once.
            revision = self._flag(env, key)[1] if method == "PUT" else self._namespace_revision(env)
            body["revision"] = revision
            self._call(method, f"{self._ns(env)}/{self.namespace}/resources", body)
            return
        raise FliptError(f"Flipt {method} flag {key} in {env} answered {status}: {data.get('message') or data}")

    def ensure(self, key: str, *, name: str, description: str, actor: str) -> None:
        """Create the flag where it's missing: off in production, on before it. Existing flags are
        left as they are, so retries never turn a flag back off."""
        for runtime_env, env in self.environments.items():
            current, _ = self._flag(env, key)
            if current is not None:
                continue
            on = env != self.production
            payload = {"key": key, "name": name[:100], "description": description[:500],
                       "type": "BOOLEAN_FLAG_TYPE", "enabled": on,
                       "metadata": {"created_by": "valxel-sdlc", "approved_by": actor}}
            self._write(env, "POST", key, payload, self._namespace_revision(env))

    def set(self, key: str, on: bool, *, actor: str, env: str | None = None) -> None:
        """On or off for everyone. Off also removes a gradual release's share: a flag turned off
        in an incident must be off for every user."""
        self.set_percent(key, 100.0 if on else 0.0, actor=actor, env=env)

    def set_percent(self, key: str, percent: float, *, actor: str, env: str | None = None) -> None:
        """On for `percent` of users: a threshold rollout (value true) over a flag that is off,
        so the rest get off. 100 is plain on, 0 plain off, both with no rollouts."""
        env = env or self.production
        current, revision = self._flag(env, key)
        if current is None:
            raise FliptError(f"flag {key} doesn't exist in Flipt environment {env}")
        pct = max(0.0, min(100.0, float(percent)))
        enabled, rollouts = pct >= 100.0, ([] if pct in (0.0, 100.0) else [
            {"type": "THRESHOLD_ROLLOUT_TYPE", "description": "valxel gradual release",
             "threshold": {"percentage": pct, "value": True}}])
        if bool(current.get("enabled", False)) == enabled and (current.get("rollouts") or []) == rollouts:
            return
        meta = {**(current.get("metadata") or {}), "changed_by": actor}
        self._write(env, "PUT", key, {**current, "enabled": enabled, "rollouts": rollouts, "metadata": meta}, revision)

    def get(self, key: str, env: str | None = None) -> bool | None:
        pct = self.percent(key, env)
        return None if pct is None else pct > 0

    def percent(self, key: str, env: str | None = None) -> float | None:
        current, _ = self._flag(env or self.production, key)
        if current is None:
            return None
        if current.get("enabled"):
            return 100.0
        for r in current.get("rollouts") or []:
            th = (r or {}).get("threshold") or {}
            if th.get("value") is True:
                return float(th.get("percentage") or 0.0)
        return 0.0


class ProductFlags:
    """Routes each product to its own flag service, from its product.yaml (`tools.flags`).

    Products with Flipt configured and a token use it. Others get in-memory flags with stub
    delivery, and no flags with live delivery (a pretend flag in production is worse than none).
    """

    def __init__(self, product_flags: Callable[[str], tuple[dict, dict] | None], *, live: bool,
                 env: dict | None = None, transport_for: Callable[[str, str], Transport] | None = None,
                 token_prefix: str = "VALXEL_FLIPT_TOKEN_") -> None:
        self._product_flags = product_flags  # product id -> (tools.flags, runtime) or None
        self.live = live
        self.env = env if env is not None else os.environ
        self.transport_for = transport_for or urllib_transport
        self.token_prefix = token_prefix
        self.stub = StubFlags()
        self._none = NoFlags()
        self._cache: dict[str, tuple[tuple, object]] = {}
        self._lock = threading.Lock()

    def for_product(self, product_id: str):
        found = self._product_flags(product_id)
        spec, runtime = found if found else ({}, {})
        token = self.env.get(f"{self.token_prefix}{product_id.upper()}", "")
        if spec.get("provider") == "flipt" and token:
            sig = (spec["url"], spec["namespace"], tuple(sorted(spec["environments"].items())), token)
            with self._lock:
                hit = self._cache.get(product_id)
                if hit and hit[0] == sig:
                    return hit[1]
                pipeline = list((runtime or {}).get("pipeline") or [])
                svc = FliptFlags(self.transport_for(spec["url"], token), namespace=spec["namespace"],
                                 environments=spec["environments"],
                                 production=pipeline[-1] if pipeline else "production")
                self._cache[product_id] = (sig, svc)
                return svc
        return self._none if self.live else self.stub

    def describe(self, product_id: str) -> str:
        svc = self.for_product(product_id)
        return "flipt" if isinstance(svc, FliptFlags) else "none" if isinstance(svc, NoFlags) else "stub"

    def available(self, product_id: str) -> bool:
        svc = self.for_product(product_id)
        return svc.available(product_id) if not isinstance(svc, FliptFlags) else True

    def ensure(self, product_id, key, *, name, description, actor) -> None:
        svc = self.for_product(product_id)
        if isinstance(svc, FliptFlags):
            svc.ensure(key, name=name, description=description, actor=actor)
        else:
            svc.ensure(product_id, key, name=name, description=description, actor=actor)

    def set(self, product_id, key, on, *, actor) -> None:
        svc = self.for_product(product_id)
        if isinstance(svc, FliptFlags):
            svc.set(key, on, actor=actor)
        else:
            svc.set(product_id, key, on, actor=actor)

    def get(self, product_id, key):
        svc = self.for_product(product_id)
        try:
            return svc.get(key) if isinstance(svc, FliptFlags) else svc.get(product_id, key)
        except (FliptError, OSError):
            return None  # views fall back to what the pipeline last set

    def set_percent(self, product_id, key, percent, *, actor) -> None:
        svc = self.for_product(product_id)
        if isinstance(svc, FliptFlags):
            svc.set_percent(key, percent, actor=actor)
        else:
            svc.set_percent(product_id, key, percent, actor=actor)

    def percent(self, product_id, key):
        svc = self.for_product(product_id)
        try:
            return svc.percent(key) if isinstance(svc, FliptFlags) else svc.percent(product_id, key)
        except (FliptError, OSError):
            return None

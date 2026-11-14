"""Repo provisioning in its own worker, with its own GitHub App (security S4).

Creating a repo and applying its settings (rulesets, security features, team access, custom
properties) needs a GitHub App with `administration` rights. A GitHub App's permissions belong to
the App, so any process that can sign for it can mint an admin token. Two Apps keep those rights
out of the internet-facing process:

    Delivery App      the API process: contents, pull requests, statuses, alerts. No administration.
    Provisioning App  only `valxel provisioner`, a separate worker with its own AWS role; its KMS key
                      lets only that role sign (valxel_sdlc.signing).

With VALXEL_PROVISIONING=queue, the API never creates repos: the provision step and inception's
governance repo put a request on this queue, and the pipeline waits ("provisioning") until the
worker has run it. The worker trusts governance, not the queue (`check`): a repo request must be
the plan G4 approved for that feature, recomputed from governance; a governance repo request must
be the repo the registry names for that product. So a compromised API can at most ask for what
was already approved.
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from datetime import UTC, datetime
from typing import Protocol

WAIT = "provisioning"


def split(env=None) -> bool:
    """Whether provisioning runs in the worker rather than in this process."""
    env = env if env is not None else os.environ
    return (env.get("VALXEL_PROVISIONING", "") or "inline").lower() == "queue"


def _now() -> str:
    return datetime.now(UTC).isoformat()


class ProvisionQueue(Protocol):
    def submit(self, *, kind: str, key: str, payload: dict) -> dict: ...  # the request (existing one if `key` seen)
    def get(self, request_id: str) -> dict | None: ...
    def claim(self) -> dict | None: ...  # the oldest queued request, now running
    def finish(self, request_id: str, *, status: str, result: dict) -> None: ...  # status: done | refused | failed


class InMemoryProvisionQueue:
    def __init__(self) -> None:
        self._rows: dict[str, dict] = {}
        self._lock = threading.Lock()

    def submit(self, *, kind, key, payload) -> dict:
        with self._lock:
            for r in sorted(self._rows.values(), key=lambda r: r["created"], reverse=True):
                if r["key"] == key:  # one request per key, whatever became of it; a retry uses a new key
                    return dict(r)
            r = {"id": uuid.uuid4().hex, "kind": kind, "key": key, "payload": payload, "status": "queued",
                 "result": None, "created": _now(), "updated": _now()}
            self._rows[r["id"]] = r
            return dict(r)

    def get(self, request_id) -> dict | None:
        with self._lock:
            r = self._rows.get(request_id)
            return dict(r) if r else None

    def claim(self) -> dict | None:
        with self._lock:
            for r in sorted(self._rows.values(), key=lambda r: r["created"]):
                if r["status"] == "queued":
                    r.update(status="running", updated=_now())
                    return dict(r)
        return None

    def finish(self, request_id, *, status, result) -> None:
        with self._lock:
            self._rows[request_id].update(status=status, result=result, updated=_now())


class PostgresProvisionQueue:
    DDL = (
        """CREATE TABLE IF NOT EXISTS valxel_provision_requests (
             id TEXT PRIMARY KEY, kind TEXT NOT NULL, key TEXT NOT NULL, payload JSONB NOT NULL,
             status TEXT NOT NULL, result JSONB, created TIMESTAMPTZ NOT NULL DEFAULT now(),
             updated TIMESTAMPTZ NOT NULL DEFAULT now())""",
        "CREATE INDEX IF NOT EXISTS valxel_provision_requests_key ON valxel_provision_requests (key)",
        ("CREATE INDEX IF NOT EXISTS valxel_provision_requests_queued ON valxel_provision_requests (created) "
         "WHERE status = 'queued'"),
    )

    def __init__(self, pool) -> None:
        self.pool = pool
        with pool.connection() as conn:
            for stmt in self.DDL:
                conn.execute(stmt)

    @staticmethod
    def _row(r) -> dict:
        return {"id": r["id"], "kind": r["kind"], "key": r["key"], "payload": r["payload"], "status": r["status"],
                "result": r["result"], "created": r["created"].isoformat(), "updated": r["updated"].isoformat()}

    def submit(self, *, kind, key, payload) -> dict:
        with self.pool.connection() as conn, conn.transaction():
            conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (key,))
            row = conn.execute("SELECT * FROM valxel_provision_requests WHERE key = %s ORDER BY created DESC LIMIT 1",
                               (key,)).fetchone()
            if row:
                return self._row(row)
            row = conn.execute("INSERT INTO valxel_provision_requests (id, kind, key, payload, status) "
                               "VALUES (%s, %s, %s, %s::jsonb, 'queued') RETURNING *",
                               (uuid.uuid4().hex, kind, key, json.dumps(payload))).fetchone()
            return self._row(row)

    def get(self, request_id) -> dict | None:
        with self.pool.connection() as conn:
            row = conn.execute("SELECT * FROM valxel_provision_requests WHERE id = %s", (request_id,)).fetchone()
        return self._row(row) if row else None

    def claim(self) -> dict | None:
        with self.pool.connection() as conn, conn.transaction():
            row = conn.execute("SELECT * FROM valxel_provision_requests WHERE status = 'queued' ORDER BY created "
                               "LIMIT 1 FOR UPDATE SKIP LOCKED").fetchone()
            if not row:
                return None
            row = conn.execute("UPDATE valxel_provision_requests SET status = 'running', updated = now() "
                               "WHERE id = %s RETURNING *", (row["id"],)).fetchone()
            return self._row(row)

    def finish(self, request_id, *, status, result) -> None:
        with self.pool.connection() as conn:
            conn.execute("UPDATE valxel_provision_requests SET status = %s, result = %s::jsonb, updated = now() "
                         "WHERE id = %s", (status, json.dumps(result), request_id))


class QueuedProvisioner:
    """Stands where RepoProvisioner did in the API process: asks the worker instead of acting.
    `wait` seconds: how long to wait for a fast worker before handing back "queued". One request per
    repo per `attempt`: a refused or failed one comes back as failed steps, and a person's retry
    (the provisioning escalation) asks again with the next attempt."""

    queued = True

    def __init__(self, services, *, wait: float = 20.0, poll: float = 0.5) -> None:
        self.services = services
        self.wait, self.poll = wait, poll

    @property
    def queue(self) -> ProvisionQueue:
        return self.services.provision_queue

    def _await(self, req: dict) -> dict:
        deadline = time.monotonic() + self.wait
        while req["status"] in ("queued", "running") and time.monotonic() < deadline:
            time.sleep(self.poll)
            req = self.queue.get(req["id"]) or req
        return req

    def provision(self, product, p: dict, *, feature_id: str, governance_repo: str, manifest_text: str,
                  attempt: int = 0) -> dict:
        req = self._await(self.queue.submit(
            kind="repo", key=f"repo:{feature_id}:{p['repo']}:{attempt}",
            payload={"product_id": product.product_id, "feature_id": feature_id, "repo": p["repo"], "plan": p,
                     "governance_repo": governance_repo}))
        if req["status"] in ("queued", "running"):
            return {"repo": p["repo"], "status": "queued", "request_id": req["id"], "url": "", "governance_pr": None,
                    "steps": []}
        if req["status"] in ("refused", "failed"):
            res = req["result"] or {}
            return {"repo": p["repo"], "status": "failed", "request_id": req["id"], "url": "", "governance_pr": None,
                    "steps": [{"step": "check" if req["status"] == "refused" else "worker", "ok": False,
                               "error": res.get("why") or res.get("error") or req["status"]}]}
        return {**(req["result"] or {}), "request_id": req["id"]}

    def create_governance_repo(self, *, product_id: str, repo: str, private: bool, description: str,
                               inception_id: str) -> str:
        req = self._await(self.queue.submit(kind="governance_repo", key=f"governance:{product_id}:{repo}",
                                            payload={"product_id": product_id, "repo": repo, "private": private,
                                                     "description": description, "inception_id": inception_id}))
        if req["status"] == "done":
            return (req["result"] or {}).get("result", "created")
        if req["status"] in ("queued", "running"):
            raise TimeoutError(f"the provisioning worker hasn't created {repo} yet (request {req['id']}); "
                               "run setup again once it has")
        raise RuntimeError((req["result"] or {}).get("why") or (req["result"] or {}).get("error") or req["status"])

    def done(self, request_ids: list[str]) -> bool:
        return all((self.queue.get(i) or {}).get("status") in ("done", "refused", "failed") for i in request_ids)


# ------------------------------------------------------------------ the worker

def _g4_approved(state: dict) -> bool:
    return any(d.get("gate") == "g4" and d.get("decision") == "approve" for d in state.get("gate_decisions") or [])


def check(engine, req: dict) -> str:
    """Why the worker refuses a request, or "" when governance backs it."""
    services = engine.services
    p = req["payload"]
    if req["kind"] == "governance_repo":  # a new product: its inception's product gate approved it
        from .inception.setup import ids

        state = engine._state(p.get("inception_id") or "")
        if not state:
            return f"no inception {p.get('inception_id')!r}"
        if not any(d.get("gate") == "product" and d.get("decision") == "approve"
                   for d in state.get("gate_decisions") or []):
            return f"{p['inception_id']} has no approved product gate"
        want = ids(state.get("brief") or "")
        if (want["product_id"], want["governance_repo"]) != (p["product_id"], p["repo"]):
            return f"{p['repo']} isn't the governance repo {p['inception_id']} approved ({want['governance_repo']})"
        return ""
    try:
        entry = services.settings.entry(p["product_id"])
    except Exception:
        return f"{p.get('product_id')!r} isn't a registered product"
    if req["kind"] != "repo":
        return f"unknown request kind {req['kind']!r}"
    from .graph.pipeline import repo_plans

    state = engine._state(p["feature_id"])
    if not state or state.get("product_id") != p["product_id"]:
        return f"no feature {p['feature_id']} in {p['product_id']}"
    if not _g4_approved(state):
        return f"{p['feature_id']} has no approved G4"
    approved = {rp["repo"]: rp for rp in repo_plans(services, state)}
    if p["repo"] not in approved:
        return f"{p['repo']} isn't among the repos G4 approved for {p['feature_id']}"
    if approved[p["repo"]] != p["plan"]:
        return f"the plan for {p['repo']} differs from what governance gives for the approved G4"
    if p["governance_repo"] != entry.governance.full_name:
        return f"{p['governance_repo']} isn't {p['product_id']}'s governance repo"
    return ""


def run_one(engine, queue: ProvisionQueue, provisioner, github) -> dict | None:
    """Claim one request, check it against governance, and run it. None when the queue is empty."""
    req = queue.claim()
    if req is None:
        return None
    try:
        why = check(engine, req)
        if why:
            queue.finish(req["id"], status="refused", result={"why": why})
            return {**req, "status": "refused", "why": why}
        p = req["payload"]
        if req["kind"] == "governance_repo":
            result = {"result": github.create_repo(repo=p["repo"], template=None, private=p["private"],
                                                   description=p["description"])}
        else:
            services = engine.services
            snap = services.snapshot(p["product_id"])
            gov = services.settings.entry(p["product_id"]).governance
            src = services.governance.source
            manifest = src.read(gov.org, gov.repo, "product.yaml", src.resolve(gov.org, gov.repo, gov.ref)) or ""
            result = provisioner.provision(snap.product, p["plan"], feature_id=p["feature_id"],
                                           governance_repo=p["governance_repo"], manifest_text=manifest)
        queue.finish(req["id"], status="done", result=result)
        return {**req, "status": "done", "result": result}
    except Exception as exc:  # the request fails; the worker keeps going
        queue.finish(req["id"], status="failed", result={"error": f"{type(exc).__name__}: {exc}"})
        return {**req, "status": "failed", "error": str(exc)}

"""Change-set environments: test a change set's pull requests together before anything merges.

After CI passes on every pull request, the pipeline creates an environment for the change set
(`cs-<feature id>`) on the data plane of the product's first environment, builds each runtime
repo's pull request head, binds those builds and the feature's managed services there, waits until
they are ready, and runs the product's dev-stage suites and the feature's acceptance tests against
it. Only then does the change set go to G5 or the merge train. A failure goes back to the agents
with what failed, like red CI, so main only ever receives a change set that worked as a whole.

The environment is never promoted from: dev, staging and production still get the merged commits
through the deployment pipeline. It is deleted when the change set merges, and the watcher deletes
any left behind (a stopped feature, a crash) once the feature is no longer active or its time to
live has passed.

  runtime:
    change_sets:
      enabled: true      # default true for products with a runtime
      required: false    # true: people decide when the environment can't be created
      max_open: 5        # change-set environments a product may have at once
      ttl_hours: 48      # deleted after this, even if the feature is still open
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

log = logging.getLogger(__name__)

PREFIX = "cs-"
DEFAULTS = {"enabled": True, "required": False, "max_open": 5, "ttl_hours": 48}
# Repos that run in the product's runtime; libraries, acceptance tests and store apps don't.
RUNTIME_ROLES = ("provider", "bff", "consumer", "event-worker", "mcp-server", "agent")


def config(runtime: dict | None) -> dict:
    raw = (runtime or {}).get("change_sets") or {}
    return {**DEFAULTS, **{k: raw[k] for k in DEFAULTS if k in raw}}


def name_for(feature_id: str) -> str:
    """A DNS label: cs-farm-feat-12."""
    return (PREFIX + feature_id.lower()).replace("_", "-")[:63]


def feature_of(env_name: str) -> str | None:
    """FARM-FEAT-12 back from cs-farm-feat-12 (feature ids are <PRODUCT>-FEAT-<n>)."""
    if not env_name.startswith(PREFIX):
        return None
    return env_name[len(PREFIX):].upper()


def components(state: dict, lanes: dict) -> list[dict]:
    """The change set's runtime repos with an open pull request: what the environment runs."""
    roles = {t["repo"]: t.get("role", "") for t in state.get("tasks") or []}
    heads = {}
    for pr in (state.get("pull_requests") or {}).values():
        if pr.get("state") != "merged" and pr.get("head_sha"):
            heads[pr["repo"]] = pr["head_sha"]
    out = []
    for repo, sha in sorted(heads.items()):
        lane = lanes.get(repo)
        if roles.get(repo) not in RUNTIME_ROLES or lane is None or lane.kind != "product" or not lane.runtime:
            continue
        rt = lane.runtime
        out.append({"repo": repo, "sha": sha, "role": roles[repo],
                    "component": (rt.get("components") or {}).get(repo, repo), "project": rt.get("openchoreo_project"),
                    "namespace": rt.get("namespace"), "like": (rt.get("pipeline") or ["dev"])[0]})
    return out


def acceptance_head(state: dict) -> dict | None:
    """The acceptance repo's pull request head: its tests run against the change set before merge."""
    roles = {t["repo"]: t.get("role", "") for t in state.get("tasks") or []}
    for pr in (state.get("pull_requests") or {}).values():
        if roles.get(pr["repo"]) == "acceptance" and pr.get("head_sha") and pr.get("state") != "merged":
            return {"repo": pr["repo"], "sha": pr["head_sha"]}
    return None


def open_count(services, namespaces) -> int:
    n = 0
    for ns in namespaces:
        n += sum(1 for e in services.openchoreo.list_environments(namespace=ns) if e["name"].startswith(PREFIX))
    return n


def teardown(services, env: dict) -> list[str]:
    """Unbind what the change set ran and delete its environment. Best effort; returns problems."""
    problems = []
    name = env.get("name", "")
    for d in (env.get("deployed") or {}).values():
        if d.get("kind") == "resource":
            continue  # resources go with the environment (their bindings are deleted with it)
        try:
            services.openchoreo.unbind(component=d["component"], env=name, project=d.get("project"),
                                       namespace=d.get("namespace"))
        except Exception as exc:
            problems.append(f"{d['component']}: {exc}")
    for ns in env.get("namespaces") or [None]:
        try:
            services.openchoreo.delete_environment(name=name, namespace=ns)
        except Exception as exc:
            problems.append(f"{name}: {exc}")
    return problems


def sweep(services, is_active, namespaces, *, now=None) -> list[str]:
    """Delete change-set environments whose feature is no longer active or that outlived their
    time to live. `is_active(feature_id) -> (active, ttl_hours)`. Returns what was deleted."""
    now = now or datetime.now(timezone.utc)
    deleted = []
    for ns in namespaces:
        try:
            envs = services.openchoreo.list_environments(namespace=ns)
        except Exception:
            continue
        for e in envs:
            fid = feature_of(e["name"])
            if fid is None:
                continue
            active, ttl = is_active(fid)
            try:
                created = datetime.fromisoformat(e["created"]) if e.get("created") else None
            except ValueError:
                created = None
            expired = created is not None and now - created > timedelta(hours=ttl)
            if active and not expired:
                continue
            try:
                services.openchoreo.delete_environment(name=e["name"], namespace=ns)
                deleted.append(e["name"])
            except Exception:
                log.warning("change-set environment %s not deleted", e["name"], exc_info=True)
    return deleted

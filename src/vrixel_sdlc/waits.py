"""External waits: the pipeline pauses on CI, OpenChoreo builds and deployments the way it
pauses on people, with an interrupt. These carry `type: "external"`; no person decides them.
The engine checks them right after a run (stubs resolve at once), on a timer (the watcher),
and when a GitHub webhook says something changed.

  wait       target fields                       done when
  ci         repo, number, sha                   every check on the PR head is green
  build      component, run_id, project, ...     the OpenChoreo WorkflowRun succeeded
  dev        component, env, project, ...        the ReleaseBinding in that env is ready
  staging    (same)
  production (same)
"""

from __future__ import annotations

from datetime import datetime, timezone

LABELS = {"ci": "CI checks", "build": "OpenChoreo builds", "dev": "Deploy to dev",
          "staging": "Deploy to staging", "production": "Deploy to production"}
TIMEOUT_S = {"ci": 3 * 3600, "build": 3600, "dev": 1800, "staging": 1800, "production": 1800}


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


class WaitChecker:
    def __init__(self, services) -> None:
        self.services = services

    def _one(self, wait: str, t: dict) -> dict:
        if wait == "ci":
            res = self.services.github.checks(repo=t["repo"], sha=t["sha"])
            failing = [c["name"] for c in res.get("checks", []) if c["state"] == "failure"]
            return {"state": res["state"], "checks": res.get("checks", []),
                    "message": f"failing: {', '.join(failing)}" if failing else ""}
        oc = self.services.openchoreo
        scope = {"project": t.get("project"), "namespace": t.get("namespace")}
        if wait == "build":
            res = oc.run_status(run_id=t["run_id"], component=t["component"], **scope)
            state = {"succeeded": "success", "failed": "failure"}.get(res["status"], "pending")
            return {"state": state, "message": res.get("message", "")}
        res = oc.deployment(component=t["component"], env=t["env"], **scope)
        state = {"ready": "success", "failed": "failure"}.get(res["state"], "pending")
        return {"state": state, "release": res.get("release", ""), "url": res.get("url", ""),
                "message": "" if state != "failure" else f"{t['component']} failed in {t['env']}"}

    def check(self, packet: dict) -> dict | None:
        """The resume value once the wait is over (success or failure), else None."""
        wait = packet["wait"]
        results = []
        for t in packet.get("targets", []):
            try:
                results.append({**t, **self._one(wait, t)})
            except Exception as exc:  # a flaky API is not a failed build: keep waiting, report it
                results.append({**t, "state": "pending", "message": f"{type(exc).__name__}: {exc}"})
        states = {r["state"] for r in results}
        if "failure" in states:
            failed = [r for r in results if r["state"] == "failure"]
            reason = f"{LABELS.get(wait, wait)} failed: " + "; ".join(
                f"{r.get('repo') or r.get('component')}{': ' + r['message'] if r.get('message') else ''}" for r in failed)
            return {"status": "failure", "wait": wait, "results": results, "reason": reason, "at": now()}
        if "pending" in states:
            since = packet.get("since")
            limit = packet.get("timeout_s") or TIMEOUT_S.get(wait, 3600)
            if since and (datetime.now(timezone.utc) - datetime.fromisoformat(since)).total_seconds() > limit:
                return {"status": "failure", "wait": wait, "results": results, "at": now(),
                        "reason": f"{LABELS.get(wait, wait)} still not done after {int(limit // 60)} minutes"}
            return None
        return {"status": "success", "wait": wait, "results": results, "at": now()}

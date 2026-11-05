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
        self._dispatched: set[tuple] = set()

    def _start_post_deploy(self, packet: dict, results: list[dict]) -> None:
        """Once a stage's deployment is ready, ask each repo to run its post-deploy suite
        (integration in dev, e2e in staging, smoke in production) against it. Once per wait;
        a repeat after a restart is harmless (the workflow cancels duplicates)."""
        q = packet.get("quality") or {}
        shas = {t["repo"]: t.get("sha", "") for t in q.get("targets") or [] if t.get("kind") != "acceptance"}
        urls = {r["repo"].split("/", 1)[-1]: r.get("url", "") for r in results}
        for t in q.get("targets") or []:
            if t.get("kind") != "acceptance":
                continue
            # The product's acceptance run: every component's URL in this environment, the
            # selected tests, and the feature's flag on for the test users.
            from .acceptance import check_name

            key = (packet.get("feature_id"), packet["wait"], packet.get("since"), t["repo"])
            if key in self._dispatched:
                continue
            self._dispatched.add(key)
            try:
                self.services.github.dispatch(repo=t["repo"], event_type="vrixel-acceptance", payload={
                    "stage": packet["wait"], "sha": t["sha"], "feature_id": t["feature_id"],
                    "check": check_name(t["feature_id"], packet["wait"]), "grep": t["grep"],
                    "projects": ",".join(t["projects"]), "flag": t.get("flag", ""),
                    "env": urls, "criteria": ",".join(t.get("acs") or [])})
            except Exception:
                self._dispatched.discard(key)
        for r in results:
            key = (packet.get("feature_id"), packet["wait"], packet.get("since"), r["repo"])
            if key in self._dispatched or not shas.get(r["repo"]):
                continue
            self._dispatched.add(key)
            try:
                self.services.github.dispatch(repo=r["repo"], event_type="vrixel-deployed", payload={
                    "stage": packet["wait"], "sha": shas[r["repo"]], "base_url": r.get("url", ""),
                    "feature_id": packet.get("feature_id", "")})
            except Exception:
                self._dispatched.discard(key)  # try again on the next check

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
        if wait in ("release_ready", "release_train", "incident_recovery"):  # resolved from the engine's own threads
            fn = getattr(self.services, "release_waits", None)
            return fn(packet) if callable(fn) else None
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
            failure = {"status": "failure", "wait": wait, "results": results, "reason": reason, "at": now()}
            if wait == "ci" and packet.get("quality"):
                # A red check may be a scanner or a test suite: let the policy say which, so security
                # findings reach security approvers and test failures go back to the build.
                q = self._quality(packet, {"wait": wait, "results": results, "at": now()})
                if q is not None and q["status"] in ("security", "failure"):
                    return q if q["status"] == "security" or not q.get("reason") else {**failure, **q}
                return {**failure, "quality": (q or {}).get("quality")}
            return failure
        if "pending" in states:
            since = packet.get("since")
            limit = packet.get("timeout_s") or TIMEOUT_S.get(wait, 3600)
            if since and (datetime.now(timezone.utc) - datetime.fromisoformat(since)).total_seconds() > limit:
                return {"status": "failure", "wait": wait, "results": results, "at": now(),
                        "reason": f"{LABELS.get(wait, wait)} still not done after {int(limit // 60)} minutes"}
            return None
        base = {"wait": wait, "results": results, "at": now()}
        if wait in ("dev", "staging", "production"):
            self._start_post_deploy(packet, results)
        return self._quality(packet, base)

    def _quality(self, packet: dict, base: dict) -> dict | None:
        """Once CI, the build or the rollout is done, check the stage's test and security suites.
        Tests that fail send work back; blocking findings go to security approvers."""
        q = packet.get("quality")
        quality = getattr(self.services, "quality", None)
        if not q or not q.get("policy") or quality is None:
            return {"status": "success", **base}
        res = quality.check(q["stage"], q["policy"], q["targets"], q.get("waivers") or [])
        if res["status"] == "pending":
            since = packet.get("since")
            limit = packet.get("timeout_s") or TIMEOUT_S.get(packet["wait"], 3600)
            if since and (datetime.now(timezone.utc) - datetime.fromisoformat(since)).total_seconds() > limit:
                return {"status": "failure", **base, "quality": res,
                        "reason": f"Still waiting for {', '.join(res['pending'])} after {int(limit // 60)} minutes"}
            return None
        if res["status"] == "failed":
            return {"status": "failure", **base, "quality": res, "reason": "; ".join(res["test_failures"])}
        if res["status"] == "blocked":
            n = len(res["blocking"])
            return {"status": "security", **base, "quality": res,
                    "reason": f"{n} security finding{'s' if n != 1 else ''} at or above the blocking severity"}
        return {"status": "success", **base, "quality": res}

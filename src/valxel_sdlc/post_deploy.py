"""After a release is deployed: is it working? (`valxel post-deploy`)

The release rehearsal checked the release against a copy of the tool's database before it merged.
This checks it live, once the rollout is healthy (platform-foundation runs it after Flux's sdlc-tool
Kustomization is ready), for VALXEL_POST_DEPLOY_WINDOW_S (15 minutes):

- every minute, probes that change nothing: the API answers /api/healthz from the new image
  (VALXEL_IMAGE) in the strict mode it's configured for, the watcher's housekeeping isn't stale,
  the web app loads, and every worker pod is healthy on the new image;
- over the window, the tool's own alert thresholds (valxel_sdlc.metrics.findings): the API's 5xx
  ratio, agent sessions that failed, how long the oldest queued run waits, the audit backlog. These
  count only with enough traffic (20 requests, 5 sessions); a quiet window passes on the probes.

It says how it went as a commit status, valxel/post-deploy, on the deployment repository's commit
that deployed the release (VALXEL_RELEASE_REPO). If the check fails, it proposes the revert: a PR
putting back the pins from before (release_proposals.Proposer.revert). It doesn't merge it, like
everything the tool proposes for itself.
"""

from __future__ import annotations

import json
import os
import socket
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

CONTEXT = "valxel/post-deploy"


def _strict() -> bool:
    from . import strict

    return bool(strict.enabled())


@dataclass
class Check:
    api: str = "http://valxel-sdlc:8080"
    workers: str = ""  # host:port of the workers' headless service; '' skips them
    image: str = ""  # what every replica should run
    window_s: float = 900.0
    every_s: float = 60.0
    get: Any = None  # url -> (status, body text): a fake in tests
    resolve: Any = None  # host -> [ip]
    sleep: Any = time.sleep
    clock: Any = time.time
    problems: list[str] = field(default_factory=list)

    @staticmethod
    def from_env(env=None) -> Check:
        env = env if env is not None else os.environ
        return Check(api=env.get("VALXEL_POST_DEPLOY_API", "http://valxel-sdlc:8080").rstrip("/"),
                     workers=env.get("VALXEL_POST_DEPLOY_WORKERS", ""), image=env.get("VALXEL_IMAGE", ""),
                     window_s=float(env.get("VALXEL_POST_DEPLOY_WINDOW_S", "900")))

    def _get(self, url: str) -> tuple[int, str]:
        if self.get is not None:
            return self.get(url)
        try:
            with urllib.request.urlopen(url, timeout=10) as r:  # noqa: S310 - the deployment's own services
                return r.status, r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            return e.code, ""
        except OSError as e:
            return 0, str(e)

    def _json(self, url: str) -> tuple[int, dict]:
        code, text = self._get(url)
        try:
            return code, json.loads(text) if text else {}
        except ValueError:
            return code, {}

    def _ips(self, host: str) -> list[str]:
        if self.resolve is not None:
            return list(self.resolve(host))
        try:
            return sorted({str(a[4][0]) for a in socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)})
        except OSError:
            return []

    def probe(self) -> list[str]:
        """What's wrong right now, changing nothing."""
        out = self._api()
        code, w = self._json(f"{self.api}/api/healthz/watcher")
        if code != 200 or not w.get("ok", True):
            out.append(f"watcher: housekeeping not succeeding ({', '.join(map(str, w.get('stale') or [])) or code})")
        code, _ = self._get(f"{self.api}/")
        if code != 200:
            out.append(f"web: / answered {code}")
        return out + (self._workers() if self.workers else [])

    def _api(self) -> list[str]:
        seen = set()
        for _ in range(4):  # the service picks a replica each time: ask a few
            code, h = self._json(f"{self.api}/api/healthz")
            if code != 200 or not h.get("ok"):
                return [f"api: /api/healthz answered {code}"]
            seen.add(str(h.get("image", "")))
            want = _strict()  # what this process, with the deployment's settings, would run
            if bool(h.get("strict")) != want:
                return [f"api: strict mode is {'on' if h.get('strict') else 'off'}; the deployment sets it "
                        f"{'on' if want else 'off'}"]
        if self.image and seen and seen != {self.image}:
            return [f"api: replicas run {', '.join(sorted(i[-19:] for i in seen))}, not the release's image"]
        return []

    def _workers(self) -> list[str]:
        host, _, port = self.workers.partition(":")
        ips = self._ips(host)
        if not ips:
            return [f"workers: none ready behind {host}"]
        out = []
        for ip in ips:
            where = f"[{ip}]" if ":" in ip else ip  # an IPv6 pod's address goes in brackets
            code, h = self._json(f"http://{where}:{port or 8082}/healthz")
            if code != 200 or not h.get("ok"):
                out.append(f"workers: {ip} answered {code}")
            elif self.image and h.get("image") and h["image"] != self.image:
                out.append(f"workers: {ip} runs {h['image'][-19:]}, not the release's image")
        return out

    def run(self, engine) -> list[str]:
        """Probe every minute through the window, then judge the window on the tool's own alerts.
        Stops early at the first probe that fails twice in a row (one miss can be a restart)."""
        from .metrics import Checker, findings, totals

        checker = Checker(engine, clock=self.clock)
        before = totals(checker.snapshots())
        end, missed = self.clock() + self.window_s, 0
        while True:
            now = self.probe()
            missed = missed + 1 if now else 0
            if missed >= 2:
                return now
            if self.clock() >= end:
                break
            self.sleep(self.every_s)
        if now:
            return now
        queue = getattr(engine, "queue", None)
        audit = getattr(engine.services, "audit", None)
        return findings(totals(checker.snapshots()), before, queue.depth() if queue is not None else None,
                        audit.backlog() if audit is not None else 0)


def main(argv: list[str] | None = None) -> int:
    from .api.app import create_engine_from_env
    from .release_proposals import Proposer
    from .tenancy import PLATFORM, bound

    check = Check.from_env()
    engine, _ = create_engine_from_env()
    proposer = Proposer.from_env(engine.services.github, engine.cluster)
    sha = proposer.deployed() if proposer else ""

    def say(state: str, text: str) -> None:  # on the commit that deployed the release
        if proposer is not None and sha:
            proposer.github.set_status(repo=proposer.repo, sha=sha, context=CONTEXT, state=state, description=text)

    say("pending", f"Watching the release for {check.window_s / 60:.0f} minutes")
    with bound(PLATFORM):
        problems = check.run(engine)
    print(json.dumps({"passed": not problems, "problems": problems, "release": sha}), flush=True)
    if not problems:
        say("success", f"Healthy for {check.window_s / 60:.0f} minutes after deploying")
        return 0
    say("failure", "; ".join(problems)[:140])
    if proposer:
        print(json.dumps({"revert": proposer.revert(problems)}), flush=True)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

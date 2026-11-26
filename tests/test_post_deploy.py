"""After a release is deployed (valxel_sdlc.post_deploy): probes that change nothing, the tool's own
alert thresholds over the window, a status on the release's commit, and the revert it proposes when
the check fails (release_proposals.Proposer.revert)."""

import json
from types import SimpleNamespace

import pytest

from valxel_sdlc import metrics
from valxel_sdlc.cluster import MemoryCluster
from valxel_sdlc.integrations.github import StubGitHub
from valxel_sdlc.post_deploy import Check
from valxel_sdlc.release_proposals import Proposer, pins

NEW, OLD = "ghcr.io/valxel-platform/agentic-sdlc:main@sha256:" + "b" * 64, "ghcr.io/x@sha256:" + "a" * 64
REPO, PATH = "valxel-platform/platform-foundation", "gitops/clusters/tools/releases.yaml"


class Deployment:
    """The tool's services as the check sees them over HTTP."""

    def __init__(self):
        self.api = {"ok": True, "strict": False, "image": NEW}
        self.workers = {"10.0.0.7": {"ok": True, "image": NEW}, "10.0.0.8": {"ok": True, "image": NEW}}
        self.watcher = {"ok": True, "stale": []}
        self.web = 200
        self.calls = []

    def get(self, url):
        self.calls.append(url)
        if url.endswith("/api/healthz"):
            return (200 if self.api.get("ok") else 503), json.dumps(self.api)
        if url.endswith("/api/healthz/watcher"):
            return 200, json.dumps(self.watcher)
        if url.endswith(":8080/"):
            return self.web, "<html>"
        ip = url.split("//")[1].split(":")[0]
        w = self.workers.get(ip)
        return (0, "refused") if w is None else ((200 if w["ok"] else 503), json.dumps(w))


@pytest.fixture
def deployment():
    return Deployment()


@pytest.fixture
def check(deployment, monkeypatch):
    monkeypatch.setattr("valxel_sdlc.post_deploy._strict", lambda: False)
    t = [0.0]
    return Check(api="http://valxel-sdlc:8080", workers="valxel-sdlc-worker:8082", image=NEW, window_s=180,
                 every_s=60, get=deployment.get, resolve=lambda host: list(deployment.workers),
                 sleep=lambda s: t.__setitem__(0, t[0] + s), clock=lambda: t[0])


def test_a_healthy_release_has_nothing_to_say(check):
    assert check.probe() == []


@pytest.mark.parametrize("break_it, says", [
    (lambda d: d.api.update(ok=False), "api: /api/healthz answered 503"),
    (lambda d: d.api.update(strict=True), "api: strict mode is on; the deployment sets it off"),
    (lambda d: d.api.update(image=OLD), "api: replicas run"),
    (lambda d: d.watcher.update(ok=False, stale=["sync_repos"]), "watcher: housekeeping not succeeding (sync_repos)"),
    (lambda d: setattr(d, "web", 404), "web: / answered 404"),
    (lambda d: d.workers["10.0.0.8"].update(ok=False), "workers: 10.0.0.8 answered 503"),
    (lambda d: d.workers["10.0.0.8"].update(image=OLD), "workers: 10.0.0.8 runs"),
    (lambda d: d.workers.clear(), "workers: none ready behind valxel-sdlc-worker"),
])
def test_each_probe_says_what_is_wrong(check, deployment, break_it, says):
    break_it(deployment)
    assert any(p.startswith(says) for p in check.probe())


def test_the_probes_only_read(check, deployment):
    check.probe()
    assert deployment.calls and all("/api/healthz" in u or u.endswith(("/", "/healthz")) for u in deployment.calls)


def engine_with(cluster, queue=None):
    return SimpleNamespace(cluster=cluster, queue=queue, services=SimpleNamespace(audit=None))


def publish(cluster, at, requests, errors):
    counters = {"valxel_http_requests_total": [({"code": "2xx"}, requests - errors), ({"code": "5xx"}, errors)]}
    cluster.put("metrics:api-1", {"at": at, "counters": counters})
    cluster.put("metrics:replicas", {"api-1": at})


def test_the_window_is_judged_on_the_tools_own_alerts(check, monkeypatch):
    cluster = MemoryCluster()
    publish(cluster, 0, 100, 0)
    sleeps = []

    def busy(s):  # the release serves 100 more requests, 30 of them 5xx, during the window
        sleeps.append(s)
        publish(cluster, check.clock() + s, 200, 30)
        check.clock = (lambda t=check.clock() + s: t)
    check.sleep = busy
    found = check.run(engine_with(cluster))
    assert sleeps == [60, 60, 60]  # probed through the whole window
    assert found == ["api: 30 of 100 requests failed with a server error (alert above 5%)"]
    assert metrics.findings  # the same thresholds the watcher alerts on


def test_a_quiet_healthy_window_passes(check):
    assert check.run(engine_with(MemoryCluster())) == []


def test_a_probe_failing_twice_ends_it_early(check, deployment):
    deployment.api.update(ok=False)
    sleeps = []
    check.sleep = sleeps.append
    assert check.run(engine_with(MemoryCluster())) == ["api: /api/healthz answered 503"]
    assert len(sleeps) == 1  # once to see it again; then it stops


# ---------------------------------------------------------------- the revert


def releases(digest):
    return f"data:\n  sdlc_tool_image: ghcr.io/valxel-platform/agentic-sdlc:main@sha256:{digest * 64}\n"


@pytest.fixture
def gh():
    g = StubGitHub()
    g.history = {(REPO, PATH): ["c2", "c1"]}  # c2 deployed the release; c1 is the one before
    g.files[(REPO, "c2", PATH)] = g.files[(REPO, "main", PATH)] = releases("b")
    g.files[(REPO, "c1", PATH)] = releases("a")
    return g


def test_a_failed_check_proposes_going_back(gh):
    p = Proposer(gh, MemoryCluster(), REPO, wait_s=1)
    assert p.deployed() == "c2"
    got = p.revert(["api: 30 of 100 requests failed with a server error (alert above 5%)"])
    [pr] = [x for x in gh.prs.values() if x["branch"] == "release/revert"]
    assert got["reverts"] == "c2" and pr["title"] == "Revert release agentic-sdlc"
    assert "30 of 100 requests failed" in pr["body"] and f"`{'b' * 12}` | `{'a' * 12}`" in pr["body"]
    assert pins(gh.files[(REPO, "release/revert", PATH)])["sdlc_tool_image"]["digest"] == "sha256:" + "a" * 64
    assert gh.files[(REPO, "main", PATH)] == releases("b")  # proposed only


def test_nothing_to_go_back_to(gh):
    gh.history = {(REPO, PATH): ["c2"]}
    assert "no earlier release" in Proposer(gh, MemoryCluster(), REPO, wait_s=1).revert(["x"])["ignored"]

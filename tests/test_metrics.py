"""T-118: Valxel's own health in numbers, across replicas, with alerts."""

import pytest
from fastapi.testclient import TestClient

from valxel_sdlc import metrics as m
from valxel_sdlc.api.app import create_app
from valxel_sdlc.cluster import MemoryCluster
from valxel_sdlc.web.auth import AuthConfig

AUTH = AuthConfig(mode="dev", secret="x" * 40)


def test_prometheus_text_has_counters_histograms_and_gauges():
    r = m.Registry()
    r.inc("valxel_http_requests_total", {"method": "GET", "route": "/api/features", "code": "2xx"})
    r.observe("valxel_http_request_seconds", {"route": "/api/features"}, 0.2)
    text = r.text({"valxel_queue_jobs": [({"state": "queued"}, 3.0)]})
    assert 'valxel_http_requests_total{code="2xx",method="GET",route="/api/features"} 1' in text
    assert 'valxel_http_request_seconds_bucket{le="0.25",route="/api/features"} 1' in text
    assert 'valxel_http_request_seconds_bucket{le="0.1",route="/api/features"} 0' in text
    assert 'valxel_http_request_seconds_count{route="/api/features"} 1' in text
    assert 'valxel_queue_jobs{state="queued"} 3' in text


def test_findings_compare_this_check_with_the_last():
    before = {"requests": 100, "errors": 0, "sessions": 10, "failed": 0}
    now = {"requests": 140, "errors": 5, "sessions": 16, "failed": 3}
    got = m.findings(now, before, {"queued": 4, "running": 1, "oldest_s": 1800}, 2000, env={})
    assert [f.split(":", 1)[0] for f in got] == ["queue", "api", "sessions", "audit"]
    quiet = m.findings({"requests": 110, "errors": 5, "sessions": 12, "failed": 2}, before,
                       {"queued": 0, "running": 0, "oldest_s": 0}, 0, env={})
    assert quiet == []  # too few requests and sessions to judge, an empty queue


def test_sessions_are_counted_by_role_and_outcome():
    from valxel_sdlc.gateway import ProviderUnavailable

    class Inner:
        name, capabilities = "fake", ()

        def __init__(self, err=None):
            self.err = err

        def run(self, task):
            if self.err:
                raise self.err
            return "ok"

    task = type("T", (), {"role": "t118-coder"})()
    assert m.MeteredRuntime(Inner()).run(task) == "ok"
    for err in (ProviderUnavailable("down"), ValueError("x")):
        with pytest.raises(type(err)):
            m.MeteredRuntime(Inner(err)).run(task)
    snap = {tuple(sorted(lab.items())): v for lab, v in m.REGISTRY.snapshot()["valxel_agent_sessions_total"]}
    for outcome in ("success", "unavailable", "failure"):
        assert snap[(("outcome", outcome), ("role", "t118-coder"))] >= 1


def test_the_checker_adds_up_replicas_and_alerts_once_an_hour():
    cluster, clock = MemoryCluster(), [1000.0]
    said = []

    class Engine:
        queue = None
        services = type("S", (), {"audit": None})()

    Engine.cluster = cluster
    c = m.Checker(Engine(), alert=said.append, clock=lambda: clock[0])

    def replica(name, requests, errors):
        cluster.put(f"metrics:{name}", {"at": clock[0], "counters": {"valxel_http_requests_total": [
            [{"code": "2xx"}, requests - errors], [{"code": "5xx"}, errors]]}})
        cluster.put("metrics:replicas", {**(cluster.get("metrics:replicas") or {}), name: clock[0]})

    replica("a", 10, 0)
    replica("b", 10, 0)
    assert c.run() == []
    clock[0] += 300
    replica("a", 40, 6)
    replica("b", 40, 6)  # 60 new requests across both, 12 failed
    assert len(c.run()) == 1 and "12 of 60" in said[0]
    clock[0] += 300
    replica("a", 70, 12)
    replica("b", 70, 12)
    assert c.run() == []  # still bad, said within the hour
    clock[0] += 3600
    replica("a", 100, 18)
    replica("b", 100, 18)
    assert len(c.run()) == 1


def test_publish_names_the_replica_in_the_index():
    cluster = MemoryCluster()
    m.publish(cluster, clock=lambda: 50.0)
    assert m.REPLICA in cluster.get("metrics:replicas") and cluster.get(f"metrics:{m.REPLICA}")["at"] == 50.0


def test_metrics_need_the_token(engine, monkeypatch):
    with TestClient(create_app(engine, auth=AUTH, web_dist=None)) as c:
        assert c.get("/metrics").status_code == 404  # not set up: nothing here
        monkeypatch.setenv("VALXEL_METRICS_TOKEN", "s3cret")
        c.post("/api/auth/dev-login", json={"login": "ann", "email": "a@x.example", "orgs": ["x"]})
        c.get("/api/features")
        assert c.get("/metrics").status_code == 401
        r = c.get("/metrics", headers={"Authorization": "Bearer s3cret"})
    assert r.status_code == 200
    assert 'route="/api/features"' in r.text and "valxel_watcher_job_failing" in r.text


def test_the_engine_checks_its_metrics(engine):
    assert "check_tool_metrics" in {j.name for j in engine.watcher_jobs()}

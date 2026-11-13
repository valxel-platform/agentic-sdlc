"""Traceability: metrics -> goals -> criteria -> tasks -> PRs -> acceptance -> deployments, and back."""

from valxel_sdlc import traceability

PRD = """# PRD: x
## Goals
- G1: Faster check-in (measured by M1)
- G2: Fewer calls
## Acceptance criteria
- AC-1: Given a, when b, then c
- AC-2: Given d, when e, then f
- AC-3: Given g, when h, then i
"""
OPP = "# Brief\n## Success metrics\n- M1: check-in under 2 min\n- M2: unused\n"
HLD = "# HLD\n## ADRs\n- ADR-F-1: Extend the intake API\n## Affected repos\n- o/api (provider)\n- o/web (consumer)\n- o/docs (provider)\n"
PLAN = "# Plan\n## Traceability\n- AC-1 -> TASK-1 / TEST-1\n- AC-2 -> TASK-2 / TEST-2\n"


def state(**over):
    s = {"feature_id": "F-1", "title": "Check-in", "profile": "express",
         "tasks": [{"id": "TASK-1", "repo": "o/api", "acs": ["AC-1"]},
                   {"id": "TASK-2", "repo": "o/web", "acs": ["AC-2"]}],
         "pull_requests": {"o/api#4": {"repo": "o/api", "number": 4, "url": "u4", "task_id": "TASK-1"}},
         "deployments": {"o/api": {"env": "staging", "release": "r1"}, "db": {"kind": "resource", "env": "dev"}},
         "quality": {"staging": {"suites": [{"suite": "acceptance", "details": {"criteria": {"AC-1": "passed",
                                                                                            "AC-2": "failed"}}}]}},
         "change_set": {"merged": True}, "flag": "f.k", "release": {"flag_percent": 25, "production": True}}
    return {**s, **over}


def load(kind):
    return {"OPP": OPP, "PRD": PRD, "HLD": HLD, "PLAN": PLAN}.get(kind, "")


def test_a_feature_traces_from_metrics_to_production():
    t = traceability.build(state(), load, [{"id": "INC-1", "title": "500s", "status": "open", "touch": "same component"}])
    assert t["metrics"][0] == {"id": "M1", "text": "check-in under 2 min", "goals": ["G1"]}
    ac1 = t["criteria"][0]
    assert ac1["tasks"] == ["TASK-1"] and ac1["test"] == "TEST-1" and ac1["repos"] == ["o/api"]
    assert ac1["prs"][0]["url"] == "u4" and ac1["acceptance"] == {"staging": "passed"}
    assert ac1["deployed"] == {"o/api": "staging"}
    assert t["decisions"] == [{"id": "ADR-F-1", "text": "Extend the intake API"}]
    assert [r["repo"] for r in t["repos"]] == ["o/api", "o/web", "o/docs"]
    assert t["release"] == {"flag": "f.k", "percent": 25, "production": True, "merged": True}
    assert t["incidents"][0]["id"] == "INC-1" and t["profile"] == "express"
    assert set(t["gaps"]) == {
        "G2 has no metric that measures it", "M2 measures no goal", "AC-3 is covered by no task",
        "o/docs is affected but no task changes it", "TASK-2 (o/web) has no pull request",
        "AC-2's acceptance test failed in staging", "AC-3 has no acceptance result",
        "The plan lists no test cases",  # an older plan: no test cases, so the matrix can't be complete
    }


def test_back_from_code_to_the_features_that_changed_it():
    rows = [{"feature_id": "F-1", "title": "Check-in", "state": state()},
            {"feature_id": "F-2", "title": "Other", "state": state(tasks=[], pull_requests={})}]
    by_repo = traceability.search(rows, repo="o/api")
    assert [(r["feature_id"], r["criteria"], r["tasks"]) for r in by_repo] == [("F-1", ["AC-1"], ["TASK-1"])]
    by_pr = traceability.search(rows, pr="o/api#4")
    assert by_pr[0]["prs"][0]["number"] == 4 and by_pr[0]["criteria"] == ["AC-1"]
    assert traceability.search(rows, repo="o/none") == []


def test_the_engine_traces_a_released_feature(engine, client=None):
    fid = engine.start(product_id="FARM", title="Field notes")["feature_id"]
    engine.auto_drive(fid)
    t = engine.trace(fid)
    assert t["criteria"] and all(c["tasks"] and c["prs"] for c in t["criteria"])
    assert t["goals"][0]["metrics"] == ["M1", "M2"] and t["metrics"][0]["goals"] == ["G1"]
    assert t["complete"] and t["gaps"] == [] and t["nfrs"] and all(c["cases"] for c in t["criteria"] + t["nfrs"])
    assert any(r["deployed"] for r in t["repos"])
    repo = t["criteria"][0]["repos"][0]
    assert any(f["feature_id"] == fid for f in engine.trace_search("FARM", repo=repo))


def test_the_api_serves_the_trace_and_the_reverse_lookup(engine):
    from fastapi.testclient import TestClient

    from valxel_sdlc.api.app import create_app
    from valxel_sdlc.web.auth import AuthConfig

    fid = engine.start(product_id="FARM", title="Field notes")["feature_id"]
    engine.auto_drive(fid)
    app = create_app(engine, auth=AuthConfig(mode="dev", secret="x" * 40), web_dist=None)
    with TestClient(app) as c:
        assert c.get(f"/api/features/{fid}/trace").status_code == 401
        c.post("/api/auth/dev-login", json={"login": "ana", "teams": [], "orgs": ["acme-farm"]})
        t = c.get(f"/api/features/{fid}/trace").json()
        assert t["feature_id"] == fid and t["criteria"]
        assert c.get("/api/features/FARM-FEAT-999/trace").status_code == 404
        pr = t["criteria"][0]["prs"][0]["key"]
        found = c.get("/api/products/FARM/trace", params={"pr": pr}).json()["features"]
        assert found[0]["feature_id"] == fid
        assert c.get("/api/products/FARM/trace").status_code == 422
        assert c.get("/api/products/NOPE/trace", params={"repo": "a/b"}).status_code == 404
        products = c.get("/api/products").json()
        assert products["FARM"]["profiles"] == ["feature", "express"]
        assert products["CLN"]["profiles"] == ["feature"]

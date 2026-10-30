from fastapi.testclient import TestClient

from vrixel_sdlc.api.app import create_app


def test_intake_inbox_and_decisions_over_http(engine):
    with TestClient(create_app(engine)) as client:
        assert client.get("/healthz").json() == {"ok": True, "mode": "stub"}
        r = client.post("/features", json={"product_id": "AG", "title": "Offline crop log"})
        assert r.status_code == 201
        fid = r.json()["feature_id"]
        assert fid == "AG-FEAT-1"

        inbox = client.get("/inbox").json()
        assert [(i["feature_id"], i["gate"]) for i in inbox] == [(fid, "g1")]

        bad = client.post(f"/features/{fid}/decisions/{inbox[0]['interrupt_id']}",
                          json={"decision": "resolved", "approver": "po"})
        assert bad.status_code == 422

        ok = client.post(f"/features/{fid}/decisions/{inbox[0]['interrupt_id']}",
                         json={"decision": "approve", "approver": "po", "active_minutes": 4})
        assert ok.status_code == 200
        assert [p["gate"] for p in ok.json()["pending"]] == ["g2"]

        usage = client.get(f"/features/{fid}/usage").json()
        assert usage and {"prefix_hashes", "cache_read_input_tokens", "est_cost_usd"} <= set(usage[0])


def test_unknown_product_and_feature_return_404(engine):
    with TestClient(create_app(engine)) as client:
        assert client.post("/features", json={"product_id": "NOPE", "title": "x y z"}).status_code == 404
        assert client.get("/features/AG-FEAT-99").status_code == 404

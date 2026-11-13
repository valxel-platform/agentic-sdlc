"""Fill a local stub-mode server with features at different stages, for UI work and screenshots.

    VALXEL_AUTH=dev VALXEL_SESSION_SECRET=dev uvicorn --factory valxel_sdlc.api.app:create_app --port 8080
    python web/scripts/seed.py [http://localhost:8080]

Signs in as `demo` (a member of every example team), starts a handful of features and approves
some of their gates, so the inbox, board, feature pages, costs and changes all have something in
them. Stub mode only: nothing leaves the machine."""

from __future__ import annotations

import sys
import time

import httpx

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8080").rstrip("/") + "/api"
ORGS = ("acme-clinic", "acme-dental", "acme-farm", "acme-pay", "acme-shared", "acme-shop", "valxel-tools")
TEAMS = ("architects", "design", "product-owners", "qa", "release-managers", "security", "tech-leads",
         "clinical-reviewers", "platform-owners", "platform")

FEATURES = [
    # product, title, description, gates to approve before stopping, faults
    ("FARM", "Offline crop log", "Field workers log crop observations without signal; sync when back online.", 99, {}),
    ("FARM", "Irrigation alerts by SMS", "Text the farm manager when soil moisture drops below the field's threshold.", 3, {}),
    ("SHOP", "Split payments at checkout", "Let a customer pay one order with two cards.", 1, {"spec": 1}),
    ("CLN", "Patient intake on tablets", "Patients fill the intake form on a clinic tablet; staff review before it's saved.", 2, {}),
    ("PAY", "Refunds over the API", "Merchants issue full and partial refunds through the public API.", 0, {}),
    ("DNT", "Recall reminders", "Remind patients six months after a cleaning, by email or text.", 4, {}),
]


def wait_idle(c: httpx.Client, fid: str, timeout: float = 60) -> dict:
    end = time.time() + timeout
    while time.time() < end:
        f = c.get(f"{BASE}/features/{fid}").json()
        if not f.get("running") and (f.get("pending") or f.get("status") in ("done", "aborted", "failed")):
            return f
        time.sleep(0.5)
    return c.get(f"{BASE}/features/{fid}").json()


def main() -> None:
    c = httpx.Client(timeout=30)
    teams = [f"{o}/{t}" for o in ORGS for t in TEAMS]
    c.post(f"{BASE}/auth/dev-login", json={"login": "demo", "orgs": list(ORGS), "teams": teams}).raise_for_status()
    for product, title, desc, approvals, faults in FEATURES:
        r = c.post(f"{BASE}/features", json={"product_id": product, "title": title, "description": desc,
                                             "simulate_faults": faults, "not_duplicate_reason": "seed"})
        if r.status_code >= 400:
            print(f"{title}: {r.status_code} {r.text[:200]}")
            continue
        fid = r.json()["feature_id"]
        done = 0
        while done < approvals:
            f = wait_idle(c, fid)
            pend = f.get("pending") or []
            if not pend:
                break
            item = pend[0]
            decision = "approve" if item.get("type") in ("gate", None) or item.get("gate") else "resolved"
            r = c.post(f"{BASE}/features/{fid}/decisions/{item["interrupt_id"]}",
                       json={"decision": decision, "feedback": "Looks right.", "active_minutes": 6})
            if r.status_code >= 400:
                print(f"{title}: {item.get('gate') or item.get('type')}: {r.status_code} {r.text[:160]}")
                break
            done += 1
        f = wait_idle(c, fid)
        print(f"{fid} {title}: {f.get('status')} at {[r["key"] for r in f.get("rail") or [] if r["state"] in ("waiting", "running")]}, "
              f"{len(f.get('pending') or [])} waiting")
    c.post(f"{BASE}/incidents", json={"product_id": "FARM", "title": "Sync fails after airplane mode",
                                      "description": "Observations entered offline don't upload.", "severity": "sev2"})


if __name__ == "__main__":
    main()

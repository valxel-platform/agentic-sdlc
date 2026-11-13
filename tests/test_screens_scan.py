"""The built screens in staging: visited, scanned with axe, screenshotted, shown at G6; serious and
critical violations block the release."""

from __future__ import annotations

from types import SimpleNamespace

from valxel_sdlc import screens
from valxel_sdlc.agents.parse import parse_screen_routes
from valxel_sdlc.quality import evaluate

SUMMARY = """3 screens
- S1 /checkin: passed; desktop: CLN-FEAT-1/staging/S1-desktop.png; phone: CLN-FEAT-1/staging/S1-phone.png
- S2 /ready: failed; desktop: CLN-FEAT-1/staging/S2-desktop.png
- S2 serious color-contrast: Save button text is 3.1:1 against its background
- S2 minor region: content outside landmarks
"""


def test_routes_come_from_the_spec():
    ux = "## Screens\n- S1: Check-in (web: acme/web) [/checkin]\n- S2: Detail (mobile: acme/app)\n"
    assert parse_screen_routes(ux) == {"S1": "/checkin"}


def test_a_report_becomes_findings_and_screenshots():
    t = {"repo": "acme/acc", "screens": ["S1", "S2", "S3"]}
    r = screens.result("screens", "staging", t, SUMMARY, "success", "https://ci/run")
    assert r.details["screens"]["S1"] == {"route": "/checkin", "result": "passed", "desktop": "CLN-FEAT-1/staging/S1-desktop.png",
                                          "phone": "CLN-FEAT-1/staging/S1-phone.png"}
    assert [(f.severity, f.rule) for f in r.findings] == [("high", "color-contrast"), ("low", "region")]
    assert r.state == "failed" and r.message == "not visited: S3"


def test_serious_violations_block_and_minor_ones_dont():
    policy = {"screens": {"kind": "security", "stage": "staging", "applies_to": "screens", "block_on": "high",
                          "required": True}}
    t = {"repo": "acme/acc", "screens": ["S1", "S2"]}
    blocked = evaluate("staging", policy, [screens.result("screens", "staging", t, SUMMARY, "success", "")], [])
    assert blocked["status"] == "blocked" and blocked["blocking"][0]["rule"] == "color-contrast"
    fine = SUMMARY.replace("serious color-contrast", "minor color-contrast")
    assert evaluate("staging", policy, [screens.result("screens", "staging", t, fine, "success", "")], [])["status"] == "passed"


def test_staging_scans_the_web_screens_from_the_acceptance_repo():
    ux = "## Screens\n- S1: Check-in (web: acme/web) [/checkin]\n- S2: Detail (mobile: acme/app)\n"
    svc = SimpleNamespace(artifacts=SimpleNamespace(load=lambda ref: ux))
    state = {"feature_id": "CLN-FEAT-1", "artifacts": {"UX": {"id": "u"}}, "flag": "cln-feat-1",
             "change_set": {"acceptance": {"repo": "acme/acc", "sha": "abc"}}}
    [t] = screens.targets(svc, state, "staging")
    assert (t["kind"], t["repo"], t["sha"]) == ("screens", "acme/acc", "abc")
    assert [s["id"] for s in t["screens"]] == ["S1"]  # phone screens aren't visited by route
    assert screens.targets(svc, state, "dev") == []
    assert screens.targets(svc, {**state, "artifacts": {}}, "staging") == []


def test_the_wait_checker_asks_for_the_scan_once_with_the_urls():
    from valxel_sdlc.waits import WaitChecker

    sent = []
    svc = SimpleNamespace(github=SimpleNamespace(dispatch=lambda **kw: sent.append(kw)))
    w = WaitChecker(svc)
    target = {"kind": "screens", "repo": "acme/acc", "sha": "abc", "feature_id": "CLN-FEAT-1", "flag": "f",
              "screens": [{"id": "S1", "route": "/checkin"}]}
    packet = {"feature_id": "CLN-FEAT-1", "wait": "staging", "since": "t0", "quality": {"targets": [target]}}
    results = [{"repo": "acme/web", "url": "https://web.staging"}]
    w._start_post_deploy(packet, results)
    w._start_post_deploy(packet, results)
    [d] = [x for x in sent if x["event_type"] == "valxel-screens"]
    assert d["repo"] == "acme/acc" and d["payload"]["env"] == {"web": "https://web.staging"}
    assert d["payload"]["check"] == "screens / CLN-FEAT-1 / staging"


def test_g6_shows_each_screen_beside_its_mockup():
    t = {"repo": "acme/acc", "screens": ["S1", "S2"]}
    from dataclasses import asdict

    row = asdict(screens.result("screens", "staging", t, SUMMARY, "success", ""))
    state = {"mockups": {"screens": [{"id": "S1", "name": "Check-in"}, {"id": "S2", "name": "Ready"}]},
             "quality": {"staging": {"suites": [row]}}}
    out = screens.g6(state)
    assert out["scanned"] and out["screens"][0]["staging"]["desktop"] == "CLN-FEAT-1/staging/S1-desktop.png"
    assert [v["rule"] for v in out["screens"][1]["violations"]] == ["color-contrast", "region"]
    assert screens.g6({}) is None


def test_screenshots_are_served_from_the_scanning_repo_to_members_only():
    from dataclasses import asdict

    t = {"repo": "acme/acc", "screens": ["S1", "S2"]}
    row = asdict(screens.result("screens", "staging", t, SUMMARY, "success", ""))
    state = {"mockups": {"screens": [{"id": "S1", "name": "Check-in"}]}, "quality": {"staging": {"suites": [row]}}}
    got = []
    svc = SimpleNamespace(github=SimpleNamespace(get_bytes=lambda **kw: got.append(kw) or b"PNG"))
    assert screens.screenshot(svc, state, "S1", "desktop") == b"PNG"
    assert got[0] == {"repo": "acme/acc", "path": "CLN-FEAT-1/staging/S1-desktop.png", "ref": "valxel-screens"}
    assert screens.screenshot(svc, state, "S9", "desktop") is None
    bad = {**state, "quality": {"staging": {"suites": [{**row, "details": {"screens": {"S1": {"desktop": "../x.png"}}}}]}}}
    assert screens.screenshot(svc, bad, "S1", "desktop") is None


def test_a_screen_that_did_not_load_fails_the_suite_even_with_findings():
    from valxel_sdlc import screens

    summary = ("- S1 /c/: passed; desktop: F/staging/S1-desktop.png\n"
               "- S1 serious color-contrast: low contrast\n"
               "- S2 /missing: failed (desktop: HTTP 404, phone: HTTP 404)\n")
    r = screens.result("screens", "staging", {"repo": "a/b", "screens": [{"id": "S1"}, {"id": "S2"}]}, summary, "failure", "u")
    assert r.state == "failed" and "didn't load: S2" in r.message
    assert [f.rule for f in r.findings] == ["color-contrast"]
